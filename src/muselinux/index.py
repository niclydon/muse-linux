"""A local index of chats this client knows about.

The gadget credential's scopes do not include any chat enumeration
route (see docs/PROBE-RESULTS.md), so a Linux client cannot list the
account's chats from the server. It keeps its own index instead: the
main chat, plus every side chat it created or was told about, enriched
from history's chat_context when a chat is read.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

MAIN_ID = "main"


SERVICE_HOME = Path("/var/lib/muse-linux")


def default_index_path() -> Path:
    base = os.environ.get("MUSE_LINUX_HOME")
    if base:
        return Path(base) / "chats.json"
    # Prefer the service home when an installed service index exists,
    # so terminal and service clients share one chat list by default.
    service_index = SERVICE_HOME / "chats.json"
    if service_index.exists():
        return service_index
    root = Path.home() / ".local" / "share" / "muse-linux"
    return root / "chats.json"


class ChatIndex:
    def __init__(self, path: Path | None = None):
        self.path = path or default_index_path()
        self._data: dict = {"chats": []}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())
        if not any(c["id"] == MAIN_ID for c in self._data["chats"]):
            self._data["chats"].insert(0, {
                "id": MAIN_ID, "label": "Main chat", "kind": "main",
                "session_id": None, "created_at": None})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)

    def list(self) -> list[dict]:
        return list(self._data["chats"])

    def get(self, chat_id: str) -> dict | None:
        for chat in self._data["chats"]:
            if chat["id"] == chat_id or chat.get("session_id") == chat_id:
                return chat
        return None

    def create(self, label: str) -> dict:
        session_id = str(uuid.uuid4())
        chat = {"id": session_id, "label": label, "kind": "side",
                "session_id": session_id,
                "created_at": int(time.time())}
        self._data["chats"].append(chat)
        self._save()
        return chat

    def add_known(self, session_id: str, label: str) -> dict:
        existing = self.get(session_id)
        if existing:
            return existing
        chat = {"id": session_id, "label": label, "kind": "side",
                "session_id": session_id,
                "created_at": int(time.time())}
        self._data["chats"].append(chat)
        self._save()
        return chat

    def rename(self, chat_id: str, label: str) -> bool:
        chat = self.get(chat_id)
        if chat is None:
            return False
        chat["label"] = label
        self._save()
        return True
