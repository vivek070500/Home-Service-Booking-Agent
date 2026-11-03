"""
Agent engine — the LLM conversation loop with hard-coded safety gates.

Key guarantees enforced in APPLICATION CODE (not prompt-only):
  1. book/cancel/reschedule require a valid PendingApproval with approved=True
  2. Approval is granted only when the user's message matches confirmation patterns
  3. Material detail changes (hold_id, booking_id) invalidate prior approval
  4. Idempotency keys are generated & reused automatically
  5. Execution is bounded by turn count, tool-call count, and wall-clock time
"""

import json
import re
import time
import uuid
from typing import Optional, Dict, Any

import openai

from .state import ConversationState, PendingApproval
from .prompts import SYSTEM_PROMPT
from .schemas import TOOL_SCHEMAS
from .memory import MemoryStore
from .tracer import Tracer
from mock_tools import ToolServer


class Agent:
    """Chat agent backed by an LLM + mock tool server."""

    CONSEQUENTIAL_TOOLS = {"book", "cancel", "reschedule"}

    # ── Execution bounds ──────────────────────────────────────────────
    MAX_LLM_TURNS = 20        # max LLM round-trips per user message
    MAX_TOOL_CALLS = 15       # max tool invocations per user message
    MAX_WALL_SECONDS = 120    # hard wall-clock limit per user message
    _MAX_LLM_RETRIES = 6  # retries for tool-name errors + rate limits

    # ── Confirmation detection (application-code enforcement) ─────────
    _CONFIRM_RE = re.compile(
        r"^\s*(yes|yeah|yep|yup|sure|ok|okay|confirm|go ahead|proceed"
        r"|book it|do it|please book|yes please|absolutely|definitely"
        r"|please proceed|please confirm|haan|ha ji)\b",
        re.IGNORECASE,
    )
    _REJECT_RE = re.compile(
        r"^\s*(no|nope|nah|don'?t|cancel that|stop|nevermind"
        r"|never mind|wait|hold on|not now)\b",
        re.IGNORECASE,
    )
    _MODIFY_WORDS = frozenset(
        ["but", "however", "instead", "change", "different",
         "modify", "actually", "update", "switch"]
    )

    # ------------------------------------------------------------------

    def __init__(
        self,
        tool_server: ToolServer,
        api_key: str,
        model: str = "llama3.1:8b",
        user_id: str = "user-1",
        memory: Optional[MemoryStore] = None,
        tracer: Optional[Tracer] = None,
        base_url: Optional[str] = None,
    ):
        self.tools = tool_server
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url or "http://localhost:11434/v1",
        )
        self.model = model
        self.user_id = user_id
        self.memory = memory
        self.tracer = tracer or Tracer()

        self.state = ConversationState()

        # Build system prompt, injecting known preferences if any
        sys_prompt = SYSTEM_PROMPT
        if self.memory:
            pref_context = self.memory.get_context_string(self.user_id)
            if pref_context:
                sys_prompt += "\n" + pref_context

        self.messages: list = [{"role": "system", "content": sys_prompt}]

        # Stable idempotency keys: (action:identifier) → key
        self._idem_keys: Dict[str, str] = {}

    # ==================================================================
    # Public API
    # ==================================================================

    def chat(self, user_message: str) -> str:
        """Process one user message and return the agent's text reply."""

        # Step 0: check for proactive events (e.g. provider cancellations)
        self._inject_pending_events()

        # Step 1: check if user confirmed/rejected a pending approval
        self._process_approval(user_message)

        # Step 2: append user message
        self.messages.append({"role": "user", "content": user_message})
        self.tracer.user_message(user_message)

        # Step 3: LLM loop with bounds
        start = time.time()
        tool_calls_count = 0

        for _turn in range(self.MAX_LLM_TURNS):
            # Wall-clock guard
            if time.time() - start > self.MAX_WALL_SECONDS:
                return self._bail("I've been processing too long — let me stop here.")

            # Call LLM (with retry on tool-name hallucination)
            llm_start = self.tracer.llm_call_start(self.model, len(self.messages))
            resp = None
            valid_names = [t["function"]["name"] for t in TOOL_SCHEMAS]
            for _llm_retry in range(self._MAX_LLM_RETRIES):
                try:
                    resp = self.client.chat.completions.create(
                        model=self.model,
                        messages=self.messages,
                        tools=TOOL_SCHEMAS,
                        tool_choice="auto",
                    )
                    break
                except openai.BadRequestError as exc:
                    err_msg = str(exc)
                    if ("tool_use_failed" in err_msg
                            or "not in request.tools" in err_msg):
                        self.messages.append({
                            "role": "user",
                            "content": (
                                "[SYSTEM] Your previous tool call used a wrong "
                                "name. The ONLY valid tool names are: "
                                f"{', '.join(valid_names)}. "
                                "Please retry with the correct spelling."
                            ),
                        })
                        continue
                    return self._bail(f"API error: {exc}")
                except openai.AuthenticationError:
                    return self._bail("Invalid API key. Please check your API key.")
                except openai.RateLimitError:
                    wait = 15 * (_llm_retry + 1)
                    time.sleep(wait)
                    continue
                except openai.APIError as exc:
                    return self._bail(f"API error: {exc}")
                except Exception as exc:
                    return self._bail(f"Unexpected error: {type(exc).__name__}: {exc}")

            if resp is None:
                return self._bail(
                    "The model kept using invalid tool names. Please try again."
                )

            choice = resp.choices[0]
            msg = choice.message

            # Extract token usage if available
            usage = None
            if resp.usage:
                usage = {
                    "prompt_tokens": resp.usage.prompt_tokens,
                    "completion_tokens": resp.usage.completion_tokens,
                    "total_tokens": resp.usage.total_tokens,
                }

            self.tracer.llm_call_end(
                llm_start,
                finish_reason=choice.finish_reason or "unknown",
                tool_call_count=len(msg.tool_calls) if msg.tool_calls else 0,
                usage=usage,
            )

            # Serialize and persist the assistant message
            assistant_dict = self._serialize_message(msg)
            self.messages.append(assistant_dict)

            # If no tool calls → check for text-based tool call fallback
            if not msg.tool_calls:
                parsed = self._parse_text_tool_call(msg.content)
                if parsed:
                    # Model dumped tool call as text — re-inject as structured
                    name, args, remaining_text = parsed
                    fake_id = f"call_text_{uuid.uuid4().hex[:8]}"
                    # Replace the assistant message with one containing the tool call
                    self.messages[-1] = {
                        "role": "assistant",
                        "content": remaining_text or None,
                        "tool_calls": [{
                            "id": fake_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }],
                    }
                    tool_calls_count += 1
                    tool_start = time.time()
                    result = self._dispatch(name, args)
                    tool_latency = (time.time() - tool_start) * 1000
                    self.tracer.tool_call(name, args, result, tool_latency)
                    self.messages.append(self._tool_result(fake_id, result))
                    continue  # let LLM see the result and respond

                response_text = msg.content or ""
                self.tracer.agent_response(response_text)
                return response_text

            # Process every tool call in this batch
            for tc in msg.tool_calls:
                tool_calls_count += 1

                if tool_calls_count > self.MAX_TOOL_CALLS:
                    self.messages.append(self._tool_result(
                        tc.id,
                        {"error": "limit_reached",
                         "message": "Tool call limit exceeded. Respond to the user now."},
                    ))
                    continue

                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    self.messages.append(self._tool_result(
                        tc.id, {"error": "invalid_json", "message": "Malformed arguments."},
                    ))
                    continue

                tool_start = time.time()
                result = self._dispatch(name, args)
                tool_latency = (time.time() - tool_start) * 1000
                self.tracer.tool_call(name, args, result, tool_latency)

                self.messages.append(self._tool_result(tc.id, result))

        # Exhausted turns
        return self._bail("Reached maximum processing turns.")

    # ==================================================================
    # Approval gate — enforced in APPLICATION CODE
    # ==================================================================

    def _process_approval(self, user_message: str) -> None:
        """
        Before the LLM sees the message, check if the user is confirming
        or rejecting a pending proposal.  This is the ONLY path that can
        set approved=True.
        """
        pa = self.state.pending_approval
        if pa is None or pa.approved:
            return

        text = user_message.strip()
        words = set(text.lower().split())
        has_modification = bool(words & self._MODIFY_WORDS)

        if self._CONFIRM_RE.search(text) and not has_modification:
            pa.approved = True
        elif self._REJECT_RE.search(text) or has_modification:
            # User rejected or wants changes → clear the proposal
            self.state.pending_approval = None
        # else: ambiguous (e.g. a question) → keep pending, let LLM handle

    def _guard_consequential(self, tool_name: str, args: dict) -> Optional[dict]:
        """
        Return an error dict if the tool call should be blocked, or None if allowed.
        """
        if tool_name not in self.CONSEQUENTIAL_TOOLS:
            return None

        pa = self.state.pending_approval

        if pa is None:
            return {
                "error": "approval_required",
                "message": (
                    f"Cannot {tool_name} without user approval. "
                    "You MUST follow this exact sequence: "
                    "1) call hold_slot to reserve the slot, "
                    "2) call propose_action with the hold details, "
                    "3) STOP and respond to the user showing the proposal, "
                    "4) wait for the user to say 'yes' before calling {tool_name}."
                ),
            }

        if not pa.approved:
            return {
                "error": "approval_pending",
                "message": (
                    "The user has not confirmed yet. "
                    "Present the proposed details and wait for their response."
                ),
            }

        if pa.action != tool_name:
            self.state.pending_approval = None
            return {
                "error": "approval_mismatch",
                "message": (
                    f"User approved '{pa.action}' but you called '{tool_name}'. "
                    "Call propose_action again with the correct action."
                ),
            }

        # Validate material details haven't been swapped
        if tool_name == "book":
            proposed_hold = pa.details.get("hold_id")
            actual_hold = args.get("hold_id")
            if proposed_hold and actual_hold and proposed_hold != actual_hold:
                self.state.pending_approval = None
                return {
                    "error": "details_changed",
                    "message": "Hold ID differs from what was approved. Get fresh approval.",
                }

        if tool_name in ("cancel", "reschedule"):
            proposed_bid = pa.details.get("booking_id")
            actual_bid = args.get("booking_id")
            if proposed_bid and actual_bid and proposed_bid != actual_bid:
                self.state.pending_approval = None
                return {
                    "error": "details_changed",
                    "message": "Booking ID differs from what was approved. Get fresh approval.",
                }

        return None  # all checks passed

    # ==================================================================
    # Tool dispatch
    # ==================================================================

    def _dispatch(self, name: str, args: dict) -> dict:
        """Route a tool call to the mock server, with approval & idempotency."""

        # Approval gate
        block = self._guard_consequential(name, args)
        if block:
            return block

        try:
            if name == "search_providers":
                return self.tools.search_providers(
                    service=args["service"],
                    area=args["area"],
                    date_range=args.get("date_range"),
                )

            elif name == "get_provider":
                return self.tools.get_provider(args["provider_id"])

            elif name == "hold_slot":
                result = self.tools.hold_slot(args["provider_id"], args["slot"])
                if result.get("status") == "ok":
                    self.state.current_hold = result
                return result

            elif name == "book":
                hold_id = args["hold_id"]
                idem = self._idem_key("book", hold_id)
                result = self.tools.book(hold_id, idem, user_id=self.user_id)

                if result.get("status") == "success":
                    self.state.current_booking = result
                    self.state.pending_approval = None
                # on timeout/slot_taken the LLM decides next steps
                return result

            elif name == "get_booking_status":
                hold_id = args.get("hold_id")
                idem = self._idem_keys.get(f"book:{hold_id}")
                return self.tools.get_booking_status(
                    hold_id=hold_id,
                    idempotency_key=idem,
                )

            elif name == "cancel":
                bid = args["booking_id"]
                idem = self._idem_key("cancel", bid)
                result = self.tools.cancel(bid, idem)
                if result.get("status") == "ok":
                    self.state.pending_approval = None
                return result

            elif name == "reschedule":
                bid = args["booking_id"]
                slot = args["slot"]
                idem = self._idem_key("reschedule", bid, slot)
                result = self.tools.reschedule(bid, slot, idem)
                if result.get("status") == "ok":
                    self.state.pending_approval = None
                return result

            elif name == "list_bookings":
                return self.tools.list_bookings(self.user_id)

            elif name == "propose_action":
                return self._handle_propose(args)

            # ── Memory tools ──────────────────────────────────────────
            elif name == "remember_preference":
                return self._handle_remember(args)

            elif name == "forget_preference":
                return self._handle_forget(args)

            elif name == "list_preferences":
                return self._handle_list_preferences()

            elif name == "check_events":
                return self.tools.check_events(self.user_id)

            else:
                return {"error": "unknown_tool", "message": f"No such tool: {name}"}

        except KeyError as exc:
            return {"error": "missing_argument", "message": f"Missing required argument: {exc}"}
        except Exception as exc:
            return {"error": "tool_error", "message": f"{type(exc).__name__}: {exc}"}

    def _handle_propose(self, args: dict) -> dict:
        action = args.get("action", "")
        details = args.get("details", {})
        summary = args.get("summary", "")

        if action not in self.CONSEQUENTIAL_TOOLS:
            return {
                "error": "invalid_action",
                "message": f"propose_action supports: {', '.join(sorted(self.CONSEQUENTIAL_TOOLS))}",
            }

        # ── Price integrity check for bookings ────────────────────────
        if action == "book":
            hold = self.state.current_hold
            proposed_hold_id = details.get("hold_id")
            proposed_price = details.get("price")

            if hold is None:
                return {
                    "error": "no_active_hold",
                    "message": "No active hold found. Call hold_slot first.",
                }

            if proposed_hold_id and proposed_hold_id != hold.get("hold_id"):
                return {
                    "error": "hold_id_mismatch",
                    "message": (
                        f"Proposed hold_id '{proposed_hold_id}' doesn't match "
                        f"active hold '{hold.get('hold_id')}'. Use the correct hold."
                    ),
                }

            if proposed_price is not None and proposed_price != hold.get("price"):
                return {
                    "error": "price_mismatch",
                    "message": (
                        f"Proposed price ₹{proposed_price} doesn't match the "
                        f"actual hold price ₹{hold.get('price')}. "
                        "Show the user the correct price from the hold."
                    ),
                }

            # Auto-fill details from hold to ensure accuracy
            details["hold_id"] = hold["hold_id"]
            details["price"] = hold["price"]

        self.state.pending_approval = PendingApproval(
            action=action,
            details=details,
            summary=summary,
            approved=False,
        )

        return {
            "status": "pending_user_confirmation",
            "instruction": (
                "Present these details clearly to the user and ask for "
                "explicit confirmation. Do NOT call the action tool yet."
            ),
            "action": action,
            "details": details,
            "summary": summary,
        }

    # ==================================================================
    # Helpers
    # ==================================================================

    def _idem_key(self, action: str, *parts) -> str:
        """Stable idempotency key — same (action, parts) always returns the same key."""
        compound = f"{action}:" + ":".join(str(p) for p in parts)
        if compound not in self._idem_keys:
            self._idem_keys[compound] = f"idem-{uuid.uuid4().hex[:12]}"
        return self._idem_keys[compound]

    # Valid tool names for text-based fallback parsing
    _VALID_TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOL_SCHEMAS)

    def _parse_text_tool_call(self, content: str) -> Optional[tuple]:
        """
        Fallback: if the model dumps a tool call as text (instead of structured
        tool_calls), parse it and return (name, args_dict, remaining_text).
        Returns None if no valid tool call is found in the text.
        """
        if not content:
            return None

        # Pattern: {"name": "tool_name", "parameters": {...}}
        match = re.search(
            r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"parameters"\s*:\s*(\{[^}]*\})\s*\}',
            content,
        )
        if not match:
            # Also try: {"name": "tool_name", "arguments": {...}}
            match = re.search(
                r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{[^}]*\})\s*\}',
                content,
            )
        if not match:
            return None

        name = match.group(1)
        if name not in self._VALID_TOOL_NAMES:
            return None

        try:
            args = json.loads(match.group(2))
        except json.JSONDecodeError:
            # Try parsing the whole matched block for nested objects
            try:
                full_match = re.search(r'\{[^{]*"name"\s*:\s*"' + name + r'".*\}', content, re.DOTALL)
                if full_match:
                    obj = json.loads(full_match.group(0))
                    args = obj.get("parameters", obj.get("arguments", {}))
                else:
                    return None
            except (json.JSONDecodeError, AttributeError):
                return None

        # Extract any text before/after the JSON
        remaining = content[:match.start()].strip()
        return (name, args, remaining)

    @staticmethod
    def _serialize_message(msg) -> dict:
        """Convert a ChatCompletionMessage to a plain dict for the messages list."""
        d: dict = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        return d

    @staticmethod
    def _tool_result(tool_call_id: str, result: dict) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result, ensure_ascii=False),
        }

    def _bail(self, reason: str) -> str:
        msg = f"⚠️ {reason}"
        self.messages.append({"role": "assistant", "content": msg})
        self.tracer.bail(reason)
        return msg

    # ==================================================================
    # Proactive events
    # ==================================================================

    def _inject_pending_events(self) -> None:
        """
        Poll the tool server for pending events (e.g. provider cancellations).
        If any exist, inject them as a system message so the LLM knows
        to inform the user — even if the user asked about something else.
        """
        result = self.tools.check_events(self.user_id)
        events = result.get("events", [])
        if not events:
            return

        self.tracer.record("proactive_events_detected", count=len(events))

        # Build a system-level notification for each event
        parts = []
        for evt in events:
            if evt["type"] == "provider_cancellation":
                parts.append(
                    f"⚠️ PROVIDER CANCELLATION: The booking {evt['booking_id']} with "
                    f"{evt['provider_name']} for {evt['service']} on {evt['slot']} "
                    f"(₹{evt['price']}) has been CANCELLED BY THE PROVIDER. "
                    f"You must: 1) Inform the user about this cancellation, "
                    f"2) Search for replacement providers for the same service and area, "
                    f"3) Propose a replacement and get fresh approval before rebooking. "
                    f"Do NOT rebook without the user's explicit confirmation."
                )

        if parts:
            notification = "\n\n".join(parts)
            self.messages.append({
                "role": "system",
                "content": notification,
            })

    # ==================================================================
    # Memory tools
    # ==================================================================

    def _handle_remember(self, args: dict) -> dict:
        if not self.memory:
            return {"status": "ok", "message": "Preference noted (memory store not configured)."}

        key = args.get("key", "")
        value = args.get("value", "")
        source = args.get("source", "explicit")

        if not key or not value:
            return {"error": "missing_fields", "message": "Both 'key' and 'value' are required."}

        if source not in ("explicit", "inferred"):
            source = "explicit"

        self.memory.set(self.user_id, key, value, source)
        return {
            "status": "ok",
            "message": f"Stored preference: {key} = {value} ({source}).",
        }

    def _handle_forget(self, args: dict) -> dict:
        if not self.memory:
            return {"status": "ok", "message": "No memory store configured."}

        key = args.get("key", "")
        if not key:
            return {"error": "missing_key", "message": "'key' is required."}

        forgotten = self.memory.forget(self.user_id, key)
        if forgotten:
            return {"status": "ok", "message": f"Forgot preference: {key}."}
        return {"status": "ok", "message": f"No preference found for key: {key}."}

    def _handle_list_preferences(self) -> dict:
        if not self.memory:
            return {"status": "ok", "preferences": {}, "message": "No memory store configured."}

        prefs = self.memory.list_all(self.user_id)
        formatted = {}
        for key, info in prefs.items():
            formatted[key] = {
                "value": info["value"],
                "source": info["source"],
            }
        return {"status": "ok", "preferences": formatted, "count": len(formatted)}
