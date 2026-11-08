"""muse-linux daemon: the one process that holds the gadget credential.

The Muse Gadget SDK keeps its pairing store root-only, so a desktop
app running as a normal user cannot open a Link session itself. The
daemon runs as a system service, reads the pairing in process, holds
one long-lived ephemeral Link session (reconnecting with backoff when
it drops), keeps one chat subscription per chat that a client is
watching, and serves desktop clients over a Unix socket. Token
material never leaves this process. See docs/DAEMON.md for the
service model and docs/PROTOCOL.md for the wire format.

The session is opened on demand: while at least one client is
connected, and for ``idle_s`` seconds after the last one leaves.
"""

from __future__ import annotations

import asyncio
import grp
import inspect
import json
import logging
import os
import random
import socket
import struct
import time
from contextlib import suppress
from pathlib import Path

from . import __version__, link
from .index import MAIN_ID, ChatIndex
from .protocol import (
    MAX_LINE_BYTES, MAX_TEXT_CHARS, PROTOCOL_VERSION, ChatTranslator,
    ProtocolError, chat_public, clean_label, clean_text, decode, encode,
    message_public, valid_session_id,
)

log = logging.getLogger("muselinux.daemon")

MAX_CLIENTS = 32
MAX_INFLIGHT = 8          # concurrent requests per client
OUTBOX_MAX = 2000         # queued outgoing messages per client
OUTBOX_MAX_BYTES = 16 * 1024 * 1024   # and queued bytes
WRITE_TIMEOUT_S = 10.0
OPEN_GRACE_S = 3.0        # older SDKs: when to call a quiet stream live
RESPONSE_BUDGET = MAX_LINE_BYTES - 64 * 1024
MAX_WATCHES = 16
IDLE_S = 300.0
CONNECT_WAIT_S = 45.0
BACKOFF_MAX_S = 60.0


class Client:
    """One connected front end.

    Outgoing messages go through a bounded queue drained by the
    client's own writer task, so one client that stops reading never
    slows events for anyone else: when its queue overflows, or a write
    stalls for WRITE_TIMEOUT_S, it is disconnected.
    """

    _next = 0

    def __init__(self, reader, writer):
        Client._next += 1
        self.number = Client._next
        self.reader = reader
        self.writer = writer
        self.watching: set[str] = set()
        self.uid = _peer_uid(writer)
        self.closed = False
        self.outbox: asyncio.Queue = asyncio.Queue(maxsize=OUTBOX_MAX)
        self.queued_bytes = 0
        self.slots = asyncio.Semaphore(MAX_INFLIGHT)
        self.writer_task = asyncio.create_task(self._write_loop())

    async def send(self, obj: dict) -> None:
        """Queue a message; never blocks."""
        if self.closed:
            return
        try:
            data = encode(obj)
        except ProtocolError:
            log.warning("dropping an oversized message for client %d",
                        self.number)
            return
        if self.queued_bytes + len(data) > OUTBOX_MAX_BYTES:
            log.warning("client %d is not reading; disconnecting it",
                        self.number)
            self.abort()
            return
        try:
            self.outbox.put_nowait(data)
        except asyncio.QueueFull:
            log.warning("client %d is not reading; disconnecting it",
                        self.number)
            self.abort()
            return
        self.queued_bytes += len(data)

    async def _write_loop(self) -> None:
        try:
            while True:
                data = await self.outbox.get()
                self.queued_bytes -= len(data)
                self.writer.write(data)
                await asyncio.wait_for(self.writer.drain(), WRITE_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, asyncio.TimeoutError, RuntimeError):
            self.abort()

    def abort(self) -> None:
        self.closed = True
        transport = getattr(self.writer, "transport", None)
        if transport is not None:
            transport.abort()


def _supports_opened(session) -> bool:
    try:
        params = inspect.signature(session.subscribe_chat).parameters
    except (TypeError, ValueError):
        return False
    return "opened" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _subscribe_chat(session, session_id, opened: asyncio.Event):
    """subscribe_chat, with ``opened`` set once the stream is up.

    Newer SDKs take an ``opened`` event and set it when the HTTP stream
    is established. With an SDK that does not, treat the stream as open
    at its first event, or after it has run a few seconds without
    failing (an idle chat may send nothing at first)."""
    if _supports_opened(session):
        async for raw in session.subscribe_chat(session_id, opened=opened):
            yield raw
        return

    async def settle():
        await asyncio.sleep(OPEN_GRACE_S)
        opened.set()

    timer = asyncio.create_task(settle())
    try:
        async for raw in session.subscribe_chat(session_id):
            opened.set()
            yield raw
    finally:
        timer.cancel()


def _size(obj: dict) -> int:
    return len(json.dumps(obj, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


def _peer_uid(writer) -> int | None:
    sock = writer.get_extra_info("socket")
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    except OSError:
        return None


class Watch:
    """A chat subscription shared by every client watching that chat."""

    def __init__(self, chat: dict):
        self.chat_id = chat["id"]
        self.session_id = chat.get("session_id")
        self.clients: set[Client] = set()
        self.translator = ChatTranslator(self.chat_id)
        self.task: asyncio.Task | None = None
        self.state = "pending"      # pending|live|not_started|error
        self.error: str | None = None
        # latest state events, replayed to a client that joins late
        self.latest: dict[str, dict] = {}

    def public(self) -> dict:
        return {"event": "watch", "chat": self.chat_id,
                "state": self.state, "error": self.error}


class Daemon:
    def __init__(self, socket_path: str | Path, index: ChatIndex | None = None,
                 *, connect=None, doctor=None, socket_group: str | None = None,
                 socket_mode: int = 0o660, idle_s: float = IDLE_S,
                 max_watches: int = MAX_WATCHES,
                 backoff_base_s: float = 1.0):
        self.socket_path = Path(socket_path)
        self.index = index or ChatIndex()
        self._connect = connect or link.connect
        self._doctor = doctor or link.doctor
        self.socket_group = socket_group
        self.socket_mode = socket_mode
        self.idle_s = idle_s
        self.max_watches = max_watches
        self.backoff_base_s = backoff_base_s
        self.clients: set[Client] = set()
        self.watches: dict[str, Watch] = {}
        self.conn = None
        self.state = "idle"          # idle|connecting|connected|disconnected
        self.last_error: str | None = None
        self.connected_since: float | None = None
        self._demand = asyncio.Event()
        self._connected = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._server = None
        self._idle_timer = None

    # -- lifecycle ---------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            self.socket_path.unlink()
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._serve_client, path=str(self.socket_path),
                limit=MAX_LINE_BYTES + 1)
        finally:
            os.umask(old_umask)
        if self.socket_group:
            gid = grp.getgrnam(self.socket_group).gr_gid
            os.chown(self.socket_path, -1, gid)
        os.chmod(self.socket_path, self.socket_mode)
        log.info("listening on %s", self.socket_path)
        supervisor = asyncio.create_task(self._supervise())
        try:
            await stop.wait()
        finally:
            supervisor.cancel()
            self._server.close()
            for client in list(self.clients):
                client.closed = True
                with suppress(Exception):
                    client.writer.close()
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(supervisor, *self._tasks,
                                 return_exceptions=True)
            with suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            with suppress(FileNotFoundError):
                self.socket_path.unlink()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- the Muse session ------------------------------------------------
    async def _set_state(self, state: str, error: str | None = None):
        self.state = state
        if error is not None:
            self.last_error = error
        await self._broadcast({"event": "connection", "state": state,
                               "error": error})

    async def _supervise(self) -> None:
        backoff = self.backoff_base_s
        while True:
            await self._demand.wait()
            await self._set_state("connecting")
            error = None
            try:
                async with self._connect() as conn:
                    self.conn = conn
                    self.connected_since = time.time()
                    self._connected.set()
                    backoff = self.backoff_base_s
                    self.last_error = None
                    await self._set_state("connected")
                    for watch in list(self.watches.values()):
                        self._start_watch(watch)
                    reason = await self._hold(conn)
                    if reason == "idle":
                        log.info("no clients for %ss; closing the session",
                                 self.idle_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.warning("Muse session failed: %s", error)
            finally:
                self._connected.clear()
                self.conn = None
                self.connected_since = None
                for watch in self.watches.values():
                    if watch.task is not None:
                        watch.task.cancel()
                        watch.task = None
                    if watch.state == "live":
                        watch.state = "pending"
                        await self._watch_broadcast(watch, watch.public())
            if not self._demand.is_set():
                await self._set_state("idle")
                continue
            await self._set_state("disconnected", error or "session closed")
            delay = backoff * (1 + random.random() * 0.25)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
            await asyncio.sleep(delay)

    async def _hold(self, conn) -> str:
        """Wait until the session drops or the daemon goes idle."""
        closed = asyncio.create_task(conn.wait_closed())
        try:
            while True:
                done, _ = await asyncio.wait({closed}, timeout=1.0)
                if done:
                    with suppress(Exception):
                        closed.result()
                    return "closed"
                if not self._demand.is_set():
                    return "idle"
        finally:
            closed.cancel()
            with suppress(BaseException):
                await closed

    def _client_count_changed(self) -> None:
        if self.clients:
            self._demand.set()
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
        elif self._demand.is_set() and self._idle_timer is None:
            self._idle_timer = asyncio.get_running_loop().call_later(
                self.idle_s, self._go_idle)

    def _go_idle(self) -> None:
        self._idle_timer = None
        if not self.clients:
            self._demand.clear()

    async def _session(self):
        if self.conn is None:
            try:
                await asyncio.wait_for(self._connected.wait(),
                                       CONNECT_WAIT_S)
            except asyncio.TimeoutError:
                raise ProtocolError(
                    "not_connected",
                    "no Muse session yet: " + (self.last_error or self.state))
        if self.conn is None:
            raise ProtocolError("not_connected", "Muse session dropped")
        return self.conn.session

    # -- watches ---------------------------------------------------------
    def _start_watch(self, watch: Watch) -> None:
        if self.conn is None or (watch.task and not watch.task.done()):
            return
        watch.task = self._spawn(self._subscribe(watch, self.conn))

    async def _subscribe(self, watch: Watch, conn) -> None:
        delay = 1.0
        while self.conn is conn and watch.clients:
            opened = asyncio.Event()
            notifier = self._spawn(self._when_opened(watch, opened))
            try:
                async for raw in _subscribe_chat(
                        conn.session, watch.session_id, opened):
                    delay = 1.0
                    for event in watch.translator.feed(raw):
                        await self._chat_event(watch, event)
                error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(exc, "status", None) == 404 and watch.session_id:
                    # a side chat that has no first message yet
                    watch.state, watch.error = "not_started", None
                    await self._watch_broadcast(watch, watch.public())
                    return
                error = f"{type(exc).__name__}: {exc}"
            finally:
                notifier.cancel()
            watch.state, watch.error = "error", error or "stream ended"
            await self._watch_broadcast(watch, watch.public())
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _when_opened(self, watch: Watch, opened: asyncio.Event):
        await opened.wait()
        watch.state, watch.error = "live", None
        await self._watch_broadcast(watch, watch.public())

    async def _chat_event(self, watch: Watch, event: dict) -> None:
        kind = event["event"]
        if kind in ("status", "task", "approvals"):
            watch.latest[kind] = event
        if kind == "user_message" or (kind == "reply" and event.get("done")):
            # once per message, not per streamed delta; file I/O and the
            # index lock stay off the event loop
            moved = await asyncio.to_thread(
                self.index.touch, watch.chat_id, event.get("occurred_at_ms"))
            if moved:
                chat = await asyncio.to_thread(self.index.get, watch.chat_id)
                if chat:
                    await self._broadcast({"event": "chats.changed",
                                           "chat": chat_public(chat)})
        await self._watch_broadcast(watch, event)

    async def _watch_broadcast(self, watch: Watch, event: dict) -> None:
        for client in list(watch.clients):
            await client.send(event)

    async def _broadcast(self, event: dict) -> None:
        for client in list(self.clients):
            await client.send(event)

    async def _add_watch(self, client: Client, chat: dict) -> Watch:
        watch = self.watches.get(chat["id"])
        if watch is None:
            if len(self.watches) >= self.max_watches:
                raise ProtocolError(
                    "bad_request",
                    f"at most {self.max_watches} chats can be watched")
            watch = Watch(chat)
            self.watches[chat["id"]] = watch
        is_new_client = client not in watch.clients
        watch.clients.add(client)
        client.watching.add(chat["id"])
        if is_new_client:
            await client.send(watch.public())
            for event in watch.latest.values():
                await client.send(event)
        self._start_watch(watch)
        return watch

    def _drop_watch(self, client: Client, chat_id: str) -> None:
        client.watching.discard(chat_id)
        watch = self.watches.get(chat_id)
        if watch is None:
            return
        watch.clients.discard(client)
        if not watch.clients:
            if watch.task is not None:
                watch.task.cancel()
            del self.watches[chat_id]

    # -- clients ---------------------------------------------------------
    async def _serve_client(self, reader, writer) -> None:
        if len(self.clients) >= MAX_CLIENTS:
            writer.close()
            return
        client = Client(reader, writer)
        self.clients.add(client)
        self._client_count_changed()
        log.info("client %d connected (uid %s)", client.number, client.uid)
        try:
            while not client.closed:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await client.send({"event": "error", "error": {
                        "code": "bad_request", "message": "line too long"}})
                    break
                except ConnectionError:
                    break
                if not line:
                    break
                if not line.strip():
                    continue
                # at most MAX_INFLIGHT requests per client at a time;
                # reading waits until one finishes
                await client.slots.acquire()
                task = self._spawn(self._handle_one(client, line))
                # released however the task ends, even cancelled unstarted
                task.add_done_callback(lambda _t, c=client: c.slots.release())
        finally:
            client.closed = True
            client.writer_task.cancel()
            self.clients.discard(client)
            for chat_id in list(client.watching):
                self._drop_watch(client, chat_id)
            self._client_count_changed()
            with suppress(Exception):
                writer.close()
            log.info("client %d left", client.number)

    @staticmethod
    async def _reply(client: Client, response: dict) -> None:
        try:
            encode(response)
        except ProtocolError:
            response = {"id": response.get("id"), "ok": False, "error": {
                "code": "too_large",
                "message": "the answer is too large to send; ask for less"}}
        await client.send(response)

    async def _handle_one(self, client: Client, line: bytes) -> None:
        request_id = None
        try:
            request = decode(line)
            request_id = request.get("id")
            cmd = request.get("cmd")
            args = request.get("args") or {}
            if not isinstance(args, dict):
                raise ProtocolError("bad_request", "args must be an object")
            handler = self._commands().get(cmd)
            if handler is None:
                raise ProtocolError("unknown_command", f"unknown command {cmd!r}")
            result = await handler(client, args)
            await self._reply(client, {"id": request_id, "ok": True,
                                       "result": result})
        except ProtocolError as exc:
            await client.send({"id": request_id, "ok": False,
                               "error": {"code": exc.code,
                                         "message": exc.message}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("command failed")
            await client.send({"id": request_id, "ok": False, "error": {
                "code": "internal", "message": f"{type(exc).__name__}: {exc}"}})

    def _commands(self) -> dict:
        return {
            "hello": self._cmd_hello,
            "doctor": self._cmd_doctor,
            "chats.list": self._cmd_chats_list,
            "chats.new": self._cmd_chats_new,
            "chats.add": self._cmd_chats_add,
            "chats.rename": self._cmd_chats_rename,
            "chats.remove": self._cmd_chats_remove,
            "history": self._cmd_history,
            "watch": self._cmd_watch,
            "unwatch": self._cmd_unwatch,
            "send": self._cmd_send,
        }

    def _chat(self, args: dict) -> dict:
        ref = args.get("chat")
        if not isinstance(ref, str) or not ref:
            raise ProtocolError("bad_request", "chat is required")
        chat = self.index.get(ref)
        if chat is None:
            raise ProtocolError("unknown_chat", f"unknown chat {ref!r}")
        return chat

    # -- commands --------------------------------------------------------
    async def _cmd_hello(self, client, args):
        return {"protocol": PROTOCOL_VERSION, "version": __version__,
                "connection": self.state, "error": self.last_error}

    async def _cmd_doctor(self, client, args):
        try:
            pairing = await asyncio.wait_for(self._doctor(), 30)
        except asyncio.TimeoutError:
            pairing = {"paired": None, "error": "pairing check timed out"}
        pairing = {k: v for k, v in dict(pairing).items()
                   if k in ("state_dir", "paired", "vm_fetch_status",
                            "vm_count", "error")}
        return {"daemon": {
                    "version": __version__, "protocol": PROTOCOL_VERSION,
                    "connection": self.state, "error": self.last_error,
                    "connected_since": self.connected_since,
                    "watches": {w.chat_id: w.state
                                for w in self.watches.values()},
                    "clients": len(self.clients)},
                "pairing": pairing}

    async def _cmd_chats_list(self, client, args):
        return {"chats": [chat_public(c) for c in self.index.list()]}

    async def _changed(self, chat: dict | None, removed: str | None = None):
        event = {"event": "chats.changed"}
        if chat is not None:
            event["chat"] = chat_public(chat)
        if removed is not None:
            event["removed"] = removed
        await self._broadcast(event)

    async def _cmd_chats_new(self, client, args):
        chat = self.index.create(clean_label(args.get("label"), "New chat"))
        await self._changed(chat)
        return chat_public(chat)

    async def _cmd_chats_add(self, client, args):
        session_id = args.get("session_id")
        if isinstance(session_id, str):
            session_id = session_id.strip()
        if not valid_session_id(session_id) or session_id == MAIN_ID:
            raise ProtocolError("bad_request", "that is not a chat id")
        chat = self.index.add_known(
            session_id, clean_label(args.get("label"), "Chat"))
        await self._changed(chat)
        return chat_public(chat)

    async def _cmd_chats_rename(self, client, args):
        chat = self._chat(args)
        self.index.rename(chat["id"], clean_label(args.get("label"),
                                                  chat["label"]))
        chat = self.index.get(chat["id"])
        await self._changed(chat)
        return chat_public(chat)

    async def _cmd_chats_remove(self, client, args):
        chat = self._chat(args)
        if chat["id"] == MAIN_ID:
            raise ProtocolError("bad_request", "the main chat stays")
        self.index.remove(chat["id"])
        watch = self.watches.pop(chat["id"], None)
        if watch is not None:
            if watch.task is not None:
                watch.task.cancel()
            for watcher in watch.clients:
                watcher.watching.discard(chat["id"])
        await self._changed(None, removed=chat["id"])
        return {"removed": chat["id"]}

    async def _cmd_history(self, client, args):
        from .history import MAX_HISTORY_LIMIT, fetch_history

        chat = self._chat(args)
        limit = args.get("limit", 50)
        if not isinstance(limit, int) or limit < 1:
            raise ProtocolError("bad_request", "limit must be a positive integer")
        limit = min(limit, MAX_HISTORY_LIMIT)
        session = await self._session()
        try:
            messages, has_more = await asyncio.wait_for(
                fetch_history(session, chat.get("session_id"), limit), 60)
        except asyncio.TimeoutError:
            raise ProtocolError("upstream", "history timed out")
        except ConnectionError as exc:
            if "404" in str(exc) and chat.get("session_id"):
                return {"chat": chat["id"], "messages": [],
                        "has_more": False, "not_started": True}
            raise ProtocolError("upstream", str(exc))
        public = [message_public(m) for m in messages]
        result = {"chat": chat["id"], "messages": public,
                  "has_more": has_more, "limit": limit}
        # keep the newest messages that fit in one protocol line, sizing
        # each message once
        total, keep = 0, 0
        for message in reversed(public):
            total += _size(message) + 1
            if total > RESPONSE_BUDGET:
                break
            keep += 1
        if keep < len(public):
            result["messages"] = public[len(public) - keep:]
            result["has_more"] = True
            result["trimmed"] = True
        return result

    async def _cmd_watch(self, client, args):
        chat = self._chat(args)
        watch = await self._add_watch(client, chat)
        return {"chat": chat["id"], "state": watch.state}

    async def _cmd_unwatch(self, client, args):
        ref = args.get("chat")
        chat = self.index.get(ref) if isinstance(ref, str) else None
        chat_id = chat["id"] if chat else ref
        if isinstance(chat_id, str):
            self._drop_watch(client, chat_id)
        return {"chat": chat_id}

    async def _cmd_send(self, client, args):
        chat = self._chat(args)
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("bad_request", "text is required")
        if len(text) > MAX_TEXT_CHARS:
            raise ProtocolError(
                "bad_request", f"messages are limited to {MAX_TEXT_CHARS} characters")
        text = clean_text(text, MAX_TEXT_CHARS)
        session = await self._session()
        session_id = chat.get("session_id")
        try:
            result = await asyncio.wait_for(
                session.send_chat(text, session_id=session_id)
                if session_id else session.send_chat(text), 75)
        except asyncio.TimeoutError:
            raise ProtocolError(
                "upstream", "the send timed out; Muse may still have "
                            "received it, so check before sending again")
        except ConnectionError as exc:
            raise ProtocolError("upstream", str(exc))
        if not isinstance(result, dict) or not result.get("ok"):
            status = result.get("status") if isinstance(result, dict) else None
            raise ProtocolError("upstream", f"Muse refused the message (HTTP {status})")
        message_id = _sent_message_id(result)
        if await asyncio.to_thread(self.index.touch, chat["id"]):
            await self._changed(self.index.get(chat["id"]))
        watch = self.watches.get(chat["id"])
        if watch is not None and watch.state in ("not_started", "error"):
            watch.state = "pending"
            self._start_watch(watch)
        return {"chat": chat["id"], "message_id": message_id}


def _sent_message_id(result: dict) -> str | None:
    response = result.get("response")
    if isinstance(response, dict):
        inner = response.get("result")
        if isinstance(inner, dict) and isinstance(inner.get("message_id"), str):
            return inner["message_id"]
        if isinstance(response.get("message_id"), str):
            return response["message_id"]
    return None


def run(socket_path: str, index_path: str | None = None,
        socket_group: str | None = None) -> None:
    """Entry point for ``muse-linux daemon``; runs until SIGTERM/SIGINT."""
    import signal

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    # The SDK's websocket layer can log request headers at debug level.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    index = ChatIndex(Path(index_path)) if index_path else ChatIndex()
    daemon = Daemon(socket_path, index, socket_group=socket_group)

    async def _main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await daemon.run(stop)

    asyncio.run(_main())
