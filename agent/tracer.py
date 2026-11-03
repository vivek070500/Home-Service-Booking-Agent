"""
Observability tracer — records a structured trace for every agent run.

Captures: user messages, LLM calls (model, latency), tool calls (name, args,
result, latency), approval checks, state transitions, and token estimates.
Writes to a JSON file per session.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional


class Tracer:
    """Append-only event log for one agent session."""

    def __init__(self):
        self.events: List[Dict[str, Any]] = []
        self.session_start: float = time.time()
        self._counters = {
            "llm_calls": 0,
            "tool_calls": 0,
            "user_messages": 0,
            "agent_responses": 0,
            "total_llm_latency_ms": 0,
            "total_tool_latency_ms": 0,
        }

    def record(self, event_type: str, **kwargs) -> None:
        """Append an event to the trace."""
        now = time.time()
        self.events.append({
            "timestamp": now,
            "elapsed_ms": round((now - self.session_start) * 1000),
            "type": event_type,
            **kwargs,
        })

    # ── Convenience methods ───────────────────────────────────────────

    def user_message(self, content: str) -> None:
        self._counters["user_messages"] += 1
        self.record("user_message", content=content)

    def llm_call_start(self, model: str, message_count: int) -> float:
        """Record LLM call start. Returns the start time for latency calc."""
        self.record("llm_call_start", model=model, message_count=message_count)
        return time.time()

    def llm_call_end(
        self,
        start_time: float,
        finish_reason: str,
        tool_call_count: int = 0,
        usage: Optional[Dict] = None,
    ) -> None:
        latency_ms = round((time.time() - start_time) * 1000)
        self._counters["llm_calls"] += 1
        self._counters["total_llm_latency_ms"] += latency_ms
        self.record(
            "llm_call_end",
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            tool_call_count=tool_call_count,
            usage=usage,
        )

    def tool_call(
        self,
        name: str,
        args: Dict,
        result: Dict,
        latency_ms: float,
    ) -> None:
        self._counters["tool_calls"] += 1
        self._counters["total_tool_latency_ms"] += latency_ms
        self.record(
            "tool_call",
            tool_name=name,
            args=args,
            result_status=result.get("status") or result.get("error", "unknown"),
            latency_ms=round(latency_ms),
        )

    def approval_event(self, action: str, outcome: str, details: Optional[Dict] = None) -> None:
        self.record("approval", action=action, outcome=outcome, details=details)

    def agent_response(self, content: str) -> None:
        self._counters["agent_responses"] += 1
        self.record("agent_response", content=content[:500])

    def bail(self, reason: str) -> None:
        self.record("bail", reason=reason)

    # ── Summary & export ──────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        """Return a summary of the session."""
        elapsed = time.time() - self.session_start
        return {
            "session_duration_s": round(elapsed, 2),
            "total_events": len(self.events),
            **self._counters,
            "avg_llm_latency_ms": (
                round(self._counters["total_llm_latency_ms"] / max(self._counters["llm_calls"], 1))
            ),
        }

    def save(self, path: str) -> None:
        """Write the full trace to a JSON file."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(
                {
                    "session_start": self.session_start,
                    "summary": self.summary(),
                    "events": self.events,
                },
                f,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
