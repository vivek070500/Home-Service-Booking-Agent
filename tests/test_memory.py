"""
Tests for persistent memory store.

Run:  python -m pytest tests/test_memory.py -v
"""

import json
import os
import pytest
import tempfile

from agent.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    """MemoryStore backed by a temp file."""
    return MemoryStore(path=str(tmp_path / "memory.json"))


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

class TestMemoryCRUD:

    def test_set_and_get(self, store):
        store.set("user-1", "preferred_area", "Koramangala", source="explicit")
        pref = store.get("user-1", "preferred_area")
        assert pref is not None
        assert pref["value"] == "Koramangala"
        assert pref["source"] == "explicit"
        assert "updated_at" in pref

    def test_get_nonexistent_returns_none(self, store):
        assert store.get("user-1", "fake_key") is None

    def test_update_overwrites(self, store):
        store.set("user-1", "preferred_area", "Koramangala")
        store.set("user-1", "preferred_area", "Whitefield")
        assert store.get("user-1", "preferred_area")["value"] == "Whitefield"

    def test_forget_existing(self, store):
        store.set("user-1", "budget", "1500")
        assert store.forget("user-1", "budget") is True
        assert store.get("user-1", "budget") is None

    def test_forget_nonexistent_returns_false(self, store):
        assert store.forget("user-1", "no_such_key") is False

    def test_list_all(self, store):
        store.set("user-1", "area", "Koramangala")
        store.set("user-1", "time", "mornings")
        prefs = store.list_all("user-1")
        assert len(prefs) == 2
        assert "area" in prefs
        assert "time" in prefs

    def test_list_all_empty_user(self, store):
        assert store.list_all("nobody") == {}


# ---------------------------------------------------------------------------
# Source distinction
# ---------------------------------------------------------------------------

class TestSourceDistinction:

    def test_explicit_vs_inferred(self, store):
        store.set("user-1", "area", "Koramangala", source="explicit")
        store.set("user-1", "budget_pattern", "always picks cheapest", source="inferred")

        assert store.get("user-1", "area")["source"] == "explicit"
        assert store.get("user-1", "budget_pattern")["source"] == "inferred"


# ---------------------------------------------------------------------------
# User isolation
# ---------------------------------------------------------------------------

class TestUserIsolation:

    def test_different_users_separate(self, store):
        store.set("user-A", "area", "Koramangala")
        store.set("user-B", "area", "Whitefield")

        assert store.get("user-A", "area")["value"] == "Koramangala"
        assert store.get("user-B", "area")["value"] == "Whitefield"

    def test_forget_only_affects_target_user(self, store):
        store.set("user-A", "area", "Koramangala")
        store.set("user-B", "area", "Whitefield")

        store.forget("user-A", "area")
        assert store.get("user-A", "area") is None
        assert store.get("user-B", "area")["value"] == "Whitefield"


# ---------------------------------------------------------------------------
# Persistence (survives reload)
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_survives_reload(self, tmp_path):
        path = str(tmp_path / "mem.json")

        store1 = MemoryStore(path=path)
        store1.set("user-1", "area", "Indiranagar")

        # Create a new instance from the same file
        store2 = MemoryStore(path=path)
        pref = store2.get("user-1", "area")
        assert pref is not None
        assert pref["value"] == "Indiranagar"

    def test_corrupt_file_handled(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("{invalid json!!")

        store = MemoryStore(path=path)
        assert store.list_all("user-1") == {}


# ---------------------------------------------------------------------------
# Context string for prompt injection
# ---------------------------------------------------------------------------

class TestContextString:

    def test_empty_user_returns_empty(self, store):
        assert store.get_context_string("nobody") == ""

    def test_builds_readable_string(self, store):
        store.set("user-1", "preferred_area", "Koramangala", source="explicit")
        store.set("user-1", "time_preference", "mornings", source="inferred")

        ctx = store.get_context_string("user-1")
        assert "Koramangala" in ctx
        assert "mornings" in ctx
        assert "explicitly stated" in ctx
        assert "inferred" in ctx


# ---------------------------------------------------------------------------
# Integration with Agent dispatch
# ---------------------------------------------------------------------------

class TestMemoryAgentIntegration:

    def test_remember_via_dispatch(self, tmp_path):
        from agent.engine import Agent
        from mock_tools import MockConfig, ToolServer

        mem = MemoryStore(path=str(tmp_path / "mem.json"))
        ts = ToolServer(config=MockConfig(seed=42))
        agent = Agent(tool_server=ts, api_key="fake", memory=mem)

        result = agent._dispatch("remember_preference", {
            "key": "preferred_area",
            "value": "HSR Layout",
            "source": "explicit",
        })
        assert result["status"] == "ok"
        assert mem.get("user-1", "preferred_area")["value"] == "HSR Layout"

    def test_forget_via_dispatch(self, tmp_path):
        from agent.engine import Agent
        from mock_tools import MockConfig, ToolServer

        mem = MemoryStore(path=str(tmp_path / "mem.json"))
        mem.set("user-1", "preferred_area", "Koramangala")
        ts = ToolServer(config=MockConfig(seed=42))
        agent = Agent(tool_server=ts, api_key="fake", memory=mem)

        result = agent._dispatch("forget_preference", {"key": "preferred_area"})
        assert result["status"] == "ok"
        assert mem.get("user-1", "preferred_area") is None

    def test_list_via_dispatch(self, tmp_path):
        from agent.engine import Agent
        from mock_tools import MockConfig, ToolServer

        mem = MemoryStore(path=str(tmp_path / "mem.json"))
        mem.set("user-1", "area", "Koramangala")
        mem.set("user-1", "time", "mornings")
        ts = ToolServer(config=MockConfig(seed=42))
        agent = Agent(tool_server=ts, api_key="fake", memory=mem)

        result = agent._dispatch("list_preferences", {})
        assert result["count"] == 2
        assert "area" in result["preferences"]

    def test_preferences_injected_into_system_prompt(self, tmp_path):
        from agent.engine import Agent
        from mock_tools import MockConfig, ToolServer

        mem = MemoryStore(path=str(tmp_path / "mem.json"))
        mem.set("user-1", "preferred_area", "Koramangala", source="explicit")
        ts = ToolServer(config=MockConfig(seed=42))
        agent = Agent(tool_server=ts, api_key="fake", memory=mem)

        system_msg = agent.messages[0]["content"]
        assert "Koramangala" in system_msg
        assert "explicitly stated" in system_msg
