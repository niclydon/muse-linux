from muselinux.index import ChatIndex
from muselinux.shim import last_user_text, models_for_index, session_for_model


def test_model_ids_encode_the_chat():
    assert session_for_model("muse") is None
    assert session_for_model("muse-chat-abc-123") == "abc-123"
    assert session_for_model("other-model") is None


def test_models_list_includes_known_chats(tmp_path):
    index = ChatIndex(tmp_path / "chats.json")
    chat = index.create("Demo")
    ids = [m["id"] for m in models_for_index(index)]
    assert ids == ["muse", "muse-chat-" + chat["session_id"]]


def test_last_user_text_plain_and_parts():
    assert last_user_text([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]) == "second"
    assert last_user_text([
        {"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "x"}},
        ]},
    ]) == "hello"
    assert last_user_text([]) == ""
