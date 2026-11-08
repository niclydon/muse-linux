"""Parsing for GET /chat/history responses.

The history route answers with an envelope whose result carries
``chat_events`` (newest N, chronological), ``has_more`` and
``transcript_revision``. Canonical event names are ``message.user``,
``message.assistant`` and ``delta.presentation``. Each event carries a
``chat_context`` with ``chat_id`` and ``chat_kind``.
"""

from __future__ import annotations

from dataclasses import dataclass


MAX_HISTORY_LIMIT = 200  # measured server ceiling, 2026-11-08


@dataclass
class Message:
    role: str            # "user" or "assistant"
    text: str
    message_id: str | None = None
    occurred_at_ms: int | None = None
    chat_id: str | None = None
    chat_kind: str | None = None
    reaction: str | None = None


def _event_text(event: dict) -> str:
    text = event.get("display_text")
    if isinstance(text, str) and text:
        return text
    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in ("display_text", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def parse_history(result: dict) -> tuple[list[Message], bool]:
    """(messages, has_more) from a tunnel request result dict."""
    response = result.get("response") if isinstance(result, dict) else None
    body = response.get("result") if isinstance(response, dict) else None
    if not isinstance(body, dict):
        return [], False
    messages: list[Message] = []
    for event in body.get("chat_events") or []:
        if not isinstance(event, dict):
            continue
        name = event.get("event_name")
        if name == "message.user":
            role = "user"
        elif name == "message.assistant":
            role = "assistant"
        else:
            continue  # delta.presentation and anything else: not a message
        context = event.get("chat_context") or {}
        reaction = event.get("reaction_emoji")
        messages.append(Message(
            role=role,
            text=_event_text(event),
            message_id=event.get("message_id"),
            occurred_at_ms=event.get("occurred_at_ms"),
            chat_id=context.get("chat_id"),
            chat_kind=context.get("chat_kind"),
            reaction=reaction if isinstance(reaction, str) and reaction
            else None,
        ))
    return messages, bool(body.get("has_more"))


def history_path(session_id: str | None, limit: int) -> str:
    limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    path = f"/chat/history?limit={limit}"
    if session_id:
        path += f"&session_id={session_id}"
    return path


async def fetch_history(session, session_id: str | None,
                        limit: int = 50) -> tuple[list[Message], bool]:
    from musegadget.noise import Header

    result = await session.request(
        "GET", history_path(session_id, limit),
        headers=[Header("Content-Type", "application/json")])
    if not result.get("ok"):
        raise ConnectionError(
            f"history failed: HTTP {result.get('status')}")
    return parse_history(result)
