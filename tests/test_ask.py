import pytest

from muselinux.ask import ask

from fakes import FakeSession

pytestmark = pytest.mark.asyncio


async def test_ask_follows_one_turn_to_completion():
    session = FakeSession()
    statuses, snapshots = [], []
    result = await ask(session, "chat-1", "say hello", timeout_s=30,
                       on_status=statuses.append,
                       on_reply=snapshots.append)
    assert session.sent == [("say hello", "chat-1")]
    assert result.ok
    assert result.reply_text == "Hello"
    assert result.completion == "turn_complete"
    assert result.task_status == "completed"
    assert "working" in statuses
    assert snapshots[-1] == "Hello"


async def test_ask_bundle_does_not_end_the_turn_vacuously():
    # The fake's pre-send bundle contains a terminal task.status for a
    # previous turn. If it leaked into the fresh turn, the ask would
    # complete with no reply.
    session = FakeSession()
    result = await ask(session, "chat-1", "again", timeout_s=30)
    assert result.reply_text == "Hello"
    assert result.send_message_id == "u1"
