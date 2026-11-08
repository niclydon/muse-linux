"""The real daemon on a real Unix socket, against an in-memory Muse."""

import asyncio
import json
import os
import socket
import stat
import threading

import pytest

from muselinux.client import DaemonClient, DaemonError, DaemonUnavailable
from muselinux.daemon import Daemon
from muselinux.index import ChatIndex
from muselinux.protocol import PROTOCOL_VERSION
from muselinux.testing import FakeMuse, fake_doctor


class Events:
    """Thread-safe event collector with waiting."""

    def __init__(self):
        self.items = []
        self.cond = threading.Condition()

    def __call__(self, event):
        with self.cond:
            self.items.append(event)
            self.cond.notify_all()

    async def wait_for(self, predicate, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            with self.cond:
                found = [e for e in self.items if predicate(e)]
            if found:
                return found[0]
            await asyncio.sleep(0.01)
        raise AssertionError(f"event never arrived; saw {self.items}")

    def of(self, kind, chat=None):
        with self.cond:
            return [e for e in self.items if e.get("event") == kind
                    and (chat is None or e.get("chat") == chat)]


@pytest.fixture()
async def stack(tmp_path):
    muse = FakeMuse()
    index = ChatIndex(tmp_path / "chats.json")
    sock = str(tmp_path / "d.sock")
    daemon = Daemon(sock, index, connect=muse.connect, doctor=fake_doctor,
                    idle_s=0.3, backoff_base_s=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    for _ in range(100):
        if os.path.exists(sock):
            break
        await asyncio.sleep(0.01)
    clients = []

    def make_client(**kwargs):
        events = Events()
        client = DaemonClient(sock, on_event=events, retry_max_s=0.2,
                              **kwargs).start()
        assert client.wait_connected()
        client.events = events
        clients.append(client)
        return client

    yield muse, daemon, index, sock, make_client
    for client in clients:
        await asyncio.to_thread(client.close)
    stop.set()
    await asyncio.wait_for(task, 5)


def call(client, cmd, **args):
    return asyncio.to_thread(client.request, cmd, 10, **args)


async def test_socket_is_private_and_hello_reports_protocol(stack):
    muse, daemon, index, sock, make_client = stack
    mode = stat.S_IMODE(os.stat(sock).st_mode)
    assert mode == 0o660
    client = make_client()
    hello = await call(client, "hello")
    assert hello["protocol"] == PROTOCOL_VERSION
    for _ in range(200):  # the client's own hello runs in its thread
        if client.daemon_info:
            break
        await asyncio.sleep(0.01)
    assert client.daemon_info["protocol"] == PROTOCOL_VERSION


async def test_chat_index_commands_round_trip(stack):
    muse, daemon, index, sock, make_client = stack
    client = make_client()
    chats = (await call(client, "chats.list"))["chats"]
    assert [c["id"] for c in chats] == ["main"]
    new = await call(client, "chats.new", label="  Trip\nplanning  ")
    assert new["label"] == "Trip planning" and not new["is_main"]
    added = await call(client, "chats.add",
                       session_id="11111111-2222-3333-4444-555555555555",
                       label="From phone")
    renamed = await call(client, "chats.rename", chat=added["id"],
                         label="From my phone")
    assert renamed["label"] == "From my phone"
    await call(client, "chats.remove", chat=new["id"])
    ids = [c["id"] for c in (await call(client, "chats.list"))["chats"]]
    assert ids == ["main", added["id"]]
    assert index.get(new["id"]) is None
    changed = client.events.of("chats.changed")
    assert any(e.get("removed") == new["id"] for e in changed)


async def test_bad_requests_are_typed_errors(stack):
    muse, daemon, index, sock, make_client = stack
    client = make_client()
    with pytest.raises(DaemonError) as info:
        await call(client, "no.such.command")
    assert info.value.code == "unknown_command"
    with pytest.raises(DaemonError) as info:
        await call(client, "send", chat="nope", text="hi")
    assert info.value.code == "unknown_chat"
    with pytest.raises(DaemonError) as info:
        await call(client, "chats.add", session_id="../../etc", label="x")
    assert info.value.code == "bad_request"
    with pytest.raises(DaemonError) as info:
        await call(client, "chats.remove", chat="main")
    assert info.value.code == "bad_request"
    with pytest.raises(DaemonError) as info:
        await call(client, "send", chat="main", text="x" * 40000)
    assert info.value.code == "bad_request"


async def test_malformed_lines_do_not_kill_the_connection(stack):
    muse, daemon, index, sock, make_client = stack

    def raw_exchange():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock)
        s.sendall(b"this is not json\n")
        s.sendall(json.dumps({"id": 1, "cmd": "hello"}).encode() + b"\n")
        data = b""
        s.settimeout(5)
        replies = []
        while len(replies) < 2:
            data += s.recv(65536)
            lines = [json.loads(x) for x in data.splitlines() if x]
            replies = [m for m in lines if "ok" in m]
        s.close()
        return replies

    replies = await asyncio.to_thread(raw_exchange)
    seen = {(m.get("id"), m.get("ok")) for m in replies}
    assert (None, False) in seen and (1, True) in seen


async def test_watch_and_send_stream_a_live_reply(stack):
    muse, daemon, index, sock, make_client = stack
    client = make_client()
    await asyncio.to_thread(lambda: client.watch("main").result(10))
    await client.events.wait_for(lambda e: e.get("event") == "watch"
                           and e.get("state") == "live")
    sent = await call(client, "send", chat="main", text="hello there")
    assert sent["message_id"]
    done = await client.events.wait_for(lambda e: e.get("event") == "reply"
                                  and e.get("done"))
    assert done["text"] == "You said: hello there"
    partial = [e for e in client.events.of("reply", "main") if not e["done"]]
    assert partial, "the reply should stream before it is done"
    status = await client.events.wait_for(lambda e: e.get("event") == "status"
                                    and e.get("code") == "working")
    assert status["text"] == "Thinking it over"
    await client.events.wait_for(lambda e: e.get("event") == "task"
                           and e.get("status") == "completed")
    user = await client.events.wait_for(lambda e: e.get("event") == "user_message")
    assert user["message_id"] == sent["message_id"]
    assert muse.sent == [("hello there", None)]


async def test_history_returns_messages(stack):
    muse, daemon, index, sock, make_client = stack
    muse.add_history(None, "user", "earlier question")
    muse.add_history(None, "assistant", "earlier answer")
    client = make_client()
    history = await call(client, "history", chat="main", limit=10)
    assert [m["text"] for m in history["messages"]] == [
        "earlier question", "earlier answer"]
    assert history["has_more"] is False


async def test_a_new_side_chat_starts_on_its_first_message(stack):
    muse, daemon, index, sock, make_client = stack
    client = make_client()
    chat = await call(client, "chats.new", label="Fresh")
    history = await call(client, "history", chat=chat["id"])
    assert history["not_started"] is True and history["messages"] == []
    await asyncio.to_thread(lambda: client.watch(chat["id"]).result(10))
    await client.events.wait_for(lambda e: e.get("event") == "watch"
                           and e.get("state") == "not_started")
    await call(client, "send", chat=chat["id"], text="first")
    await client.events.wait_for(lambda e: e.get("event") == "watch"
                           and e.get("chat") == chat["id"]
                           and e.get("state") == "live")
    done = await client.events.wait_for(lambda e: e.get("event") == "reply"
                                  and e.get("chat") == chat["id"]
                                  and e.get("done"))
    assert done["text"] == "You said: first"
    assert muse.sent[-1] == ("first", chat["session_id"])
    assert index.get(chat["id"]).get("last_activity_ms")


async def test_two_clients_share_one_subscription_and_late_joiners_get_state(stack):
    muse, daemon, index, sock, make_client = stack
    muse.approvals = [{"id": "ap-1", "title": "Run a command",
                       "detail": "ls"}]
    first = make_client()
    await asyncio.to_thread(lambda: first.watch("main").result(10))
    await first.events.wait_for(lambda e: e.get("event") == "watch"
                          and e.get("state") == "live")
    await call(first, "send", chat="main", text="go")
    await first.events.wait_for(lambda e: e.get("event") == "approvals"
                          and e.get("pending"))
    second = make_client()
    await asyncio.to_thread(lambda: second.watch("main").result(10))
    # the late joiner receives the latest state events straight away
    await second.events.wait_for(lambda e: e.get("event") == "approvals")
    await second.events.wait_for(lambda e: e.get("event") == "status")
    assert len(muse.subscribers.get(None, [])) == 1


async def test_session_reconnects_and_watches_resume(stack):
    muse, daemon, index, sock, make_client = stack
    client = make_client()
    await asyncio.to_thread(lambda: client.watch("main").result(10))
    await client.events.wait_for(lambda e: e.get("event") == "watch"
                           and e.get("state") == "live")
    connects = muse.connect_count
    muse.connect_failures = 1
    muse.drop_all()
    await client.events.wait_for(lambda e: e.get("event") == "connection"
                           and e.get("state") == "disconnected")
    for _ in range(200):
        if muse.connect_count >= connects + 2 and daemon.state == "connected" \
                and muse.subscribers.get(None):
            break
        await asyncio.sleep(0.02)
    assert daemon.state == "connected"
    assert muse.subscribers.get(None), "the watch should resubscribe"
    await call(client, "send", chat="main", text="after the drop")
    await client.events.wait_for(lambda e: e.get("event") == "reply"
                           and e.get("text") == "You said: after the drop"
                           and e.get("done"))


async def test_session_closes_when_the_last_client_leaves(stack):
    muse, daemon, index, sock, make_client = stack
    client = make_client()
    await call(client, "history", chat="main")
    assert daemon.state == "connected"
    await asyncio.to_thread(client.close)
    for _ in range(200):
        if daemon.state == "idle":
            break
        await asyncio.sleep(0.02)
    assert daemon.state == "idle"
    assert not muse.connections


async def test_doctor_never_reports_the_node_id(stack):
    muse, daemon, index, sock, make_client = stack
    client = make_client()
    report = await call(client, "doctor")
    assert report["pairing"]["paired"] is True
    assert "node_id" not in report["pairing"]
    assert "node_id" not in json.dumps(report)
    assert report["daemon"]["protocol"] == PROTOCOL_VERSION


async def test_client_reconnects_after_a_daemon_restart(tmp_path):
    muse = FakeMuse()
    index = ChatIndex(tmp_path / "chats.json")
    sock = str(tmp_path / "d.sock")
    links = []
    client = DaemonClient(sock, retry_max_s=0.1,
                          on_link=lambda up, why: links.append(up)).start()
    with pytest.raises(DaemonUnavailable):
        await call(client, "hello")
    for _ in range(200):
        if client.last_problem:
            break
        await asyncio.sleep(0.01)
    assert "not running" in (client.last_problem or "")

    async def run_daemon():
        daemon = Daemon(sock, index, connect=muse.connect,
                        doctor=fake_doctor, idle_s=0.2)
        stop = asyncio.Event()
        task = asyncio.create_task(daemon.run(stop))
        return stop, task

    client.watch("main")
    for _round in range(2):
        stop, task = await run_daemon()
        assert await asyncio.to_thread(client.wait_connected, 5)
        for _ in range(100):
            if True in links:
                break
            await asyncio.sleep(0.02)
        assert (await call(client, "hello"))["protocol"] == PROTOCOL_VERSION
        for _ in range(100):
            if muse.subscribers.get(None):
                break
            await asyncio.sleep(0.02)
        assert muse.subscribers.get(None), "watches are re-sent"
        links.clear()
        stop.set()
        await asyncio.wait_for(task, 5)
    await asyncio.to_thread(client.close)


def test_live_replies_drop_muse_app_widget_tokens():
    from muselinux.protocol import ChatTranslator

    translator = ChatTranslator("main")
    events = translator.feed({"type": "event", "event": "message.assistant",
                              "payload": {"message_id": "r1",
                                          "display_text": "Here is the plan.\n\n"
                                          "[[hatch_widget:widget-1]]\n"}})
    assert events[0]["text"] == "Here is the plan."


async def test_a_client_that_stops_reading_does_not_hold_up_others(stack):
    """A client that never reads its socket is dropped once its queue
    fills, and events keep reaching everyone else."""
    import muselinux.daemon as daemon_mod

    muse, daemon, index, sock, make_client = stack
    reader = make_client()
    await asyncio.to_thread(lambda: reader.watch("main").result(10))

    stuck = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    await asyncio.to_thread(stuck.connect, sock)
    stuck.sendall(json.dumps({"id": 1, "cmd": "watch",
                              "args": {"chat": "main"}}).encode() + b"\n")
    for _ in range(200):
        if len(daemon.clients) == 2:
            break
        await asyncio.sleep(0.01)
    stuck_client = max(daemon.clients, key=lambda c: c.number)
    try:
        # flood the stuck client's queue directly, as many events would
        big = {"event": "reply", "chat": "main", "text": "x" * 4000}
        for _ in range(daemon_mod.OUTBOX_MAX + 10):
            await stuck_client.send(big)
        assert stuck_client.closed
        await call(reader, "send", chat="main", text="still flowing")
        await reader.events.wait_for(
            lambda e: e.get("event") == "reply" and e.get("done"))
    finally:
        stuck.close()


async def test_oversized_history_is_trimmed_to_fit(stack):
    muse, daemon, index, sock, make_client = stack
    for i in range(60):
        muse.add_history(None, "assistant", f"{i} " + "y" * 30000)
    client = make_client()
    result = await call(client, "history", chat="main", limit=60)
    assert result["trimmed"] and result["has_more"]
    assert 0 < len(result["messages"]) < 60
    # the newest message is kept
    assert result["messages"][-1]["text"].startswith("59 ")


async def test_index_is_touched_once_per_reply_not_per_delta(stack):
    muse, daemon, index, sock, make_client = stack
    client = make_client()
    await asyncio.to_thread(lambda: client.watch("main").result(10))
    await call(client, "send", chat="main", text="count my touches")
    await client.events.wait_for(
        lambda e: e.get("event") == "reply" and e.get("done"))
    await asyncio.sleep(0.1)
    changed = [e for e in client.events.of("chats.changed")
               if e.get("chat", {}).get("id") == "main"]
    deltas = [e for e in client.events.of("reply", "main") if not e["done"]]
    assert len(deltas) > 1
    # the send, the user message and the finished reply; never a delta
    assert len(changed) <= 3 < len(deltas) + 3


async def test_history_text_has_control_characters_removed(stack):
    muse, daemon, index, sock, make_client = stack
    muse.add_history(None, "assistant", "bell\x07 and\x1b[31m escape")
    client = make_client()
    result = await call(client, "history", chat="main")
    assert result["messages"][-1]["text"] == "bell and[31m escape"
