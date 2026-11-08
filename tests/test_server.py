import json

from muselinux.history import Message
from muselinux.index import ChatIndex
from muselinux.server import chat_public, message_public, sse_pack


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
        "occurred_at_ms": 42}
    packed = sse_pack({"type": "reply", "text": "hi"})
    assert packed.startswith(b"data: ") and packed.endswith(b"\n\n")
    assert json.loads(packed[6:-2]) == {"type": "reply", "text": "hi"}
