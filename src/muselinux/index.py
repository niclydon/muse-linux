"""A local index of chats this client knows about.

The gadget credential's scopes do not include any chat enumeration
route (see docs/PROBE-RESULTS.md), so a Linux client cannot list the
account's chats from the server. It keeps its own index instead: the
main chat, plus every side chat it created or was told about.

More than one process can share an index file (the daemon, the web
service and the terminal clients all default to the same one), so
every change takes an exclusive lock on a sidecar lock file, re-reads
the file, applies the change and writes it back atomically. Reads
re-read the file whenever it changed on disk. Without this, two
processes holding their own copies silently drop each other's chats.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
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


def _main_entry() -> dict:
    return {"id": MAIN_ID, "label": "Main chat", "kind": "main",
            "session_id": None, "created_at": None}


class ChatIndex:
    def __init__(self, path: Path | None = None):
        self.path = path or default_index_path()
        self._data: dict = {"chats": []}
        self._stamp: tuple | None = None
        self._refresh()

    # -- storage -----------------------------------------------------
    def _read_disk(self, strict: bool = False) -> dict:
        """The index as stored. A damaged file reads as empty, except
        when about to write (``strict``): then it is an error, so a
        bad file is never silently replaced by an empty one."""
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"chats": []}
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            if strict:
                raise RuntimeError(
                    f"chat index {self.path} is unreadable "
                    f"({type(exc).__name__}); not overwriting it") from exc
            return {"chats": []}
        if not isinstance(data, dict) or \
                not isinstance(data.get("chats"), list):
            return {"chats": []}
        return data

    @staticmethod
    def _seed(data: dict) -> dict:
        chats = [c for c in data["chats"]
                 if isinstance(c, dict) and c.get("id")]
        if not any(c["id"] == MAIN_ID for c in chats):
            chats.insert(0, _main_entry())
        data["chats"] = chats
        return data

    def _disk_stamp(self) -> tuple | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _refresh(self) -> None:
        stamp = self._disk_stamp()
        if stamp is not None and stamp == self._stamp:
            return
        self._data = self._seed(self._read_disk())
        self._stamp = stamp

    @contextmanager
    def _mutate(self):
        """Lock, reload, yield the data for change, then save."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with open(lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self._data = self._seed(self._read_disk(strict=True))
                yield self._data
                tmp = self.path.with_name(
                    f".{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(self._data, indent=2))
                tmp.replace(self.path)
                self._stamp = self._disk_stamp()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _find(data: dict, chat_id: str) -> dict | None:
        for chat in data["chats"]:
            if chat["id"] == chat_id or chat.get("session_id") == chat_id:
                return chat
        return None

    # -- queries -----------------------------------------------------
    def list(self) -> list[dict]:
        self._refresh()
        return [dict(c) for c in self._data["chats"]]

    def get(self, chat_id: str) -> dict | None:
        self._refresh()
        chat = self._find(self._data, chat_id)
        return dict(chat) if chat is not None else None

    # -- changes -----------------------------------------------------
    def create(self, label: str) -> dict:
        session_id = str(uuid.uuid4())
        chat = {"id": session_id, "label": label, "kind": "side",
                "session_id": session_id,
                "created_at": int(time.time())}
        with self._mutate() as data:
            data["chats"].append(chat)
        return dict(chat)

    def add_known(self, session_id: str, label: str) -> dict:
        with self._mutate() as data:
            existing = self._find(data, session_id)
            if existing:
                return dict(existing)
            chat = {"id": session_id, "label": label, "kind": "side",
                    "session_id": session_id,
                    "created_at": int(time.time())}
            data["chats"].append(chat)
        return dict(chat)

    def rename(self, chat_id: str, label: str) -> bool:
        with self._mutate() as data:
            chat = self._find(data, chat_id)
            if chat is None:
                return False
            chat["label"] = label
        return True

    def remove(self, chat_id: str) -> bool:
        """Drop a side chat from the local list. The chat itself stays
        on the server; the main chat cannot be removed."""
        with self._mutate() as data:
            chat = self._find(data, chat_id)
            if chat is None or chat["id"] == MAIN_ID:
                return False
            data["chats"].remove(chat)
        return True

    def touch(self, chat_id: str, at_ms: int | None = None) -> bool:
        """Record activity on a chat, for newest-first ordering.
        Returns True when the stored time moved forward."""
        at_ms = int(at_ms or time.time() * 1000)
        self._refresh()
        current = self._find(self._data, chat_id)
        if current is None or (current.get("last_activity_ms") or 0) >= at_ms:
            return False
        with self._mutate() as data:
            chat = self._find(data, chat_id)
            if chat is None or (chat.get("last_activity_ms") or 0) >= at_ms:
                return False
            chat["last_activity_ms"] = at_ms
        return True
