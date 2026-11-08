import pytest

from muselinux.history import fetch_history, history_path, parse_history

from fakes import FakeSession




def test_history_path_clamps_and_scopes():
    assert history_path(None, 500) == "/chat/history?limit=50"
    assert history_path("abc", 5) == "/chat/history?limit=5&session_id=abc"


async def test_parse_history_from_fake_session():
    session = FakeSession()
    messages, has_more = await fetch_history(session, "chat-1", 20)
    assert has_more is False
    assert [(m.role, m.text) for m in messages] == [
        ("user", "hi"), ("assistant", "hello")]
    assert messages[0].chat_kind == "side"


def test_parse_history_tolerates_bad_envelope():
    assert parse_history({"response": None}) == ([], False)
    assert parse_history({}) == ([], False)
