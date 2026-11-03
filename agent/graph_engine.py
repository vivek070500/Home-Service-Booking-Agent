"""
LangGraph-based agent engine — workflow enforced by graph structure.

The LLM NEVER has access to consequential tools (book/cancel/reschedule).
Those are executed by the graph when the user confirms a proposal.

Graph flow:
    user message
         ↓
    [check_events]  ← inject provider cancellation alerts
         ↓
    [check_approval] ← if awaiting: yes→execute action, no→clear
         ↓
    [agent]  ← LLM with SAFE tools only
         ↓
    has tool calls? ──yes──→ [tools] → back to [agent]
         ↓ no
    return text to user
"""

import json
import re
import time
import uuid
from typing import Optional, Dict, Any, TypedDict, Annotated, Sequence

from langchain_ollama import ChatOllama
from langchain_core.messages import (
    BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage,
)
from langchain_core.tools import tool as lc_tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from .state import PendingApproval
from .prompts import SYSTEM_PROMPT
from .memory import MemoryStore
from .tracer import Tracer
from mock_tools import ToolServer


# ══════════════════════════════════════════════════════════════════════
# Graph State
# ══════════════════════════════════════════════════════════════════════

class GraphState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    phase: str                            # "chatting" | "awaiting_approval"
    proposal: Optional[Dict[str, Any]]    # pending proposal details
    hold: Optional[Dict[str, Any]]        # current hold
    execution_result: Optional[str]       # result of executed action


# ══════════════════════════════════════════════════════════════════════
# Confirmation detection (same regex as old engine)
# ══════════════════════════════════════════════════════════════════════

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


def _is_confirmation(text: str) -> bool:
    words = set(text.lower().split())
    return bool(_CONFIRM_RE.search(text)) and not bool(words & _MODIFY_WORDS)


def _is_rejection(text: str) -> bool:
    words = set(text.lower().split())
    return bool(_REJECT_RE.search(text)) or bool(words & _MODIFY_WORDS)


# ══════════════════════════════════════════════════════════════════════
# GraphAgent — wraps LangGraph, exposes chat()
# ══════════════════════════════════════════════════════════════════════

class GraphAgent:
    """
    Chat agent using LangGraph for workflow enforcement.
    
    The LLM only has access to safe tools. Consequential actions
    (book/cancel/reschedule) are executed by the graph after user approval.
    """

    MAX_AGENT_LOOPS = 12

    def __init__(
        self,
        tool_server: ToolServer,
        model: str = "qwen2.5:14b",
        user_id: str = "user-1",
        memory: Optional[MemoryStore] = None,
        tracer: Optional[Tracer] = None,
        api_key: str = "ollama",
    ):
        self.ts = tool_server
        self.model_name = model
        self.user_id = user_id
        self.memory = memory
        self.tracer = tracer or Tracer()

        # Build system prompt
        sys_prompt = SYSTEM_PROMPT
        if self.memory:
            pref_context = self.memory.get_context_string(self.user_id)
            if pref_context:
                sys_prompt += "\n" + pref_context

        # Persistent state across turns
        self._phase = "chatting"
        self._proposal: Optional[Dict] = None
        self._hold: Optional[Dict] = None
        self._idem_keys: Dict[str, str] = {}
        self._known_providers: set = set()  # provider names returned by tools

        # Build LLM and tools
        self.llm = ChatOllama(model=model)
        self._safe_tools = self._build_safe_tools()
        self._llm_with_tools = self.llm.bind_tools(self._safe_tools)

        # Messages list (persistent across chat() calls)
        self.messages: list = [SystemMessage(content=sys_prompt)]

    # ── Build safe tools (no book/cancel/reschedule) ──────────────

    def _build_safe_tools(self):
        ts = self.ts
        agent = self

        @lc_tool
        def search_providers(
            service: str,
            area: str = "",
            date_range: str = "",
        ) -> str:
            """Search for home-service providers. service must be one of: plumbing, electrical, ac_service, cleaning, carpentry, painting."""
            result = ts.search_providers(
                service=service,
                area=area or None,
                date_range=date_range or None,
            )
            # Track known providers
            for p in result.get("providers", []):
                agent._known_providers.add(p["name"].lower())
            agent.tracer.tool_call("search_providers", {"service": service, "area": area}, result, 0)
            return json.dumps(result, ensure_ascii=False)

        @lc_tool
        def get_provider(provider_id: str) -> str:
            """Get detailed info and reviews for a specific provider."""
            result = ts.get_provider(provider_id)
            if result.get("status") == "ok":
                agent._known_providers.add(result["provider"]["name"].lower())
            agent.tracer.tool_call("get_provider", {"provider_id": provider_id}, result, 0)
            return json.dumps(result, ensure_ascii=False)

        @lc_tool
        def hold_slot(provider_id: str, slot: str) -> str:
            """Temporarily reserve a time slot. Call this BEFORE propose_action."""
            result = ts.hold_slot(provider_id, slot)
            if result.get("status") == "ok":
                agent._hold = result
            agent.tracer.tool_call("hold_slot", {"provider_id": provider_id, "slot": slot}, result, 0)
            return json.dumps(result, ensure_ascii=False)

        @lc_tool
        def propose_action(action: str, summary: str, hold_id: str = "", booking_id: str = "", new_slot: str = "") -> str:
            """Propose a consequential action (book/cancel/reschedule) for user approval. After calling this, STOP and wait for user confirmation."""
            details = {}

            if action == "book":
                if agent._hold is None:
                    return json.dumps({"error": "no_active_hold", "message": "Call hold_slot first before proposing a booking."})
                details = {
                    "hold_id": agent._hold["hold_id"],
                    "price": agent._hold["price"],
                    "provider_id": agent._hold.get("provider_id", ""),
                    "slot": agent._hold.get("slot", ""),
                }
            elif action == "cancel":
                if not booking_id:
                    return json.dumps({"error": "missing_booking_id", "message": "Provide booking_id to cancel."})
                details = {"booking_id": booking_id}
            elif action == "reschedule":
                if not booking_id or not new_slot:
                    return json.dumps({"error": "missing_fields", "message": "Provide booking_id and new_slot."})
                details = {"booking_id": booking_id, "new_slot": new_slot}
            else:
                return json.dumps({"error": "invalid_action", "message": f"Action must be book, cancel, or reschedule."})

            agent._proposal = {"action": action, "details": details, "summary": summary}
            agent._phase = "awaiting_approval"
            agent.tracer.tool_call("propose_action", {"action": action, "summary": summary}, {"status": "pending_user_confirmation"}, 0)
            return json.dumps({
                "status": "pending_user_confirmation",
                "message": "Present these details to the user and ask for confirmation. Do NOT proceed until they say yes.",
                "action": action,
                "details": details,
                "summary": summary,
            }, ensure_ascii=False)

        @lc_tool
        def list_bookings() -> str:
            """List all bookings for the current user."""
            result = ts.list_bookings(agent.user_id)
            agent.tracer.tool_call("list_bookings", {}, result, 0)
            return json.dumps(result, ensure_ascii=False)

        @lc_tool
        def get_booking_status(hold_id: str) -> str:
            """Check if a booking was created for a given hold (use after timeouts)."""
            idem = agent._idem_keys.get(f"book:{hold_id}")
            result = ts.get_booking_status(hold_id=hold_id, idempotency_key=idem)
            agent.tracer.tool_call("get_booking_status", {"hold_id": hold_id}, result, 0)
            return json.dumps(result, ensure_ascii=False)

        @lc_tool
        def check_events() -> str:
            """Check for any pending notifications like provider cancellations."""
            result = ts.check_events(agent.user_id)
            agent.tracer.tool_call("check_events", {}, result, 0)
            return json.dumps(result, ensure_ascii=False)

        @lc_tool
        def remember_preference(key: str, value: str, source: str = "explicit") -> str:
            """Store a user preference. source should be 'explicit' or 'inferred'."""
            if not agent.memory:
                return json.dumps({"status": "ok", "message": "Noted (no memory store)."})
            agent.memory.set(agent.user_id, key, value, source)
            agent.tracer.tool_call("remember_preference", {"key": key, "value": value}, {"status": "ok"}, 0)
            return json.dumps({"status": "ok", "message": f"Stored: {key} = {value} ({source})"})

        @lc_tool
        def forget_preference(key: str) -> str:
            """Remove a stored user preference."""
            if not agent.memory:
                return json.dumps({"status": "ok", "message": "No memory store."})
            agent.memory.forget(agent.user_id, key)
            agent.tracer.tool_call("forget_preference", {"key": key}, {"status": "ok"}, 0)
            return json.dumps({"status": "ok", "message": f"Forgot: {key}"})

        @lc_tool
        def list_preferences() -> str:
            """List all stored user preferences."""
            if not agent.memory:
                return json.dumps({"status": "ok", "preferences": {}, "count": 0})
            prefs = agent.memory.list_all(agent.user_id)
            result = {"status": "ok", "count": len(prefs), "preferences": {
                k: {"value": v["value"], "source": v["source"]} for k, v in prefs.items()
            }}
            agent.tracer.tool_call("list_preferences", {}, result, 0)
            return json.dumps(result, ensure_ascii=False)

        return [
            search_providers, get_provider, hold_slot, propose_action,
            list_bookings, get_booking_status, check_events,
            remember_preference, forget_preference, list_preferences,
        ]

    # ── Execute consequential action (graph-controlled) ───────────

    def _execute_proposal(self) -> Dict:
        """Execute the approved proposal. Called by the graph, not the LLM."""
        if not self._proposal:
            return {"error": "no_proposal", "message": "Nothing to execute."}

        action = self._proposal["action"]
        details = self._proposal["details"]

        try:
            if action == "book":
                hold_id = details["hold_id"]
                idem_key = self._idem_key("book", hold_id)
                result = self.ts.book(hold_id, idem_key, user_id=self.user_id)
                if result.get("status") == "success":
                    self._proposal = None
                    self._hold = None
                elif result.get("error") == "timeout":
                    status = self.ts.get_booking_status(hold_id=hold_id, idempotency_key=idem_key)
                    result["booking_check"] = status
                return result

            elif action == "cancel":
                bid = details["booking_id"]
                idem_key = self._idem_key("cancel", bid)
                result = self.ts.cancel(bid, idem_key)
                if result.get("http_status") == 503:
                    result = self.ts.cancel(bid, idem_key)
                if result.get("status") == "ok":
                    self._proposal = None
                return result

            elif action == "reschedule":
                bid = details["booking_id"]
                new_slot = details["new_slot"]
                idem_key = self._idem_key("reschedule", bid, new_slot)
                result = self.ts.reschedule(bid, new_slot, idem_key)
                if result.get("status") == "ok":
                    self._proposal = None
                return result

            else:
                return {"error": "unknown_action", "message": f"Unknown action: {action}"}

        except Exception as exc:
            return {"error": "execution_error", "message": str(exc)}

    def _idem_key(self, action: str, *parts) -> str:
        compound = f"{action}:" + ":".join(str(p) for p in parts)
        if compound not in self._idem_keys:
            self._idem_keys[compound] = f"idem-{uuid.uuid4().hex[:12]}"
        return self._idem_keys[compound]

    # Valid tool names for fallback parsing
    _SAFE_TOOL_NAMES = {
        "search_providers", "get_provider", "hold_slot", "propose_action",
        "list_bookings", "get_booking_status", "check_events",
        "remember_preference", "forget_preference", "list_preferences",
    }

    def _validate_response(self, text: str) -> str:
        """
        Check LLM response for hallucinated providers.
        If found, silently regenerate with a correction.
        """
        if not self._known_providers or not text:
            return text

        provider_mentions = re.findall(
            r'(\b[A-Z][a-z]+(?:\s+[A-Z&][a-z]*)*'
            r'(?:\s+(?:Plumbing|Electrical|Services|Cleaning|AC|Carpentry|Painting))+)\b',
            text,
        )

        hallucinated = [n for n in provider_mentions if n.lower() not in self._known_providers]

        if hallucinated:
            self.tracer.record("hallucination_detected", providers=hallucinated)
            # Silently re-ask the LLM with a correction
            self.messages.append(SystemMessage(
                content=(
                    f"CORRECTION: You mentioned providers that do not exist: "
                    f"{', '.join(hallucinated)}. "
                    f"Only use providers from search results. "
                    f"If no provider matches the budget, say so honestly. "
                    f"Regenerate your response using ONLY real data."
                )
            ))
            corrected = self.llm.invoke(self.messages)
            self.messages.append(corrected)
            return corrected.content or text

        return text

    def _parse_text_tool_call(self, content: str):
        """
        If the model dumped a tool call as JSON text, parse it.
        Returns (name, args_dict, remaining_text) or None.
        """
        if not content or "{" not in content:
            return None

        # Match {"name": "tool_name", "parameters": {...}}
        match = re.search(
            r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"parameters"\s*:\s*(\{[^}]*\})\s*\}',
            content,
        )
        if not match:
            match = re.search(
                r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{[^}]*\})\s*\}',
                content,
            )
        if not match:
            return None

        name = match.group(1)
        if name not in self._SAFE_TOOL_NAMES:
            return None

        try:
            args = json.loads(match.group(2))
        except json.JSONDecodeError:
            return None

        remaining = content[:match.start()].strip()
        return (name, args, remaining)

    # ── Proactive event injection ─────────────────────────────────

    def _inject_pending_events(self):
        result = self.ts.check_events(self.user_id)
        events = result.get("events", [])
        if not events:
            return

        self.tracer.record("proactive_events_detected", count=len(events))
        parts = []
        for evt in events:
            if evt["type"] == "provider_cancellation":
                parts.append(
                    f"⚠️ PROVIDER CANCELLATION: Booking {evt['booking_id']} with "
                    f"{evt['provider_name']} for {evt['service']} on {evt['slot']} "
                    f"(₹{evt['price']}) was CANCELLED BY THE PROVIDER. "
                    f"Inform the user, search for replacements, and propose a new booking."
                )
        if parts:
            self.messages.append(SystemMessage(content="\n\n".join(parts)))

    # ══════════════════════════════════════════════════════════════
    # Main chat() — the only public API
    # ══════════════════════════════════════════════════════════════

    def chat(self, user_message: str) -> str:
        """Process one user message and return agent's text reply."""

        # Step 0: proactive events
        self._inject_pending_events()

        # Step 1: if awaiting approval, check confirmation
        if self._phase == "awaiting_approval" and self._proposal:
            if _is_confirmation(user_message):
                self.tracer.user_message(user_message)
                self.tracer.record("approval", action=self._proposal["action"], outcome="confirmed")

                action = self._proposal["action"]
                summary = self._proposal.get("summary", "")
                details = self._proposal.get("details", {})
                result = self._execute_proposal()
                self.tracer.tool_call(f"execute_{action}", {}, result, 0)
                self._phase = "chatting"

                # Build deterministic response (don't trust LLM here)
                action_word = {"book": "booking", "cancel": "cancellation", "reschedule": "reschedule"}.get(action, action)
                if result.get("status") == "success" or result.get("status") == "ok":
                    text = f"✅ Your {action_word} has been confirmed!"
                    if result.get("booking_id"):
                        text += f"\n📋 Booking ID: {result['booking_id']}"
                    if action == "book":
                        provider = details.get("provider_id", "").replace("_", " ").title()
                        slot = details.get("slot", "")
                        price = details.get("price", "")
                        if provider or slot:
                            text += f"\n🔧 {provider} — {slot}"
                        if price:
                            text += f" — ₹{price}"
                    text += "\n\nIs there anything else I can help you with?"
                elif result.get("error") == "timeout":
                    text = f"⏳ The {action_word} request timed out. Let me check the status..."
                    if result.get("booking_check", {}).get("status") == "confirmed":
                        text = f"✅ Your {action_word} went through despite the timeout!\n📋 Booking ID: {result['booking_check'].get('booking_id', 'N/A')}"
                elif result.get("error"):
                    error_type = result.get("error", "")
                    text = f"⚠️ There was an issue with your {action_word}: {result.get('message', error_type)}"
                    if error_type in ("slot_taken", "slot_conflict"):
                        suggested = result.get("suggested_slots", [])
                        if suggested:
                            text += f"\n\nHere are some other available slots: {', '.join(suggested)}"
                        text += "\n\nWould you like to try a different slot, or should I search for other providers?"
                    elif error_type == "hold_expired":
                        text += "\n\nThe hold expired before we could book. Would you like me to try reserving the slot again?"
                else:
                    text = f"Action completed. Result: {json.dumps(result)}"

                # Inject into message history so LLM has context for future turns
                self.messages.append(HumanMessage(content=user_message))
                self.messages.append(AIMessage(content=text))
                self.tracer.agent_response(text)
                self._proposal = None
                return text

            elif _is_rejection(user_message):
                self.tracer.user_message(user_message)
                self.tracer.record("approval", action=self._proposal["action"], outcome="rejected")
                self._proposal = None
                self._phase = "chatting"
                # Fall through to normal chat

        # Step 2: normal chat with safe tools
        self.messages.append(HumanMessage(content=user_message))
        self.tracer.user_message(user_message)

        for _turn in range(self.MAX_AGENT_LOOPS):
            llm_start = self.tracer.llm_call_start(self.model_name, len(self.messages))

            response = self._llm_with_tools.invoke(self.messages)
            self.messages.append(response)

            tc_count = len(response.tool_calls) if response.tool_calls else 0
            self.tracer.llm_call_end(llm_start, finish_reason="tool_calls" if tc_count else "stop", tool_call_count=tc_count)

            # No tool calls → check for text-based tool call fallback
            if not response.tool_calls:
                text = response.content or ""

                # Fallback: model dumped a tool call as JSON text
                parsed = self._parse_text_tool_call(text)
                if parsed:
                    tool_name, tool_args, remaining_text = parsed
                    tool_fn = next((t for t in self._safe_tools if t.name == tool_name), None)
                    if tool_fn:
                        # Replace last message with clean text (no JSON)
                        self.messages[-1] = AIMessage(content=remaining_text or "")
                        try:
                            tool_result = tool_fn.invoke(tool_args)
                        except Exception as exc:
                            tool_result = json.dumps({"error": str(exc)})
                        fake_id = f"text_{uuid.uuid4().hex[:8]}"
                        self.messages[-1] = AIMessage(
                            content=remaining_text or "",
                            tool_calls=[{"id": fake_id, "name": tool_name, "args": tool_args}],
                        )
                        self.messages.append(ToolMessage(
                            content=tool_result if isinstance(tool_result, str) else json.dumps(tool_result),
                            tool_call_id=fake_id,
                        ))
                        self.tracer.tool_call(tool_name, tool_args, {"status": "ok"}, 0)
                        continue  # let LLM respond to tool result

                # Auto-detect: if model has a hold and asks user to confirm
                if (self._hold and not self._proposal
                        and self._phase == "chatting"
                        and any(w in text.lower() for w in ["confirm", "yes", "proceed", "go ahead", "book"])):
                    self._proposal = {
                        "action": "book",
                        "details": {
                            "hold_id": self._hold["hold_id"],
                            "price": self._hold["price"],
                            "provider_id": self._hold.get("provider_id", ""),
                            "slot": self._hold.get("slot", ""),
                        },
                        "summary": "Auto-detected proposal from hold",
                    }
                    self._phase = "awaiting_approval"
                text = self._validate_response(text)
                self.tracer.agent_response(text)
                return text

            # Execute tool calls
            for tc in response.tool_calls:
                tool_name = tc["name"]
                tool_args = tc["args"]

                # Find and invoke the tool
                tool_fn = next((t for t in self._safe_tools if t.name == tool_name), None)
                if tool_fn is None:
                    tool_result = json.dumps({"error": "unknown_tool", "message": f"No tool named '{tool_name}'. Check spelling."})
                else:
                    tool_result = tool_fn.invoke(tool_args)

                self.messages.append(ToolMessage(
                    content=tool_result if isinstance(tool_result, str) else json.dumps(tool_result),
                    tool_call_id=tc["id"],
                ))

                # If propose_action was called → let LLM generate proposal text, then stop
                if tool_name == "propose_action" and self._phase == "awaiting_approval":
                    # Continue the loop so LLM can generate the proposal message
                    pass

            # If we just entered awaiting_approval, let LLM respond with proposal text
            if self._phase == "awaiting_approval":
                llm_start2 = self.tracer.llm_call_start(self.model_name, len(self.messages))
                final = self.llm.invoke(self.messages)  # no tools — just generate text
                self.messages.append(final)
                self.tracer.llm_call_end(llm_start2, finish_reason="stop", tool_call_count=0)
                text = self._validate_response(final.content or "")
                self.tracer.agent_response(text)
                return text

        return "⚠️ Reached maximum processing turns."
