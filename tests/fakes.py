"""A fake LinkSession speaking the SDK's event shapes, for tests."""

from __future__ import annotations

import asyncio


def event(name, message_id=None, *, parent=None, session="chat-1", **payload):
    data = {"session_id": session, **payload}
    if message_id is not None:
        data["message_id"] = message_id
    if parent is not None:
        data["reply_to_message_id"] = parent
    return {"type": "event", "event": name, "payload": data}


class FakeSession:
    """Bundle first, then (after send) one streamed reply and task end."""

    def __init__(self):
        self.chat_subscribed = asyncio.Event()
        self.sent: list[tuple[str, str | None]] = []
        self._sent = asyncio.Event()
        self.requests: list[tuple[str, str]] = []

    async def subscribe_chat(self, session_id=None):
        self.chat_subscribed.set()
        yield event("agent.status", activity_code="online")
        yield event("task.status", status="completed")
        yield event("approvals.snapshot")
        await self._sent.wait()
        yield event("agent.status", parent="u1", activity_code="working")
        yield event("delta.text_append", "r1", parent="u1", text="Hel")
        yield event("delta.text_append", "r1", parent="u1", text="lo")
        yield event("delta.message_done", "r1", parent="u1",
                    content="Hello")
        yield event("task.status", parent="u1", status="completed")
        await asyncio.sleep(3600)

    async def send_chat(self, message, session_id=None, **kwargs):
        self.sent.append((message, session_id))
        self._sent.set()
        return {"ok": True, "status": 200,
                "response": {"result": {"message_id": "u1",
                                        "session_id": session_id or "chat-1"}}}

    async def request(self, method, path, body=b"", *, headers=None,
                      timeout=None):
        self.requests.append((method, path))
        if path.startswith("/chat/history"):
            return {"ok": True, "status": 200, "response": {"result": {
                "channel": "all", "has_more": False,
                "transcript_revision": 7,
                "chat_events": [
                    {"event_name": "message.user", "message_id": "u0",
                     "display_text": "hi", "occurred_at_ms": 1,
                     "chat_context": {"chat_id": "chat-1",
                                      "chat_kind": "side"}},
                    {"event_name": "delta.presentation",
                     "message_id": "x", "display_text": "ignored"},
                    {"event_name": "message.assistant", "message_id": "r0",
                     "display_text": "hello", "occurred_at_ms": 2,
                     "chat_context": {"chat_id": "chat-1",
                                      "chat_kind": "side"}},
                ]}}}
        return {"ok": False, "status": 403,
                "response": "route not allowed for this credential's scopes"}
