#!/usr/bin/env python3
"""
CLI chat interface for the Home-Service Booking Agent.

Usage:
    python main.py                     # uses gpt-4o-mini (default)
    python main.py --model gpt-4o      # pick a different model
"""

import argparse
import os
import sys
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from mock_tools import MockConfig, ToolServer
from agent import GraphAgent, MemoryStore, Tracer


def main():
    parser = argparse.ArgumentParser(description="Home-Service Booking Agent")
    parser.add_argument("--model", default="qwen2.5:14b", help="Ollama model name")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for mock tools")
    parser.add_argument("--user", default="user-1", help="User ID for memory")
    parser.add_argument("--no-memory", action="store_true", help="Disable persistent memory")
    parser.add_argument("--demo-timeout", action="store_true",
                        help="Demo mode: booking always times out but actually succeeds")
    parser.add_argument("--demo-slot-taken", action="store_true",
                        help="Demo mode: booking always fails with slot taken")
    args = parser.parse_args()

    api_key = os.getenv("GROQ_API_KEY", "ollama")

    # Set up components
    if args.demo_timeout:
        cfg = MockConfig(seed=args.seed, book_success_rate=0.0,
                         book_slot_taken_rate=0.0, timeout_actually_booked_rate=1.0)
    elif args.demo_slot_taken:
        cfg = MockConfig(seed=args.seed, book_success_rate=0.0, book_slot_taken_rate=1.0)
    else:
        cfg = MockConfig(seed=args.seed)
    tool_server = ToolServer(config=cfg)
    memory = None if args.no_memory else MemoryStore(path="data/memory.json")
    tracer = Tracer()

    agent = GraphAgent(
        tool_server=tool_server,
        model=args.model,
        user_id=args.user,
        memory=memory,
        tracer=tracer,
    )

    print("┌──────────────────────────────────────────────┐")
    print("│  🏠  Home-Service Booking Agent               │")
    print("│  Type your request, or 'quit' to exit.       │")
    print("└──────────────────────────────────────────────┘")
    if args.demo_timeout:
        print("  ⚡ Demo mode: bookings will timeout (but succeed on server)")
    elif args.demo_slot_taken:
        print("  ⚡ Demo mode: bookings will fail with slot taken")

    if memory:
        prefs = memory.list_all(args.user)
        if prefs:
            print(f"  📋 Loaded {len(prefs)} stored preference(s) from memory.")
    print()

    while True:
        try:
            user_input = input("\033[1mYou:\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye! 👋")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit", "bye", "q"):
            print("Goodbye! 👋")
            break

        # Simulation command: /cancel <booking_id>
        if user_input.startswith("/cancel "):
            booking_id = user_input.split(" ", 1)[1].strip()
            result = tool_server.simulate_provider_cancellation(booking_id)
            if result["status"] == "ok":
                print(f"\n⚡ Simulated: Provider cancelled booking {booking_id}.")
                print("   (The agent will detect this on your next message.)\n")
            else:
                print(f"\n❌ Could not simulate cancellation: {result.get('error')}\n")
            continue

        print()
        response = agent.chat(user_input)
        print(f"\033[1mAgent:\033[0m {response}")
        print()

    # Save trace on exit
    os.makedirs("data/traces", exist_ok=True)
    trace_file = f"data/traces/trace_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    tracer.save(trace_file)
    print(f"📊 Trace saved to {trace_file}")

    summary = tracer.summary()
    print(f"   Session: {summary['session_duration_s']}s | "
          f"LLM calls: {summary['llm_calls']} | "
          f"Tool calls: {summary['tool_calls']} | "
          f"Messages: {summary['user_messages']}")


if __name__ == "__main__":
    main()
