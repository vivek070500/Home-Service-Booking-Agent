"""
Tests for the observability tracer.

Run:  python -m pytest tests/test_tracer.py -v
"""

import json
import os
import pytest

from agent.tracer import Tracer


@pytest.fixture
def tracer():
    return Tracer()


class TestTracerRecording:

    def test_user_message(self, tracer):
        tracer.user_message("Hello")
        assert len(tracer.events) == 1
        assert tracer.events[0]["type"] == "user_message"
        assert tracer.events[0]["content"] == "Hello"

    def test_tool_call(self, tracer):
        tracer.tool_call(
            name="search_providers",
            args={"service": "plumbing", "area": "Koramangala"},
            result={"status": "ok", "count": 3},
            latency_ms=5.2,
        )
        assert len(tracer.events) == 1
        evt = tracer.events[0]
        assert evt["type"] == "tool_call"
        assert evt["tool_name"] == "search_providers"
        assert evt["result_status"] == "ok"

    def test_llm_call(self, tracer):
        start = tracer.llm_call_start("gpt-4o-mini", 5)
        tracer.llm_call_end(start, finish_reason="stop", tool_call_count=0)
        assert len(tracer.events) == 2
        assert tracer.events[0]["type"] == "llm_call_start"
        assert tracer.events[1]["type"] == "llm_call_end"
        assert tracer.events[1]["latency_ms"] >= 0

    def test_approval_event(self, tracer):
        tracer.approval_event("book", "approved", details={"hold_id": "H-1"})
        assert tracer.events[0]["type"] == "approval"
        assert tracer.events[0]["outcome"] == "approved"

    def test_bail(self, tracer):
        tracer.bail("Time limit exceeded")
        assert tracer.events[0]["type"] == "bail"

    def test_agent_response_truncated(self, tracer):
        long_text = "A" * 1000
        tracer.agent_response(long_text)
        assert len(tracer.events[0]["content"]) == 500


class TestTracerCounters:

    def test_counters_increment(self, tracer):
        tracer.user_message("hi")
        tracer.user_message("bye")
        tracer.tool_call("search", {}, {"status": "ok"}, 1.0)
        tracer.agent_response("response")

        s = tracer.summary()
        assert s["user_messages"] == 2
        assert s["tool_calls"] == 1
        assert s["agent_responses"] == 1

    def test_llm_latency_average(self, tracer):
        import time
        start1 = tracer.llm_call_start("model", 1)
        time.sleep(0.01)
        tracer.llm_call_end(start1, "stop")

        start2 = tracer.llm_call_start("model", 2)
        time.sleep(0.01)
        tracer.llm_call_end(start2, "stop")

        s = tracer.summary()
        assert s["llm_calls"] == 2
        assert s["avg_llm_latency_ms"] > 0


class TestTracerSave:

    def test_save_to_file(self, tracer, tmp_path):
        tracer.user_message("test")
        tracer.tool_call("search", {"a": 1}, {"status": "ok"}, 2.0)

        path = str(tmp_path / "trace.json")
        tracer.save(path)

        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert "summary" in data
        assert "events" in data
        assert len(data["events"]) == 2

    def test_save_creates_directories(self, tracer, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "trace.json")
        tracer.save(path)
        assert os.path.exists(path)
