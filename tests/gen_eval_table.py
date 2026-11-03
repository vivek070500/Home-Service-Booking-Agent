#!/usr/bin/env python3
"""
Generate the eval table from pytest output.

Usage:
    python -m pytest tests/test_eval_scenarios.py -v --tb=line 2>&1 | python tests/gen_eval_table.py

Or run directly:
    python tests/gen_eval_table.py
"""

import re
import subprocess
import sys


SCENARIO_DESCRIPTIONS = {
    "TestS01_BasicSearchAndBook": ("Happy path: search & book", "Booking confirmed, appears in list_bookings"),
    "TestS02_GetProviderDetails": ("View provider details + reviews", "Reviews returned, rating correct"),
    "TestS03_CancelBooking": ("Cancel with 503 retry", "First call 503, retry succeeds, status=cancelled"),
    "TestS04_RescheduleBooking": ("Reschedule to new slot", "Slot updated successfully"),
    "TestS05_ListBookingsMultiple": ("Multiple bookings across services", "Count=2, both present"),
    "TestS06_NoBookingWithoutApproval": ("Book blocked without approval", "error=approval_required"),
    "TestS07_NoCancelWithoutApproval": ("Cancel blocked without approval", "error=approval_required"),
    "TestS08_ApprovalClearedOnRejection": ("User rejects → approval cleared", "pending_approval=None, book blocked"),
    "TestS09_TimeoutThenVerify": ("Timeout (actually booked) → check status", "get_booking_status returns confirmed"),
    "TestS10_TimeoutNotBooked": ("Timeout (not booked) → check status", "get_booking_status returns not_found"),
    "TestS11_SlotTakenRecovery": ("Slot taken → can search alternatives", "error=slot_taken, search still works"),
    "TestS12_PromptInjectionInReview": ("Injection in review doesn't bypass approval", "Approval gate still enforced"),
    "TestS13_PromptInjectionMultipleProviders": ("Multiple injected providers, approval holds", "All 4 providers checked, gate intact"),
    "TestS14_InjectionCannotAlterApprovalState": ("Tool output can't set approved=True", "approved remains False after get_provider"),
    "TestS15_PriceChangeDetected": ("Hold price differs from search", "price_notice present, price ≠ base"),
    "TestS16_WrongPriceProposalRejected": ("Propose old price after change", "error=price_mismatch"),
    "TestS17_CorrectPriceAfterChangeAccepted": ("Propose actual hold price → book", "Proposal accepted, booking succeeds"),
    "TestS18_MemoryPersistsAcrossSessions": ("Preference survives new session", "System prompt contains stored area"),
    "TestS19_MemoryForgetWorks": ("Forget removes preference", "list_preferences count=0 in new session"),
    "TestS20_ProviderCancellationDetected": ("Provider cancels → event injected", "System message with PROVIDER CANCELLATION"),
    "TestS21_ReplacementRequiresFreshApproval": ("Rebooking after cancel needs approval", "Direct book blocked, full flow works"),
    "TestS22_HoldExpiredCannotBook": ("Book after hold expiry", "error=hold_expired"),
    "TestS23_RescheduleSlotConflict": ("Reschedule to conflict slot", "error=slot_conflict, alternatives offered"),
    "TestS24_IdempotentBookRetry": ("Idempotent book retry", "Only 1 booking in list (no duplicate)"),
    "TestS25_CancelRestoresSlot": ("Cancel frees slot for re-booking", "Slot available again, re-book succeeds"),
}


def run_tests():
    """Run pytest and capture output."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_eval_scenarios.py", "-v", "--tb=line"],
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def parse_results(output):
    """Parse pytest -v output into (class_name, passed/failed) pairs."""
    results = {}
    for line in output.splitlines():
        match = re.search(r"(TestS\d+_\w+)::test\s+(PASSED|FAILED)", line)
        if not match:
            match = re.search(r"(TestS\d+_\w+)::test (PASSED|FAILED)", line)
        if not match:
            match = re.search(r"(TestS\d+_\w+)\S*\s+(PASSED|FAILED)", line)
        if match:
            results[match.group(1)] = match.group(2)
    return results


def generate_table(results):
    """Print a formatted eval table."""
    header = f"{'#':<4} {'Scenario':<45} {'Expected':<50} {'Result':<8} {'Notes'}"
    sep = "─" * 140

    print()
    print("EVALUATION TABLE — Auto-generated from test run")
    print(sep)
    print(header)
    print(sep)

    passed = 0
    failed = 0

    for i, (class_name, (scenario, expected)) in enumerate(SCENARIO_DESCRIPTIONS.items(), 1):
        result = results.get(class_name, "NOT RUN")
        status = "✅ PASS" if result == "PASSED" else "❌ FAIL" if result == "FAILED" else "⚠️  N/A"
        notes = "" if result == "PASSED" else "CHECK FAILURE" if result == "FAILED" else "Test not found"

        if result == "PASSED":
            passed += 1
        elif result == "FAILED":
            failed += 1

        print(f"{i:<4} {scenario:<45} {expected:<50} {status:<8} {notes}")

    print(sep)
    print(f"Total: {passed + failed} | Passed: {passed} | Failed: {failed}")
    print()


def main():
    if not sys.stdin.isatty():
        # Piped from pytest
        output = sys.stdin.read()
    else:
        # Run pytest ourselves
        output = run_tests()

    results = parse_results(output)
    generate_table(results)


if __name__ == "__main__":
    main()
