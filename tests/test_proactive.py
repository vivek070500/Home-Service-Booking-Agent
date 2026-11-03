"""
Tests for proactive follow-up — provider cancellation detection and handling.

Run:  python -m pytest tests/test_proactive.py -v
"""

import pytest
from mock_tools import MockConfig, ToolServer
from agent.engine import Agent
from agent.memory import MemoryStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_booked_agent(tmp_path=None):
    """Create an agent with a confirmed booking, ready for cancellation testing."""
    cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
    ts = ToolServer(config=cfg)
    mem = MemoryStore(path=str(tmp_path / "mem.json")) if tmp_path else None
    agent = Agent(tool_server=ts, api_key="fake", memory=mem)

    # Book PLB-001 for Monday 9:00 AM
    hold = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})
    agent._dispatch("propose_action", {
        "action": "book",
        "details": {"hold_id": hold["hold_id"], "price": hold["price"]},
        "summary": "Book Raju Plumbing for Monday 9 AM",
    })
    agent._process_approval("Yes")
    book = agent._dispatch("book", {"hold_id": hold["hold_id"]})
    assert book["status"] == "success"

    return agent, ts, book["booking_id"]


# ---------------------------------------------------------------------------
# ToolServer: simulate_provider_cancellation
# ---------------------------------------------------------------------------

class TestSimulateProviderCancellation:

    def test_cancels_confirmed_booking(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        book = ts.book(hold["hold_id"], "idem-1")
        bid = book["booking_id"]

        result = ts.simulate_provider_cancellation(bid)
        assert result["status"] == "ok"
        assert ts.bookings[bid]["status"] == "provider_cancelled"

    def test_restores_slot_on_cancellation(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        book = ts.book(hold["hold_id"], "idem-1")
        bid = book["booking_id"]

        assert "Monday 9:00 AM" not in ts._provider_index["PLB-001"]["available_slots"]
        ts.simulate_provider_cancellation(bid)
        assert "Monday 9:00 AM" in ts._provider_index["PLB-001"]["available_slots"]

    def test_queues_event_for_user(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        book = ts.book(hold["hold_id"], "idem-1", user_id="user-1")
        bid = book["booking_id"]

        ts.simulate_provider_cancellation(bid)

        events = ts.check_events("user-1")
        assert events["count"] == 1
        evt = events["events"][0]
        assert evt["type"] == "provider_cancellation"
        assert evt["booking_id"] == bid
        assert evt["provider_name"] == "Raju Plumbing Services"

    def test_events_cleared_after_check(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        book = ts.book(hold["hold_id"], "idem-1", user_id="user-1")

        ts.simulate_provider_cancellation(book["booking_id"])
        ts.check_events("user-1")  # first call gets events

        events2 = ts.check_events("user-1")  # second call → empty
        assert events2["count"] == 0

    def test_cannot_cancel_nonexistent_booking(self):
        ts = ToolServer(config=MockConfig(seed=42))
        result = ts.simulate_provider_cancellation("BKG-fake")
        assert result["error"] == "booking_not_found"

    def test_cannot_cancel_already_cancelled_booking(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        book = ts.book(hold["hold_id"], "idem-1")

        # Cancel by user first
        ts.cancel(book["booking_id"], "c-1")
        ts.cancel(book["booking_id"], "c-1")  # succeeds on retry

        result = ts.simulate_provider_cancellation(book["booking_id"])
        assert result["error"] == "booking_not_active"


# ---------------------------------------------------------------------------
# Agent: event injection into conversation
# ---------------------------------------------------------------------------

class TestEventInjection:

    def test_no_events_no_injection(self):
        agent, ts, bid = make_booked_agent()
        initial_msg_count = len(agent.messages)
        agent._inject_pending_events()
        assert len(agent.messages) == initial_msg_count

    def test_cancellation_injects_system_message(self):
        agent, ts, bid = make_booked_agent()
        ts.simulate_provider_cancellation(bid)

        initial_msg_count = len(agent.messages)
        agent._inject_pending_events()

        assert len(agent.messages) == initial_msg_count + 1
        injected = agent.messages[-1]
        assert injected["role"] == "system"
        assert "PROVIDER CANCELLATION" in injected["content"]
        assert bid in injected["content"]
        assert "Raju Plumbing Services" in injected["content"]

    def test_events_consumed_after_injection(self):
        agent, ts, bid = make_booked_agent()
        ts.simulate_provider_cancellation(bid)

        agent._inject_pending_events()  # consumes events
        msg_count_after = len(agent.messages)

        agent._inject_pending_events()  # second call → no new events
        assert len(agent.messages) == msg_count_after

    def test_multiple_cancellations_all_injected(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        agent = Agent(tool_server=ts, api_key="fake")

        # Book two providers
        h1 = agent._dispatch("hold_slot", {"provider_id": "PLB-001", "slot": "Monday 9:00 AM"})
        agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": h1["hold_id"], "price": h1["price"]},
            "summary": "Book 1",
        })
        agent._process_approval("Yes")
        b1 = agent._dispatch("book", {"hold_id": h1["hold_id"]})

        # Clear approval for second booking
        agent.state.pending_approval = None

        h2 = agent._dispatch("hold_slot", {"provider_id": "ELC-001", "slot": "Monday 10:00 AM"})
        agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": h2["hold_id"], "price": h2["price"]},
            "summary": "Book 2",
        })
        agent._process_approval("Yes")
        b2 = agent._dispatch("book", {"hold_id": h2["hold_id"]})

        # Both providers cancel
        ts.simulate_provider_cancellation(b1["booking_id"])
        ts.simulate_provider_cancellation(b2["booking_id"])

        initial = len(agent.messages)
        agent._inject_pending_events()

        # One system message with both cancellations
        assert len(agent.messages) == initial + 1
        content = agent.messages[-1]["content"]
        assert b1["booking_id"] in content
        assert b2["booking_id"] in content

    def test_tracer_records_proactive_event(self):
        agent, ts, bid = make_booked_agent()
        ts.simulate_provider_cancellation(bid)
        agent._inject_pending_events()

        event_types = [e["type"] for e in agent.tracer.events]
        assert "proactive_events_detected" in event_types


# ---------------------------------------------------------------------------
# Booking status reflects provider cancellation
# ---------------------------------------------------------------------------

class TestBookingStatusAfterProviderCancel:

    def test_list_bookings_shows_provider_cancelled(self):
        agent, ts, bid = make_booked_agent()
        ts.simulate_provider_cancellation(bid)

        bookings = agent._dispatch("list_bookings", {})
        assert bookings["count"] == 1
        assert bookings["bookings"][0]["status"] == "provider_cancelled"

    def test_get_booking_status_shows_provider_cancelled(self):
        agent, ts, bid = make_booked_agent()
        # Get the hold_id for lookup
        hold_id = ts.bookings[bid]["hold_id"]

        ts.simulate_provider_cancellation(bid)

        status = agent._dispatch("get_booking_status", {"hold_id": hold_id})
        assert status["status"] == "found"
        assert status["booking"]["booking_status"] == "provider_cancelled"


# ---------------------------------------------------------------------------
# Full replacement flow (no LLM, via dispatch)
# ---------------------------------------------------------------------------

class TestReplacementFlow:

    def test_full_replacement_after_cancellation(self):
        """
        Simulate: book → provider cancels → search replacement → hold → propose → confirm → rebook.
        """
        agent, ts, bid = make_booked_agent()

        # Provider cancels
        ts.simulate_provider_cancellation(bid)

        # Agent detects (this happens at start of chat())
        agent._inject_pending_events()
        assert any("PROVIDER CANCELLATION" in m.get("content", "") for m in agent.messages if m["role"] == "system")

        # Agent searches for replacement (same service, same area)
        search = agent._dispatch("search_providers", {"service": "plumbing", "area": "Koramangala"})
        assert search["count"] >= 1

        # Pick a new provider and hold
        new_provider = search["providers"][0]
        new_slot = new_provider["available_slots"][0]
        hold = agent._dispatch("hold_slot", {"provider_id": new_provider["id"], "slot": new_slot})
        assert hold["status"] == "ok"

        # Propose replacement
        propose = agent._dispatch("propose_action", {
            "action": "book",
            "details": {"hold_id": hold["hold_id"], "price": hold["price"],
                        "provider_name": new_provider["name"], "slot": new_slot},
            "summary": f"Book {new_provider['name']} for {new_slot} at ₹{hold['price']}",
        })
        assert propose["status"] == "pending_user_confirmation"

        # User confirms
        agent._process_approval("Yes, book the replacement")

        # Rebook
        book = agent._dispatch("book", {"hold_id": hold["hold_id"]})
        assert book["status"] == "success"

        # Now user has 2 bookings: 1 cancelled, 1 confirmed
        bookings = agent._dispatch("list_bookings", {})
        assert bookings["count"] == 2
        statuses = {b["status"] for b in bookings["bookings"]}
        assert statuses == {"provider_cancelled", "confirmed"}

    def test_replacement_still_requires_approval(self):
        """Even after a provider cancellation, agent can't auto-rebook."""
        agent, ts, bid = make_booked_agent()
        ts.simulate_provider_cancellation(bid)
        agent._inject_pending_events()

        # Try to book directly without propose → blocked
        hold = agent._dispatch("hold_slot", {"provider_id": "PLB-002", "slot": "Monday 10:00 AM"})
        result = agent._dispatch("book", {"hold_id": hold["hold_id"]})
        assert result["error"] == "approval_required"
