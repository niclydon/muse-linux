import http.client
import json
import threading
from contextlib import asynccontextmanager
from http.server import ThreadingHTTPServer

import pytest

from muselinux import server as server_module
from muselinux.history import Message
from muselinux.index import ChatIndex
from muselinux.server import chat_public, message_public, sse_pack

from fakes import FakeSession


def test_chat_public_shape(tmp_path):
    index = ChatIndex(tmp_path / "chats.json")
    main = chat_public(index.list()[0])
    assert main == {"id": "main", "label": "Main chat",
                    "session_id": None, "is_main": True}
    chat = chat_public(index.create("Demo"))
    assert chat["is_main"] is False
    assert chat["session_id"] == chat["id"]


def test_message_public_and_sse_pack():
    message = Message(role="assistant", text="hi", message_id="m1",
                      occurred_at_ms=42)
    assert message_public(message) == {
        "role": "assistant", "text": "hi", "message_id": "m1",
        "occurred_at_ms": 42, "reaction": None}
    reacted = Message(role="assistant", text="hi", message_id="m1",
                      reaction="👍")
    assert message_public(reacted)["reaction"] == "👍"
    packed = sse_pack({"type": "reply", "text": "hi"})
    assert packed.startswith(b"data: ") and packed.endswith(b"\n\n")
    assert json.loads(packed[6:-2]) == {"type": "reply", "text": "hi"}


@pytest.fixture()
def backend(tmp_path, monkeypatch):
    """The real HTTP handler with link.open_session faked out."""
    session = FakeSession(with_approvals=True)

    @asynccontextmanager
    async def fake_open_session(*args, **kwargs):
        yield session

    monkeypatch.setattr(server_module.link, "open_session",
                        fake_open_session)
    index = ChatIndex(tmp_path / "chats.json")
    # The fake session's events carry session id "chat-1"; register the
    # chat under that id so the turn machinery binds its replies.
    chat = index.add_known("chat-1", "Demo")
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), server_module.make_handler(index))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield session, chat, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    raw = response.read()
    status, ctype = response.status, response.getheader("Content-Type")
    conn.close()
    return status, ctype, raw


def _sse(port, path, body=None, headers=None):
    status, _ctype, raw = _request(port, "POST", path, body=body,
                                   headers=headers)
    events = []
    for block in raw.decode().split("\n\n"):
        line = block.strip()
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return status, events


def test_history_endpoint_serializes_ids_and_reactions(backend):
    _session, chat, port = backend
    status, _ctype, raw = _request(
        port, "GET", f"/api/chats/{chat['id']}/history?limit=10")
    assert status == 200
    data = json.loads(raw)
    assert data["has_more"] is False
    user, assistant = data["messages"]
    assert user["message_id"] == "u0" and user["reaction"] is None
    assert assistant["message_id"] == "r0"
    assert assistant["reaction"] == "👍"


def test_text_turn_approvals_event_is_structured(backend):
    _session, chat, port = backend
    status, events = _sse(
        port, f"/api/chats/{chat['id']}/messages",
        body=json.dumps({"text": "hi"}),
        headers={"Content-Type": "application/json"})
    assert status == 200
    approvals = [e for e in events if e["type"] == "approvals"]
    assert approvals == [{
        "type": "approvals",
        "pending": [{"id": "ap-1", "title": "Run a shell command",
                     "detail": "ls -la /tmp"}]}]
    done = [e for e in events if e["type"] == "done"]
    assert done and done[-1]["ok"] is True


def test_voice_endpoint_streams_a_done_event(backend):
    session, chat, port = backend
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt fake-voice-note"
    status, events = _sse(
        port, f"/api/chats/{chat['id']}/voice", body=wav,
        headers={"Content-Type": "audio/wav"})
    assert status == 200
    assert session.voice_sent == [(wav, chat["session_id"])]
    replies = [e for e in events if e["type"] == "reply"]
    assert replies and replies[-1]["text"] == "Hello"
    done = [e for e in events if e["type"] == "done"]
    assert done and done[-1]["ok"] is True
    assert done[-1]["completion"] == "turn_complete"


def test_tts_endpoint_returns_audio_bytes(backend):
    session, chat, port = backend
    status, ctype, raw = _request(
        port, "GET", f"/api/chats/{chat['id']}/tts/r0")
    assert status == 200
    assert ctype == "audio/mpeg"
    assert raw == b"fake-mp3-chunk-1;fake-mp3-chunk-2"
    assert session.tts_requests == ["r0"]
