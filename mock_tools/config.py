from dataclasses import dataclass, field
from typing import List


@dataclass
class MockConfig:
    """All knobs for the mock tool server. Override any field to test specific scenarios."""

    seed: int = 42
    hold_duration_seconds: int = 600  # 10 minutes

    # hold_slot: probability the quoted price differs from search price
    price_change_probability: float = 0.20
    price_change_min_multiplier: float = 0.90
    price_change_max_multiplier: float = 1.30

    # book outcome distribution (must sum to 1.0)
    book_success_rate: float = 0.70
    book_slot_taken_rate: float = 0.15
    # remaining 0.15 is timeout rate (implicit)

    # of the timeouts, fraction that actually created a booking server-side
    timeout_actually_booked_rate: float = 0.50

    # reschedule: slots that will always produce a slot_conflict
    slot_conflict_slots: List[str] = field(default_factory=lambda: [
        "Saturday 2:00 PM",
        "Sunday 10:00 AM",
    ])
