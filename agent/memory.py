"""
Persistent memory store — remembers user preferences across sessions.

Stores to a JSON file on disk. Each preference is tagged as 'explicit'
(user directly stated it) or 'inferred' (derived from behavior).
Users can inspect, correct, and forget stored facts.
"""

import json
import os
from datetime import datetime
from typing import Optional, Dict, Any


class MemoryStore:
    """Simple file-backed preference store, keyed by user_id."""

    def __init__(self, path: str = "data/memory.json"):
        self.path = path
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._load()

    # ── Public API ────────────────────────────────────────────────────

    def set(
        self,
        user_id: str,
        key: str,
        value: Any,
        source: str = "explicit",
    ) -> None:
        """Store or update a preference."""
        if user_id not in self._data:
            self._data[user_id] = {}
        self._data[user_id][key] = {
            "value": value,
            "source": source,
            "updated_at": datetime.now().isoformat(),
        }
        self._save()

    def get(self, user_id: str, key: str) -> Optional[Dict[str, Any]]:
        """Return a single preference record, or None."""
        return self._data.get(user_id, {}).get(key)

    def forget(self, user_id: str, key: str) -> bool:
        """Remove a preference. Returns True if it existed."""
        if user_id in self._data and key in self._data[user_id]:
            del self._data[user_id][key]
            self._save()
            return True
        return False

    def list_all(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        """Return all preferences for a user."""
        return dict(self._data.get(user_id, {}))

    def get_context_string(self, user_id: str) -> str:
        """
        Build a human-readable summary of known preferences,
        suitable for injecting into the system prompt.
        """
        prefs = self.list_all(user_id)
        if not prefs:
            return ""

        lines = ["\n═══ KNOWN USER PREFERENCES (from previous sessions) ═══"]
        for key, info in prefs.items():
            label = "(explicitly stated)" if info["source"] == "explicit" else "(inferred from past behaviour)"
            lines.append(f"  • {key}: {info['value']} {label}")
        lines.append(
            "Use these to pre-fill details when possible, but always confirm "
            "with the user. If the user corrects a preference, update it."
        )
        return "\n".join(lines)

    # ── Persistence ───────────────────────────────────────────────────

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._data = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
