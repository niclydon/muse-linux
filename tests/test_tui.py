"""Headless pilot test: the TUI builds, loads history, sends a turn."""

import pytest

from muselinux import tui
from muselinux.history import Message
from muselinux.index import ChatIndex


class FakeResult:
    ok = True
    reply_text = "Hello from the fake backend"
    completion = "turn_complete"


async def fake_history(session_id, limit=40):
    return [Message(role="user", text="hi there"),
            Message(role="assistant", text="hello")]


async def fake_turn(session_id, text, on_status, on_reply, on_approvals):
    on_status("working")
    on_reply("Hello from the fake backend")
    return FakeResult()


def build_app(tmp_path, monkeypatch, history=None, turn=None,
              extra_chats=()):
    monkeypatch.setattr(tui, "_fetch_history", history or fake_history)
    monkeypatch.setattr(tui, "_run_turn", turn or fake_turn)
    index = ChatIndex(tmp_path / "chats.json")
    for session_id, label in extra_chats:
        index.add_known(session_id, label)
    return tui._build_app(index)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    return build_app(tmp_path, monkeypatch)


async def test_tui_mounts_with_history(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#chat-list") is not None
        assert app.query_one("#composer") is not None
        messages = app.query("#messages .msg")
        assert len(messages) >= 2
        # 2 messages came back, fewer than the limit: no button.
        assert len(app.query("#load-older")) == 0


async def test_tui_send_turn(app):
    from textual.widgets import TextArea

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        composer = app.query_one("#composer", TextArea)
        composer.text = "say hello"
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()
        assert app.sending is False
        assert composer.text == ""
        # The user message and the streamed reply were mounted.
        assert len(app.query("#messages .msg")) >= 3


async def test_tui_tab_toggles_focus(app):
    from textual.widgets import ListView, TextArea

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        composer = app.query_one("#composer", TextArea)
        chat_list = app.query_one("#chat-list", ListView)
        assert app.focused is composer
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is chat_list
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is composer


async def test_tui_filter_hides_nonmatching(tmp_path, monkeypatch):
    from textual.widgets import Input, ListView

    app = build_app(tmp_path, monkeypatch, extra_chats=[
        ("sess-alpha", "Alpha project"),
        ("sess-beta", "Beta project"),
    ])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.focused is app.query_one("#chat-filter", Input)
        await pilot.press("a", "l", "p", "h", "a")
        await pilot.pause()
        await pilot.pause()
        items = {item.chat["label"]: item.display
                 for item in app.query_one("#chat-list", ListView).children}
        # Hiding is by display flag only; every chat stays in the list.
        assert len(items) == 3
        assert items["Alpha project"]
        assert not items["Beta project"]
        assert not items["Main chat"]


async def test_tui_load_older_button(tmp_path, monkeypatch):
    async def full_history(session_id, limit=40):
        return [Message(role="user", text=f"message {i}")
                for i in range(limit)]

    app = build_app(tmp_path, monkeypatch, history=full_history)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        # Exactly `limit` messages came back: older history may exist.
        assert len(app.query("#load-older")) == 1
        assert app.history_limit == 40
        # The button sits at the top of the scrolled-to-end pane;
        # scroll it into view before clicking.
        app.query_one("#messages").scroll_home(animate=False)
        await pilot.pause()
        await pilot.click("#load-older")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        assert app.history_limit == 80
