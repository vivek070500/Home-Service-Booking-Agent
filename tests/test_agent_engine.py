"""
Tests for agent engine logic — approval gate, idempotency, bounded execution.

These tests verify APPLICATION-CODE enforcement without calling the real LLM.
They directly invoke the agent's internal methods.

Run:  python -m pytest tests/test_agent_engine.py -v
"""

import pytest
from unittest.mock import MagicMock

from agent.state import ConversationState, PendingApproval
from agent.engine import Agent
from mock_tools import MockConfig, ToolServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_agent(seed=42, **config_overrides):
    """Create an Agent with a fake API key (we won't call the LLM in these tests)."""
    cfg = MockConfig(seed=seed, book_success_rate=1.0, book_slot_taken_rate=0.0, **config_overrides)
    ts = ToolServer(config=cfg)
    agent = Agent(tool_server=ts, api_key="fake-key-for-unit-tests", model="gpt-4o-mini")
    return agent


def hold_and_propose(agent, provider="PLB-001", slot="Monday 9:00 AM"):
    """Helper: hold a slot and set up a pending book proposal."""
    hold_result = agent._dispatch("hold_slot", {"provider_id": provider, "slot": slot})
    assert hold_result["status"] == "ok"
    hold_id = hold_result["hold_id"]

    propose_result = agent._dispatch("propose_action", {
        "action": "book",
        "details": {"hold_id": hold_id, "provider_name": "Test Provider", "slot": slot, "price": hold_result["price"]},
        "summary": f"Book Test Provider for {slot} at ₹{hold_result['price']}",
    })
    assert propose_result["status"] == "pending_user_confirmation"
    return hold_id


# ---------------------------------------------------------------------------
# Approval Gate
# ---------------------------------------------------------------------------

class TestApprovalGate:

    def test_book_blocked_without_propose(self):
        agent = make_agent()
        hold_r = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})
        result = agent._dispatch("book", {"hold_id": hold_r["hold_id"]})
        assert result["error"] == "approval_required"

    def test_book_blocked_before_user_confirms(self):
        agent = make_agent()
        hold_id = hold_and_propose(agent)
        # User hasn't said "yes" yet → approval still pending
        result = agent._dispatch("book", {"hold_id": hold_id})
        assert result["error"] == "approval_pending"

    def test_book_succeeds_after_user_confirms(self):
        agent = make_agent()
        hold_id = hold_and_propose(agent)
        # Simulate user saying "yes"
        agent._process_approval("Yes, go ahead")
        assert agent.state.pending_approval.approved is True
        result = agent._dispatch("book", {"hold_id": hold_id})
        assert result["status"] == "success"

    def test_book_blocked_after_user_rejects(self):
        agent = make_agent()
        hold_id = hold_and_propose(agent)
        agent._process_approval("No, I changed my mind")
        assert agent.state.pending_approval is None
        result = agent._dispatch("book", {"hold_id": hold_id})
        assert result["error"] == "approval_required"

    def test_modification_clears_approval(self):
        agent = make_agent()
        hold_id = hold_and_propose(agent)
        agent._process_approval("Yes but change the time to 11 AM instead")
        assert agent.state.pending_approval is None

    def test_cancel_blocked_without_approval(self):
        agent = make_agent()
        result = agent._dispatch("cancel", {"booking_id": "BKG-fake"})
        assert result["error"] == "approval_required"

    def test_reschedule_blocked_without_approval(self):
        agent = make_agent()
        result = agent._dispatch("reschedule", {"booking_id": "BKG-fake", "slot": "Tuesday 9:00 AM"})
        assert result["error"] == "approval_required"

    def test_action_mismatch_blocked(self):
        """User approved 'cancel' but agent tries 'book'."""
        agent = make_agent()
        agent.state.pending_approval = PendingApproval(
            action="cancel",
            details={"booking_id": "BKG-123"},
            approved=True,
        )
        hold_r = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})
        result = agent._dispatch("book", {"hold_id": hold_r["hold_id"]})
        assert result["error"] == "approval_mismatch"

    def test_hold_id_swap_blocked(self):
        """User approved booking for HOLD-A but agent tries HOLD-B."""
        agent = make_agent()
        hold_and_propose(agent)  # sets pending approval with HOLD-A
        agent._process_approval("Yes")

        # Try to book a DIFFERENT hold
        result = agent._dispatch("book", {"hold_id": "HOLD-different-one"})
        assert result["error"] == "details_changed"

    def test_booking_id_swap_blocked_for_cancel(self):
        """User approved cancel for BKG-A but agent tries BKG-B."""
        agent = make_agent()
        agent._dispatch("propose_action", {
            "action": "cancel",
            "details": {"booking_id": "BKG-real"},
            "summary": "Cancel booking BKG-real",
        })
        agent._process_approval("Yes")
        result = agent._dispatch("cancel", {"booking_id": "BKG-different"})
        assert result["error"] == "details_changed"


# ---------------------------------------------------------------------------
# Confirmation Detection
# ---------------------------------------------------------------------------

class TestConfirmationDetection:

    @pytest.mark.parametrize("msg", [
        "Yes", "yes", "YES",
        "Yeah", "yep", "sure", "ok", "okay",
        "Confirm", "Go ahead", "Proceed",
        "Book it", "Do it", "Yes please",
        "Absolutely", "Definitely",
        "Haan",  # Hindi
    ])
    def test_confirmations_recognized(self, msg):
        agent = make_agent()
        hold_and_propose(agent)
        agent._process_approval(msg)
        assert agent.state.pending_approval.approved is True

    @pytest.mark.parametrize("msg", [
        "No", "nope", "nah", "don't",
        "Cancel that", "Stop", "Nevermind",
        "Wait", "Hold on", "Not now",
    ])
    def test_rejections_recognized(self, msg):
        agent = make_agent()
        hold_and_propose(agent)
        agent._process_approval(msg)
        assert agent.state.pending_approval is None

    @pytest.mark.parametrize("msg", [
        "Yes but change the price",
        "Sure, however I want a different slot",
        "Ok actually make it Tuesday instead",
    ])
    def test_modification_clears(self, msg):
        agent = make_agent()
        hold_and_propose(agent)
        agent._process_approval(msg)
        assert agent.state.pending_approval is None

    def test_ambiguous_keeps_pending(self):
        """A question should keep the proposal pending, not approve or reject."""
        agent = make_agent()
        hold_and_propose(agent)
        agent._process_approval("What's the provider's rating?")
        assert agent.state.pending_approval is not None
        assert agent.state.pending_approval.approved is False


# ---------------------------------------------------------------------------
# Price Integrity (code-enforced)
# ---------------------------------------------------------------------------

class TestPriceIntegrity:

    def test_propose_with_wrong_price_rejected(self):
        """LLM tries to propose a lower price than the hold actually quoted."""
        agent = make_agent()
        hold = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})
        actual_price = hold["price"]

        result = agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": hold["hold_id"], "price": actual_price - 200},
            "summary": "Book at wrong price",
        })
        assert result["error"] == "price_mismatch"

    def test_propose_with_correct_price_accepted(self):
        agent = make_agent()
        hold = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})

        result = agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": hold["hold_id"], "price": hold["price"]},
            "summary": "Book at correct price",
        })
        assert result["status"] == "pending_user_confirmation"

    def test_propose_without_hold_rejected(self):
        """LLM tries to propose a booking without calling hold_slot first."""
        agent = make_agent()
        result = agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": "HOLD-fake", "price": 800},
            "summary": "Book without holding",
        })
        assert result["error"] == "no_active_hold"

    def test_propose_with_wrong_hold_id_rejected(self):
        """LLM holds one slot but proposes a different hold_id."""
        agent = make_agent()
        hold = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})

        result = agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": "HOLD-different", "price": hold["price"]},
            "summary": "Book wrong hold",
        })
        assert result["error"] == "hold_id_mismatch"

    def test_propose_auto_fills_price_from_hold(self):
        """If LLM omits price, engine fills it from the hold."""
        agent = make_agent()
        hold = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})

        result = agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": hold["hold_id"]},
            "summary": "Book",
        })
        assert result["status"] == "pending_user_confirmation"
        assert result["details"]["price"] == hold["price"]

    def test_price_change_forces_correct_proposal(self):
        """When hold_slot changes the price, only the new price is accepted."""
        agent = make_agent(price_change_probability=1.0)
        hold = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})
        assert "price_notice" in hold  # price changed

        # Try proposing the OLD base price (800) → rejected
        result = agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": hold["hold_id"], "price": 800},
            "summary": "Book at old price",
        })
        assert result["error"] == "price_mismatch"

        # Propose the ACTUAL hold price → accepted
        result = agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": hold["hold_id"], "price": hold["price"]},
            "summary": "Book at new price",
        })
        assert result["status"] == "pending_user_confirmation"


# ---------------------------------------------------------------------------
# Idempotency Keys
# ---------------------------------------------------------------------------

class TestIdempotencyKeys:

    def test_same_hold_same_key(self):
        agent = make_agent()
        k1 = agent._idem_key("book", "HOLD-abc")
        k2 = agent._idem_key("book", "HOLD-abc")
        assert k1 == k2

    def test_different_holds_different_keys(self):
        agent = make_agent()
        k1 = agent._idem_key("book", "HOLD-abc")
        k2 = agent._idem_key("book", "HOLD-xyz")
        assert k1 != k2

    def test_cancel_retry_uses_same_key(self):
        agent = make_agent()
        k1 = agent._idem_key("cancel", "BKG-001")
        k2 = agent._idem_key("cancel", "BKG-001")
        assert k1 == k2

    def test_reschedule_different_slots_different_keys(self):
        agent = make_agent()
        k1 = agent._idem_key("reschedule", "BKG-001", "Monday 9:00 AM")
        k2 = agent._idem_key("reschedule", "BKG-001", "Tuesday 10:00 AM")
        assert k1 != k2


# ---------------------------------------------------------------------------
# Non-consequential tools work without approval
# ---------------------------------------------------------------------------

class TestNonConsequentialTools:

    def test_search_needs_no_approval(self):
        agent = make_agent()
        result = agent._dispatch("search_providers", {"service": "plumbing", "area": "Koramangala"})
        assert result["status"] == "ok"

    def test_get_provider_needs_no_approval(self):
        agent = make_agent()
        result = agent._dispatch("get_provider", {"provider_id": "PLB-001"})
        assert result["status"] == "ok"

    def test_hold_slot_needs_no_approval(self):
        agent = make_agent()
        result = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})
        assert result["status"] == "ok"

    def test_list_bookings_needs_no_approval(self):
        agent = make_agent()
        result = agent._dispatch("list_bookings", {})
        assert result["status"] == "ok"

    def test_get_booking_status_needs_no_approval(self):
        agent = make_agent()
        result = agent._dispatch("get_booking_status", {"hold_id": "HOLD-fake"})
        assert result["status"] == "not_found"  # not found is fine, no approval error


# ---------------------------------------------------------------------------
# End-to-end tool flow (no LLM, but full dispatch chain)
# ---------------------------------------------------------------------------

class TestFullFlowNoLLM:

    def test_search_hold_propose_confirm_book(self):
        """Complete booking flow via _dispatch, simulating what the LLM would do."""
        agent = make_agent()

        # 1. Search
        search = agent._dispatch("search_providers", {"service": "plumbing", "area": "Koramangala"})
        assert search["status"] == "ok"
        assert search["count"] >= 1
        pid = search["providers"][0]["id"]

        # 2. Hold
        slots = search["providers"][0]["available_slots"]
        hold = agent._dispatch("hold_slot", {"provider_id": pid, "slot": slots[0]})
        assert hold["status"] == "ok"

        # 3. Propose
        propose = agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": hold["hold_id"], "provider_name": "Test", "slot": slots[0], "price": hold["price"]},
            "summary": f"Book for {slots[0]} at ₹{hold['price']}",
        })
        assert propose["status"] == "pending_user_confirmation"

        # 4. User confirms
        agent._process_approval("Yes")

        # 5. Book
        book = agent._dispatch("book", {"hold_id": hold["hold_id"]})
        assert book["status"] == "success"
        assert "booking_id" in book

        # 6. List bookings
        bookings = agent._dispatch("list_bookings", {})
        assert bookings["count"] == 1

    def test_cancel_flow_with_retry(self):
        """Cancel with 503 on first try, success on retry."""
        agent = make_agent()

        # Book first
        hold = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})
        agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": hold["hold_id"], "price": hold["price"]},
            "summary": "Book",
        })
        agent._process_approval("Yes")
        book = agent._dispatch("book", {"hold_id": hold["hold_id"]})
        bid = book["booking_id"]

        # Propose cancel
        agent._dispatch("propose_action", {
            "action": "cancel",
            "details": {"booking_id": bid},
            "summary": "Cancel booking",
        })
        agent._process_approval("Yes")

        # First cancel → 503
        r1 = agent._dispatch("cancel", {"booking_id": bid})
        assert r1["http_status"] == 503

        # Retry → success (same idem key used automatically)
        r2 = agent._dispatch("cancel", {"booking_id": bid})
        assert r2["status"] == "ok"
