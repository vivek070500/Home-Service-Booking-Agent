"""
Tests for the mock tool server — validates every behaviour the spec requires.

Run:  cd home-service-agent && python -m pytest tests/ -v
"""

import time
import pytest
from mock_tools import MockConfig, ToolServer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ts():
    """Fresh ToolServer with default config and deterministic seed."""
    return ToolServer(config=MockConfig(seed=42))


@pytest.fixture
def ts_with_clock():
    """ToolServer with a controllable clock (starts at epoch 1_000_000)."""
    fake_now = [1_000_000.0]

    def clock():
        return fake_now[0]

    def advance(seconds):
        fake_now[0] += seconds

    server = ToolServer(config=MockConfig(seed=42), clock=clock)
    return server, advance


# ---------------------------------------------------------------------------
# 1. search_providers
# ---------------------------------------------------------------------------

class TestSearchProviders:

    def test_returns_matching_providers(self, ts):
        result = ts.search_providers("plumbing", "Koramangala")
        assert result["status"] == "ok"
        assert 1 <= result["count"] <= 8
        for p in result["providers"]:
            assert "id" in p
            assert "name" in p
            assert "price" in p
            assert "rating" in p
            assert "available_slots" in p

    def test_service_alias_works(self, ts):
        r1 = ts.search_providers("plumber", "Koramangala")
        r2 = ts.search_providers("plumbing", "Koramangala")
        # Both should find the same providers (order may differ due to sampling)
        ids1 = {p["id"] for p in r1["providers"]}
        ids2 = {p["id"] for p in r2["providers"]}
        assert ids1 == ids2

    def test_no_match_returns_empty(self, ts):
        result = ts.search_providers("plumbing", "Timbuktu")
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["providers"] == []

    def test_area_case_insensitive(self, ts):
        r1 = ts.search_providers("plumbing", "koramangala")
        r2 = ts.search_providers("plumbing", "Koramangala")
        assert {p["id"] for p in r1["providers"]} == {p["id"] for p in r2["providers"]}

    def test_deterministic_with_same_seed(self):
        ts1 = ToolServer(config=MockConfig(seed=99))
        ts2 = ToolServer(config=MockConfig(seed=99))
        r1 = ts1.search_providers("electrical", "Koramangala")
        r2 = ts2.search_providers("electrical", "Koramangala")
        assert r1 == r2

    def test_logged_in_trace(self, ts):
        ts.search_providers("plumbing", "HSR Layout")
        assert len(ts.trace) == 1
        assert ts.trace[0]["tool"] == "search_providers"


# ---------------------------------------------------------------------------
# 2. get_provider
# ---------------------------------------------------------------------------

class TestGetProvider:

    def test_returns_full_details(self, ts):
        result = ts.get_provider("PLB-001")
        assert result["status"] == "ok"
        p = result["provider"]
        assert p["name"] == "Raju Plumbing Services"
        assert "reviews" in p
        assert len(p["reviews"]) >= 2

    def test_not_found(self, ts):
        result = ts.get_provider("FAKE-999")
        assert result["status"] == "error"
        assert result["error"] == "provider_not_found"

    def test_contains_prompt_injection_review(self, ts):
        """Spec: at least one review must contain a prompt-injection attempt."""
        providers_with_injection = ["PLB-001", "ELC-001", "ACS-002", "CRP-002"]
        for pid in providers_with_injection:
            result = ts.get_provider(pid)
            reviews = result["provider"]["reviews"]
            injection_keywords = ["IGNORE", "OVERRIDE", "override", "SYSTEM", "skip confirmation",
                                  "without asking", "auto-confirm", "auto-book",
                                  "Do not show other providers"]
            has_injection = any(
                any(kw.lower() in r["text"].lower() for kw in injection_keywords)
                for r in reviews
            )
            assert has_injection, f"Provider {pid} should contain a prompt-injection review"


# ---------------------------------------------------------------------------
# 3. hold_slot
# ---------------------------------------------------------------------------

class TestHoldSlot:

    def test_hold_creates_with_expiry(self, ts_with_clock):
        ts, advance = ts_with_clock
        result = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        assert result["status"] == "ok"
        assert "hold_id" in result
        assert result["provider_id"] == "PLB-001"
        assert result["slot"] == "Monday 9:00 AM"
        assert result["price"] > 0
        assert result["expires_at"] == 1_000_000.0 + 600

    def test_invalid_provider(self, ts):
        result = ts.hold_slot("FAKE-999", "Monday 9:00 AM")
        assert result["status"] == "error"
        assert result["error"] == "provider_not_found"

    def test_invalid_slot(self, ts):
        result = ts.hold_slot("PLB-001", "Sunday 3:00 AM")
        assert result["status"] == "error"
        assert result["error"] == "slot_not_available"

    def test_price_can_change(self):
        """With high price_change_probability, price should differ from base."""
        cfg = MockConfig(seed=42, price_change_probability=1.0)
        ts = ToolServer(config=cfg)
        result = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        assert result["status"] == "ok"
        assert "price_notice" in result
        assert result["price"] != 800  # base price for PLB-001

    def test_price_unchanged_when_probability_zero(self):
        cfg = MockConfig(seed=42, price_change_probability=0.0)
        ts = ToolServer(config=cfg)
        result = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        assert result["status"] == "ok"
        assert "price_notice" not in result
        assert result["price"] == 800


# ---------------------------------------------------------------------------
# 4. book
# ---------------------------------------------------------------------------

class TestBook:

    def _create_hold(self, ts, provider="PLB-001", slot="Monday 9:00 AM"):
        r = ts.hold_slot(provider, slot)
        assert r["status"] == "ok"
        return r["hold_id"]

    def test_success_path(self):
        """Force 100% success rate to test the happy path."""
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold_id = self._create_hold(ts)
        result = ts.book(hold_id, "idem-001")
        assert result["status"] == "success"
        assert "booking_id" in result
        assert result["price"] > 0

    def test_slot_taken_path(self):
        cfg = MockConfig(seed=42, book_success_rate=0.0, book_slot_taken_rate=1.0)
        ts = ToolServer(config=cfg)
        hold_id = self._create_hold(ts)
        result = ts.book(hold_id, "idem-002")
        assert result["status"] == "error"
        assert result["error"] == "slot_taken"

    def test_timeout_path(self):
        cfg = MockConfig(seed=42, book_success_rate=0.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold_id = self._create_hold(ts)
        result = ts.book(hold_id, "idem-003")
        assert result["status"] == "error"
        assert result["error"] == "timeout"

    def test_hold_expired(self):
        """Book after hold expiry should return hold_expired."""
        fake_now = [1_000_000.0]
        ts = ToolServer(
            config=MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0),
            clock=lambda: fake_now[0],
        )
        hold_id = self._create_hold(ts)
        fake_now[0] += 700  # 700s > 600s hold duration
        result = ts.book(hold_id, "idem-exp")
        assert result["status"] == "error"
        assert result["error"] == "hold_expired"

    def test_idempotency_returns_same_result(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold_id = self._create_hold(ts)
        r1 = ts.book(hold_id, "idem-dup")
        r2 = ts.book(hold_id, "idem-dup")
        assert r2.get("idempotent_replay") is True
        assert r1["booking_id"] == r2["booking_id"]

    def test_hold_not_found(self, ts):
        result = ts.book("HOLD-nonexistent", "idem-x")
        assert result["error"] == "hold_not_found"

    def test_hold_already_used(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold_id = self._create_hold(ts)
        ts.book(hold_id, "idem-first")
        result = ts.book(hold_id, "idem-second")  # different key, same hold
        assert result["error"] == "hold_already_used"


# ---------------------------------------------------------------------------
# 5. get_booking_status
# ---------------------------------------------------------------------------

class TestGetBookingStatus:

    def test_found_by_idempotency_key(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold_r = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        book_r = ts.book(hold_r["hold_id"], "idem-status-1")
        status = ts.get_booking_status(idempotency_key="idem-status-1")
        assert status["status"] == "found"
        assert status["booking"]["booking_id"] == book_r["booking_id"]
        assert status["booking"]["booking_status"] == "confirmed"

    def test_found_by_hold_id(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold_r = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        ts.book(hold_r["hold_id"], "idem-status-2")
        status = ts.get_booking_status(hold_id=hold_r["hold_id"])
        assert status["status"] == "found"

    def test_timeout_but_actually_booked(self):
        """After a timeout, get_booking_status reveals the truth."""
        cfg = MockConfig(
            seed=42,
            book_success_rate=0.0,
            book_slot_taken_rate=0.0,
            timeout_actually_booked_rate=1.0,
        )
        ts = ToolServer(config=cfg)
        hold_r = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        book_r = ts.book(hold_r["hold_id"], "idem-timeout-check")
        assert book_r["error"] == "timeout"

        status = ts.get_booking_status(idempotency_key="idem-timeout-check")
        assert status["status"] == "found"
        assert status["booking"]["booking_status"] == "confirmed"

    def test_timeout_not_actually_booked(self):
        cfg = MockConfig(
            seed=42,
            book_success_rate=0.0,
            book_slot_taken_rate=0.0,
            timeout_actually_booked_rate=0.0,
        )
        ts = ToolServer(config=cfg)
        hold_r = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        book_r = ts.book(hold_r["hold_id"], "idem-timeout-nobook")
        assert book_r["error"] == "timeout"

        status = ts.get_booking_status(idempotency_key="idem-timeout-nobook")
        assert status["status"] == "not_found"

    def test_not_found(self, ts):
        status = ts.get_booking_status(hold_id="HOLD-fake")
        assert status["status"] == "not_found"


# ---------------------------------------------------------------------------
# 6. cancel
# ---------------------------------------------------------------------------

class TestCancel:

    def _book_one(self, ts):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts_inner = ToolServer(config=cfg)
        hold_r = ts_inner.hold_slot("PLB-001", "Monday 9:00 AM")
        book_r = ts_inner.book(hold_r["hold_id"], "idem-cancel-test")
        return ts_inner, book_r["booking_id"]

    def test_first_call_fails_with_503(self):
        ts, booking_id = self._book_one(None)
        result = ts.cancel(booking_id, "cancel-idem-1")
        assert result["status"] == "error"
        assert result["http_status"] == 503

    def test_second_call_succeeds(self):
        ts, booking_id = self._book_one(None)
        ts.cancel(booking_id, "cancel-idem-1")      # first: 503
        result = ts.cancel(booking_id, "cancel-idem-1")  # second: ok
        assert result["status"] == "ok"
        assert ts.bookings[booking_id]["status"] == "cancelled"

    def test_already_cancelled(self):
        ts, booking_id = self._book_one(None)
        ts.cancel(booking_id, "c-1")
        ts.cancel(booking_id, "c-1")  # succeeds
        result = ts.cancel(booking_id, "c-1")  # third call
        assert result["status"] == "ok"
        assert "already cancelled" in result["message"]

    def test_booking_not_found(self, ts):
        result = ts.cancel("BKG-nonexistent", "c-x")
        assert result["error"] == "booking_not_found"


# ---------------------------------------------------------------------------
# 7. reschedule
# ---------------------------------------------------------------------------

class TestReschedule:

    def _book_one(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        hold_r = ts.hold_slot("PLB-002", "Monday 10:00 AM")
        book_r = ts.book(hold_r["hold_id"], "idem-resched")
        return ts, book_r["booking_id"]

    def test_successful_reschedule(self):
        ts, booking_id = self._book_one()
        result = ts.reschedule(booking_id, "Tuesday 9:00 AM", "resched-idem-1")
        assert result["status"] == "ok"
        assert result["new_slot"] == "Tuesday 9:00 AM"
        assert ts.bookings[booking_id]["slot"] == "Tuesday 9:00 AM"

    def test_slot_conflict(self):
        ts, booking_id = self._book_one()
        result = ts.reschedule(booking_id, "Saturday 2:00 PM", "resched-idem-2")
        assert result["status"] == "error"
        assert result["error"] == "slot_conflict"
        assert "suggested_slots" in result
        # Original slot should be unchanged
        assert ts.bookings[booking_id]["slot"] == "Monday 10:00 AM"

    def test_booking_not_found(self, ts):
        result = ts.reschedule("BKG-fake", "Monday 9:00 AM", "r-x")
        assert result["error"] == "booking_not_found"

    def test_cancelled_booking_cannot_reschedule(self):
        ts, booking_id = self._book_one()
        ts.cancel(booking_id, "c-1")
        ts.cancel(booking_id, "c-1")  # succeed
        result = ts.reschedule(booking_id, "Tuesday 9:00 AM", "r-y")
        assert result["error"] == "booking_not_active"


# ---------------------------------------------------------------------------
# 8. list_bookings
# ---------------------------------------------------------------------------

class TestListBookings:

    def test_empty_for_new_user(self, ts):
        result = ts.list_bookings("user-nobody")
        assert result["status"] == "ok"
        assert result["count"] == 0

    def test_returns_user_bookings(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)

        h1 = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        ts.book(h1["hold_id"], "list-idem-1", user_id="user-A")

        h2 = ts.hold_slot("ELC-001", "Monday 10:00 AM")
        ts.book(h2["hold_id"], "list-idem-2", user_id="user-A")

        result = ts.list_bookings("user-A")
        assert result["count"] == 2
        services = {b["service"] for b in result["bookings"]}
        assert "plumbing" in services
        assert "electrical" in services

    def test_different_users_isolated(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)

        h1 = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        ts.book(h1["hold_id"], "iso-idem-1", user_id="user-X")

        h2 = ts.hold_slot("ELC-001", "Monday 10:00 AM")
        ts.book(h2["hold_id"], "iso-idem-2", user_id="user-Y")

        assert ts.list_bookings("user-X")["count"] == 1
        assert ts.list_bookings("user-Y")["count"] == 1


# ---------------------------------------------------------------------------
# Cross-cutting: observability trace
# ---------------------------------------------------------------------------

class TestTrace:

    def test_all_calls_are_traced(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)
        ts.search_providers("plumbing", "Koramangala")
        ts.get_provider("PLB-001")
        hold_r = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        ts.book(hold_r["hold_id"], "trace-idem")
        ts.get_booking_status(idempotency_key="trace-idem")
        ts.list_bookings("default_user")

        assert len(ts.trace) == 6
        tools_called = [t["tool"] for t in ts.trace]
        assert tools_called == [
            "search_providers", "get_provider", "hold_slot",
            "book", "get_booking_status", "list_bookings",
        ]

    def test_trace_reset(self, ts):
        ts.search_providers("plumbing", "Koramangala")
        assert len(ts.trace) == 1
        ts.reset()
        assert len(ts.trace) == 0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:

    def test_same_seed_same_outcomes(self):
        """Entire call sequence produces identical results with same seed."""
        results = []
        for _ in range(2):
            ts = ToolServer(config=MockConfig(seed=123))
            r1 = ts.search_providers("plumbing", "Koramangala")
            r2 = ts.hold_slot("PLB-001", "Monday 9:00 AM")
            r3 = ts.book(r2["hold_id"], "det-idem")
            results.append((r1, r2["price"], r3["status"]))

        assert results[0] == results[1]


# ---------------------------------------------------------------------------
# Slot availability management
# ---------------------------------------------------------------------------

class TestSlotAvailability:

    def test_hold_removes_slot(self):
        ts = ToolServer(config=MockConfig(seed=42))
        assert "Monday 9:00 AM" in ts._provider_index["PLB-001"]["available_slots"]

        ts.hold_slot("PLB-001", "Monday 9:00 AM")
        assert "Monday 9:00 AM" not in ts._provider_index["PLB-001"]["available_slots"]

    def test_hold_expired_restores_slot(self):
        fake_now = [1_000_000.0]
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg, clock=lambda: fake_now[0])

        hold_r = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        assert "Monday 9:00 AM" not in ts._provider_index["PLB-001"]["available_slots"]

        # Expire the hold
        fake_now[0] += 700
        ts.book(hold_r["hold_id"], "exp-idem")

        assert "Monday 9:00 AM" in ts._provider_index["PLB-001"]["available_slots"]

    def test_cancel_restores_slot(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)

        hold_r = ts.hold_slot("PLB-001", "Monday 9:00 AM")
        book_r = ts.book(hold_r["hold_id"], "cancel-slot-idem")
        bid = book_r["booking_id"]

        assert "Monday 9:00 AM" not in ts._provider_index["PLB-001"]["available_slots"]

        # Cancel (first fails, second succeeds)
        ts.cancel(bid, "c-idem")
        ts.cancel(bid, "c-idem")

        assert "Monday 9:00 AM" in ts._provider_index["PLB-001"]["available_slots"]

    def test_reschedule_swaps_slots(self):
        cfg = MockConfig(seed=42, book_success_rate=1.0, book_slot_taken_rate=0.0)
        ts = ToolServer(config=cfg)

        hold_r = ts.hold_slot("PLB-002", "Monday 10:00 AM")
        book_r = ts.book(hold_r["hold_id"], "resched-slot-idem")
        bid = book_r["booking_id"]

        assert "Monday 10:00 AM" not in ts._provider_index["PLB-002"]["available_slots"]
        assert "Tuesday 9:00 AM" in ts._provider_index["PLB-002"]["available_slots"]

        ts.reschedule(bid, "Tuesday 9:00 AM", "r-idem")

        # Old slot freed, new slot taken
        assert "Monday 10:00 AM" in ts._provider_index["PLB-002"]["available_slots"]
        assert "Tuesday 9:00 AM" not in ts._provider_index["PLB-002"]["available_slots"]
