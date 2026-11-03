"""Conversation state and approval-gate data structures."""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any


@dataclass
class PendingApproval:
    """
    Represents a consequential action awaiting user confirmation.
    Created by propose_action, consumed by book/cancel/reschedule.
    """
    action: str                    # "book" | "cancel" | "reschedule"
    details: Dict[str, Any]        # material facts the user approved
    summary: str = ""              # human-readable one-liner
    approved: bool = False         # flipped by application code, not the LLM


@dataclass
class ConversationState:
    """All mutable state the engine tracks across turns."""

    # Info gathered from user
    service: Optional[str] = None
    area: Optional[str] = None
    date_range: Optional[str] = None
    budget: Optional[int] = None

    # Latest search & selection
    last_search_results: List[Dict] = field(default_factory=list)
    selected_provider_id: Optional[str] = None

    # Current hold and booking
    current_hold: Optional[Dict] = None
    current_booking: Optional[Dict] = None

    # Approval gate
    pending_approval: Optional[PendingApproval] = None

    # Execution counters (reset per user message)
    turn_count: int = 0
    tool_call_count: int = 0
