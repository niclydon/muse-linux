"""The daemon's local socket protocol, shared by daemon and clients.

Transport: a Unix stream socket. Each message is one JSON object on
one line (UTF-8, newline terminated), at most ``MAX_LINE_BYTES``.

Client to daemon, a request::

    {"id": 7, "cmd": "send", "args": {"chat": "main", "text": "hi"}}

Daemon to client, the response to that request::

    {"id": 7, "ok": true, "result": {...}}
    {"id": 7, "ok": false, "error": {"code": "not_connected",
                                     "message": "..."}}

Daemon to client, an event (no ``id``)::

    {"event": "reply", "chat": "main", "message_id": "m1",
     "text": "Hello", "done": false}

docs/PROTOCOL.md is the reference for every command and event. This
module holds the names, limits and the parsing of raw Muse chat
events into protocol events, so the daemon, the clients and the test
fakes agree on one definition.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 32 * 1024          # one outgoing message
MAX_REPLY_CHARS = 64 * 1024         # one assistant message, as shown
MAX_LABEL_CHARS = 200
DEFAULT_SOCKET = "/run/muse-linux/socket"

COMMANDS = (
    "hello", "doctor",
    "chats.list", "chats.new", "chats.add", "chats.rename", "chats.remove",
    "history", "watch", "unwatch", "send",
)

ERROR_CODES = (
    "bad_request",      # malformed request or arguments
    "unknown_command",
    "unknown_chat",
    "not_connected",    # no Muse session right now; retry later
    "upstream",         # Muse answered with an error
    "internal",
)

# Session ids are UUIDs in practice; accept a conservative superset.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# C0 and C1 controls except tab and newline (same rule as the SDK).
_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")

IDLE_CODES = frozenset({"idle", "online"})
TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "errored", "cancelled", "canceled"})


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def encode(obj: dict) -> bytes:
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    data = line.encode("utf-8") + b"\n"
    if len(data) > MAX_LINE_BYTES:
        raise ProtocolError("internal", "message exceeds the line limit")
    return data


def decode(line: bytes) -> dict:
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError("bad_request", "line too long")
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("bad_request", f"not JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise ProtocolError("bad_request", "expected a JSON object")
    return obj


def valid_session_id(value) -> bool:
    return isinstance(value, str) and bool(_SESSION_ID.match(value))


def clean_text(value, limit: int = MAX_REPLY_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROLS.sub("", value)[:limit]


def clean_label(value, default: str) -> str:
    text = clean_text(value, MAX_LABEL_CHARS).replace("\n", " ").strip()
    return text or default


def chat_public(chat: dict) -> dict:
    return {"id": chat["id"], "label": chat["label"],
            "session_id": chat.get("session_id"),
            "is_main": chat["id"] == "main",
            "created_at": chat.get("created_at"),
            "last_activity_ms": chat.get("last_activity_ms")}


def message_public(message) -> dict:
    return {"role": message.role, "text": message.text,
            "message_id": message.message_id,
            "occurred_at_ms": message.occurred_at_ms,
            "reaction": message.reaction}


def approvals_public(payload) -> list[dict]:
    from .approvals import parse_snapshot

    return [{"id": a.approval_id, "title": clean_text(a.title, 500),
             "detail": clean_text(a.detail, 2000)}
            for a in parse_snapshot(payload)]


class ChatTranslator:
    """Turns one chat subscription's raw Muse events into protocol
    events. Holds the per-message text buffers that deltas append to,
    bounded to the most recent messages."""

    MAX_MESSAGES = 32

    def __init__(self, chat: str):
        self.chat = chat
        self._replies: OrderedDict[str, dict] = OrderedDict()

    def _reply(self, message_id: str) -> dict:
        reply = self._replies.get(message_id)
        if reply is None:
            reply = {"text": "", "done": False}
            self._replies[message_id] = reply
            while len(self._replies) > self.MAX_MESSAGES:
                self._replies.popitem(last=False)
        return reply

    def feed(self, raw) -> list[dict]:
        if not isinstance(raw, dict):
            return []
        name = raw.get("event")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        chat = self.chat
        if name == "agent.status":
            code = payload.get("activity_code")
            return [{"event": "status", "chat": chat,
                     "code": code if isinstance(code, str) else None,
                     "text": clean_text(payload.get("activity_text"), 500)
                     or None}]
        if name == "task.status":
            status = payload.get("status")
            if not isinstance(status, str):
                return []
            return [{"event": "task", "chat": chat, "status": status}]
        if name == "approvals.snapshot":
            return [{"event": "approvals", "chat": chat,
                     "pending": approvals_public(payload)}]
        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return []
        at = payload.get("occurred_at_ms")
        at = at if isinstance(at, int) else None
        if name == "message.user":
            text = payload.get("display_text") or payload.get("text") \
                or payload.get("content")
            return [{"event": "user_message", "chat": chat,
                     "message_id": message_id,
                     "text": clean_text(text, MAX_TEXT_CHARS),
                     "occurred_at_ms": at}]
        if name not in ("delta.message_start", "delta.text_append",
                        "delta.message_done", "message.assistant"):
            return []
        reply = self._reply(message_id)
        before = (reply["text"], reply["done"])
        if name == "delta.text_append":
            text = payload.get("text")
            if isinstance(text, str) and not reply["done"]:
                reply["text"] = clean_text(reply["text"] + text)
        elif name == "delta.message_done":
            content = payload.get("content")
            if isinstance(content, str) and content:
                reply["text"] = clean_text(content)
            reply["done"] = True
        elif name == "message.assistant":
            text = payload.get("display_text") or payload.get("text") \
                or payload.get("content")
            if isinstance(text, str) and text:
                reply["text"] = clean_text(text)
            reply["done"] = True
        if name != "delta.message_start" and \
                (reply["text"], reply["done"]) == before:
            return []
        return [{"event": "reply", "chat": chat, "message_id": message_id,
                 "text": reply["text"], "done": reply["done"],
                 "occurred_at_ms": at}]
