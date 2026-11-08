"""Headless pilot test: the TUI builds, loads history, sends a turn."""

import pytest

from muselinux import tui
from muselinux.history import Message
from muselinux.index import ChatIndex


class FakeResult:
    ok = True
    reply_text = "Hello from the fake backend"
    completion = "turn_complete"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    async def fake_history(session_id, limit=40):
        return [Message(role="user", text="hi there"),
                Message(role="assistant", text="hello")]

    async def fake_turn(session_id, text, on_status, on_reply,
                        on_approvals):
        on_status("working")
        on_reply("Hello from the fake backend")
        return FakeResult()

    monkeypatch.setattr(tui, "_fetch_history", fake_history)
    monkeypatch.setattr(tui, "_run_turn", fake_turn)
    index = ChatIndex(tmp_path / "chats.json")
    return tui._build_app(index)


async def test_tui_mounts_with_history(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#chat-list") is not None
        assert app.query_one("#composer") is not None
        messages = app.query("#messages .msg")
        assert len(messages) >= 2


async def test_tui_send_turn(app):
    from textual.widgets import Input

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        composer = app.query_one("#composer", Input)
        composer.value = "say hello"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app.sending is False
