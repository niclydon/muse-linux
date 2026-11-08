import pytest

from muselinux.history import fetch_history, history_path, parse_history

from fakes import FakeSession




def test_history_path_clamps_and_scopes():
    assert history_path(None, 500) == "/chat/history?limit=200"
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


def test_widget_tokens_are_stripped_from_display_text():
    result = _result([
        {"event_name": "message.assistant",
         "display_text": "Here is the plan.\n\n"
                         "[[hatch_widget:widget-4033d-fa78]]\n",
         "message_id": "a1"},
        {"event_name": "message.user", "display_text": "Thanks",
         "message_id": "u1"},
    ])
    messages, _ = parse_history(result)
    assert messages[0].text == "Here is the plan."
    assert messages[1].text == "Thanks"
