#!/usr/bin/env python3
"""Generate a trace for timeout-after-success scenario."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_tools import MockConfig, ToolServer
from agent.graph_engine import GraphAgent
from agent.memory import MemoryStore
from agent.tracer import Tracer

cfg = MockConfig(
    seed=42,
    book_success_rate=0.0,        # booking always "times out"
    book_slot_taken_rate=0.0,
    timeout_actually_booked_rate=1.0,  # but it actually went through
)
ts = ToolServer(config=cfg)
tracer = Tracer()

agent = GraphAgent(
    tool_server=ts,
    model="qwen2.5:14b",
    memory=None,
    tracer=tracer,
)

turns = [
    "I need a plumber in Koramangala this weekend, budget 1000",
    "I want Raju Plumbing Services, Monday 11 AM slot",
    "Yes, confirm the booking",
    "Yes",
]

for msg in turns:
    print(f"\n>>> User: {msg}")
    resp = agent.chat(msg)
    print(f"<<< Agent: {resp}")

tracer.save("data/traces/timeout_success_trace.json")
print(f"\n✅ Trace saved to data/traces/timeout_success_trace.json")
print(f"   {tracer.summary()}")
