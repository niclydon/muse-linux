"""Per-chat view state for the GNOME client, free of any GTK import.

A ``ChatState`` merges three sources into one ordered transcript:
the history the daemon fetched, live events from the chat's
subscription, and messages this client is sending. It is a live
mirror of the chat: messages sent from the phone or the Mac app at
the same time appear as they arrive, and nothing is labelled as the
answer to a particular send.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

from ..protocol import IDLE_CODES, TERMINAL_TASK_STATUSES

_keys = itertools.count(1)


@dataclass
class Message:
    role: str                      # "user" | "assistant"
    text: str
    message_id: str | None = None
    occurred_at_ms: int | None = None
    done: bool = True
    pending: bool = False          # our send, not yet acknowledged
    error: str | None = None       # our send failed
    uncertain: bool = False        # ...but Muse may have received it
    reaction: str | None = None
    live: bool = False             # arrived as a live event, not history
    key: int = field(default_factory=lambda: next(_keys))


@dataclass
class Change:
    messages: list[Message] = field(default_factory=list)  # added/updated
    removed: list[Message] = field(default_factory=list)
    reset: bool = False            # rebuild the whole transcript
    status: bool = False
    approvals: bool = False
    reply_finished: Message | None = None
    new_approvals: list[dict] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.messages or self.removed or self.reset
                    or self.status or self.approvals)


class ChatState:
    def __init__(self, chat: dict):
        self.chat = chat
        self.messages: list[Message] = []
        self.loaded = False
        self.loading = False
        self.has_more = False
        self.limit = 0
        self.not_started = False
        self.status_code: str | None = None
        self.status_text: str | None = None
        self.task_status: str | None = None
        self.approvals: list[dict] = []
        self.watch_state = "pending"
        self.unread = False
        self._seen_approvals: set = set()

    @property
    def chat_id(self) -> str:
        return self.chat["id"]

    @property
    def is_main(self) -> bool:
        return self.chat_id == "main"

    @property
    def working(self) -> bool:
        if self.status_code and self.status_code not in IDLE_CODES:
            return True
        return bool(self.task_status) and \
            self.task_status not in TERMINAL_TASK_STATUSES

    def _by_id(self, message_id: str | None) -> Message | None:
        if not message_id:
            return None
        for message in self.messages:
            if message.message_id == message_id:
                return message
        return None

    # -- history -------------------------------------------------------
    def load_history(self, result: dict) -> Change:
        history = []
        ids = set()
        for item in result.get("messages") or []:
            message = Message(role=item.get("role") or "assistant",
                              text=item.get("text") or "",
                              message_id=item.get("message_id"),
                              occurred_at_ms=item.get("occurred_at_ms"),
                              reaction=item.get("reaction"))
            history.append(message)
            if message.message_id:
                ids.add(message.message_id)
        # keep in-flight sends and live messages newer than this history
        extra = [m for m in self.messages
                 if (m.pending or m.error or m.live)
                 and (m.message_id is None or m.message_id not in ids)]
        self.messages = history + extra
        self.loaded = True
        self.has_more = bool(result.get("has_more"))
        self.limit = int(result.get("limit") or len(history))
        self.not_started = bool(result.get("not_started"))
        return Change(reset=True)

    # -- our own sends ---------------------------------------------------
    def add_pending(self, text: str) -> Message:
        message = Message(role="user", text=text, pending=True,
                          occurred_at_ms=int(time.time() * 1000))
        self.messages.append(message)
        self.not_started = False
        return message

    def acknowledge(self, message: Message, message_id: str | None) -> Change:
        message.pending = False
        message.live = True
        if message_id:
            twin = self._by_id(message_id)
            if twin is not None and twin is not message:
                # the live user_message event beat the acknowledgement
                self.messages.remove(message)
                return Change(removed=[message])
            message.message_id = message_id
        return Change(messages=[message])

    def fail(self, message: Message, error: str,
             uncertain: bool = False) -> Change:
        message.pending = False
        message.error = error
        message.uncertain = uncertain
        return Change(messages=[message])

    # -- live events -----------------------------------------------------
    def apply(self, event: dict) -> Change:
        kind = event.get("event")
        if kind == "status":
            self.status_code = event.get("code")
            self.status_text = event.get("text")
            return Change(status=True)
        if kind == "task":
            self.task_status = event.get("status")
            return Change(status=True)
        if kind == "watch":
            self.watch_state = event.get("state") or "pending"
            if self.watch_state == "live":
                self.not_started = False
            return Change(status=True)
        if kind == "approvals":
            pending = event.get("pending") or []
            self.approvals = pending
            fresh = []
            for item in pending:
                key = item.get("id") or (item.get("title"), item.get("detail"))
                if key not in self._seen_approvals:
                    self._seen_approvals.add(key)
                    fresh.append(item)
            return Change(approvals=True, new_approvals=fresh)
        if kind == "user_message":
            message_id = event.get("message_id")
            if self._by_id(message_id):
                return Change()
            text = event.get("text") or ""
            for message in self.messages:
                if message.pending and message.message_id is None \
                        and message.text.strip() == text.strip():
                    message.message_id = message_id
                    return Change(messages=[message])
            message = Message(role="user", text=text, message_id=message_id,
                              occurred_at_ms=event.get("occurred_at_ms"),
                              live=True)
            self.messages.append(message)
            return Change(messages=[message])
        if kind == "reply":
            message_id = event.get("message_id")
            message = self._by_id(message_id)
            was_done = message.done if message else False
            if message is None:
                message = Message(role="assistant", text="",
                                  message_id=message_id, done=False,
                                  live=True)
                self.messages.append(message)
            message.text = event.get("text") or message.text
            message.done = bool(event.get("done"))
            if event.get("occurred_at_ms"):
                message.occurred_at_ms = event["occurred_at_ms"]
            change = Change(messages=[message])
            if message.done and not was_done and message.text:
                change.reply_finished = message
            return change
        return Change()
