"""An in-memory stand-in for the Muse VM, for tests and offline demos.

``FakeMuse`` keeps chats, histories and live subscribers, and answers
messages with scripted replies that stream the way Muse's do: an
``agent.status`` with activity text, ``task.status`` running, text
deltas, ``delta.message_done``, then the task and agent going idle.
``FakeMuse.connect`` is a drop-in for ``muselinux.link.connect``, so
the real daemon can run against it with no pairing and no network.

Nothing here is used by the daemon or the clients at run time.
"""

from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable


class FakeStreamError(ConnectionError):
    def __init__(self, status: int, path: str = "/chat/subscribe"):
        super().__init__(f"Muse request {path} failed: HTTP {status}")
        self.status = status
        self.path = path


def default_reply(text: str) -> str:
    return f"You said: {text}"


class FakeMuse:
    def __init__(self, reply: Callable[[str], str] = default_reply, *,
                 step_s: float = 0.01, activity: str = "Thinking it over",
                 approvals: list[dict] | None = None):
        self.reply = reply
        self.step_s = step_s
        self.activity = activity
        self.approvals = approvals or []
        self.history: dict[str | None, list[dict]] = {None: []}
        self.sent: list[tuple[str, str | None]] = []
        self.subscribers: dict[str | None, list[asyncio.Queue]] = {}
        self.connections: list[FakeConnection] = []
        self.connect_failures = 0      # fail this many connects first
        self.connect_count = 0
        self._ids = itertools.count(1)
        start = int(time.time() * 1000) - 3_600_000
        self._clock = itertools.count(start, 60_000)

    # -- seeding ---------------------------------------------------------
    def start_chat(self, session_id: str | None) -> None:
        self.history.setdefault(session_id, [])

    def add_history(self, session_id: str | None, role: str,
                    text: str, at: int | None = None) -> str:
        self.start_chat(session_id)
        message_id = f"m{next(self._ids)}"
        self.history[session_id].append({
            "event_name": "message.user" if role == "user"
            else "message.assistant",
            "message_id": message_id, "display_text": text,
            "occurred_at_ms": at or next(self._clock),
            "chat_context": {"chat_id": session_id or "main",
                             "chat_kind": "side" if session_id else "main"}})
        return message_id

    # -- link.connect stand-in -------------------------------------------
    @asynccontextmanager
    async def connect(self, *args, **kwargs):
        self.connect_count += 1
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise ConnectionError("VM lookup failed (HTTP 503)")
        conn = FakeConnection(self)
        self.connections.append(conn)
        try:
            yield conn
        finally:
            conn.close()

    def drop_all(self) -> None:
        """Simulate the network dropping every open session."""
        for conn in list(self.connections):
            conn.close()

    # -- events ----------------------------------------------------------
    async def emit(self, session_id: str | None, name: str, **payload):
        payload.setdefault("session_id", session_id)
        event = {"type": "event", "event": name, "payload": payload}
        for queue in list(self.subscribers.get(session_id, [])):
            await queue.put(event)

    async def _answer(self, session_id, user_id, text):
        step = self.step_s
        reply_id = f"r{next(self._ids)}"
        task_id = f"t{next(self._ids)}"
        await asyncio.sleep(step)
        await self.emit(session_id, "agent.status", activity_code="working",
                        activity_text=self.activity,
                        reply_to_message_id=user_id)
        await self.emit(session_id, "task.status", status="running",
                        task_id=task_id, reply_to_message_id=user_id)
        for approval in self.approvals:
            await self.emit(session_id, "approvals.snapshot",
                            pending_approvals=[approval])
        answer = self.reply(text)
        await self.emit(session_id, "delta.message_start",
                        message_id=reply_id, reply_to_message_id=user_id)
        words = answer.split(" ")
        for i, word in enumerate(words):
            await asyncio.sleep(step)
            await self.emit(session_id, "delta.text_append",
                            message_id=reply_id, reply_to_message_id=user_id,
                            text=word + (" " if i < len(words) - 1 else ""))
        at = int(time.time() * 1000)
        await self.emit(session_id, "delta.message_done", message_id=reply_id,
                        reply_to_message_id=user_id, content=answer,
                        occurred_at_ms=at)
        self.history[session_id].append({
            "event_name": "message.assistant", "message_id": reply_id,
            "display_text": answer, "occurred_at_ms": at,
            "chat_context": {"chat_id": session_id or "main",
                             "chat_kind": "side" if session_id else "main"}})
        await self.emit(session_id, "task.status", status="completed",
                        task_id=task_id, reply_to_message_id=user_id)
        if self.approvals:
            await self.emit(session_id, "approvals.snapshot",
                            pending_approvals=[])
        await self.emit(session_id, "agent.status", activity_code="idle")


class FakeConnection:
    def __init__(self, muse: FakeMuse):
        self.muse = muse
        self.closed = asyncio.get_running_loop().create_future()
        self.session = FakeSession(self)

    async def wait_closed(self) -> None:
        await asyncio.shield(self.closed)

    def close(self) -> None:
        if not self.closed.done():
            self.closed.set_result(None)
        for queues in self.muse.subscribers.values():
            for queue in queues:
                if getattr(queue, "conn", None) is self:
                    queue.put_nowait(None)
        if self in self.muse.connections:
            self.muse.connections.remove(self)


class FakeSession:
    def __init__(self, conn: FakeConnection):
        self.conn = conn
        self.muse = conn.muse
        self.chat_subscribed = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    async def subscribe_chat(self, session_id=None, *, opened=None):
        if self.conn.closed.done():
            raise ConnectionError("session ended")
        if session_id not in self.muse.history:
            raise FakeStreamError(404)
        queue: asyncio.Queue = asyncio.Queue()
        queue.conn = self.conn
        self.muse.subscribers.setdefault(session_id, []).append(queue)
        try:
            self.chat_subscribed.set()
            if opened is not None:
                opened.set()
            # the subscribe-time state bundle
            for name, payload in (("agent.status", {"activity_code": "idle"}),
                                  ("task.status", {"status": "completed"}),
                                  ("approvals.snapshot", {})):
                payload["session_id"] = session_id
                yield {"type": "event", "event": name, "payload": payload}
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            subs = self.muse.subscribers.get(session_id, [])
            if queue in subs:
                subs.remove(queue)

    async def send_chat(self, message, session_id=None, **kwargs):
        if self.conn.closed.done():
            raise ConnectionError("Muse session is not connected")
        self.muse.sent.append((message, session_id))
        self.muse.start_chat(session_id)
        user_id = self.muse.add_history(session_id, "user", message,
                                        at=int(time.time() * 1000))
        await self.muse.emit(session_id, "message.user", message_id=user_id,
                             display_text=message)
        task = asyncio.create_task(
            self.muse._answer(session_id, user_id, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"ok": True, "status": 200, "response": {"result": {
            "message_id": user_id,
            "session_id": session_id or "main",
            "is_thread": bool(session_id)}}}

    async def request(self, method, path, body=b"", *, headers=None,
                      timeout=None):
        from urllib.parse import parse_qs, urlparse

        if self.conn.closed.done():
            raise ConnectionError("session ended")
        parsed = urlparse(path)
        if parsed.path == "/chat/history":
            query = parse_qs(parsed.query)
            session_id = (query.get("session_id") or [None])[0]
            limit = int((query.get("limit") or ["50"])[0])
            if session_id not in self.muse.history:
                return {"ok": False, "status": 404, "response": None}
            events = self.muse.history[session_id]
            return {"ok": True, "status": 200, "response": {"result": {
                "has_more": len(events) > limit,
                "transcript_revision": len(events),
                "chat_events": events[-limit:]}}}
        return {"ok": False, "status": 403,
                "response": "route not allowed for this credential's scopes"}


async def fake_doctor(*args, **kwargs) -> dict:
    return {"state_dir": "/var/lib/musegadget", "paired": True,
            "node_id": "fake-node", "vm_fetch_status": 200, "vm_count": 1}


def new_session_id() -> str:
    return str(uuid.uuid4())


