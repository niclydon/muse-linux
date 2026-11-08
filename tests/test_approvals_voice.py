"""Approvals parsing, reaction parsing, and voice helpers."""

import pytest

from muselinux.approvals import format_pending, parse_snapshot
from muselinux.history import parse_history
from muselinux.voice import MAX_WAV_BYTES, fetch_tts, send_voice_message


def test_parse_snapshot_shapes():
    snapshot = {"pending_approvals": [
        {"id": "a1", "title": "Run a shell command",
         "detail": "ls ~/workspace"},
        {"approval_id": "a2", "tool_name": "send_email"},
        {"unexpected": True},
        "not-a-dict",
    ]}
    parsed = parse_snapshot(snapshot)
    assert [p.approval_id for p in parsed] == ["a1", "a2", None]
    assert parsed[0].title == "Run a shell command"
    assert parsed[1].title == "send_email"
    assert parsed[2].title == "Approval requested"
    assert "waiting in the Muse app" in format_pending(parsed)
    assert parse_snapshot({}) == []
    assert parse_snapshot(None) == []
    assert format_pending([]) == ""


def test_parse_history_reaction():
    result = {"response": {"result": {"chat_events": [
        {"event_name": "message.user", "display_text": "hi",
         "message_id": "m1", "reaction_emoji": "👍",
         "chat_context": {"chat_id": "c", "chat_kind": "side"}},
        {"event_name": "message.assistant", "display_text": "hello",
         "message_id": "m2", "reaction_emoji": "",
         "chat_context": {}},
    ], "has_more": False}}}
    messages, _ = parse_history(result)
    assert messages[0].reaction == "👍"
    assert messages[1].reaction is None


class VoiceSession:
    def __init__(self):
        self.sent = None

    async def send_voice(self, wav, session_id=None):
        self.sent = (wav, session_id)
        return {"ok": True}

    async def stream_tts(self, message_id):
        yield b"ID3"
        yield b"rest"


async def test_voice_helpers():
    session = VoiceSession()
    result = await send_voice_message(session, "chat-1", b"RIFF....")
    assert result == {"ok": True}
    assert session.sent == (b"RIFF....", "chat-1")
    with pytest.raises(ValueError):
        await send_voice_message(session, None, b"x" * (MAX_WAV_BYTES + 1))
    assert await fetch_tts(session, "m1") == b"ID3rest"
