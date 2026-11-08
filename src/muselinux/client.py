"""Client for the muse-linux daemon socket, shared by every front end.

Toolkit-agnostic on purpose: a background thread owns the socket,
reconnects when the daemon restarts, re-sends the client's watches,
and hands events to a callback. Requests return
``concurrent.futures.Future`` objects. A GTK front end wraps the
callbacks in ``GLib.idle_add``; a Qt front end would emit signals.
Nothing here touches Link, the SDK or any credential.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import threading
import time
from concurrent.futures import Future
from typing import Callable

from .protocol import DEFAULT_SOCKET, MAX_LINE_BYTES, PROTOCOL_VERSION

SOCKET_ENV = "MUSE_LINUX_SOCKET"


def default_socket_path() -> str:
    return os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET


class DaemonError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DaemonUnavailable(DaemonError):
    def __init__(self, message: str, in_flight: bool = False):
        super().__init__("unavailable", message)
        # the request had already gone out when the connection closed,
        # so the daemon may have carried it out
        self.in_flight = in_flight


def describe_socket_problem(path: str, exc: BaseException) -> str:
    """One plain sentence on why the socket cannot be opened, with the fix."""
    if isinstance(exc, FileNotFoundError):
        return ("The muse-linux daemon is not running. Start it with "
                "'sudo systemctl start muse-linuxd'.")
    if isinstance(exc, PermissionError):
        return ("You are not allowed to use the daemon's socket. Add "
                "yourself to the muse-linux group, then log out and back "
                "in: 'sudo usermod -aG muse-linux $USER'.")
    if isinstance(exc, ConnectionRefusedError):
        return ("The daemon socket exists but nothing answers on it. "
                "Restart the daemon: 'sudo systemctl restart muse-linuxd'.")
    return f"Cannot reach the daemon at {path}: {exc}"


class DaemonClient:
    def __init__(self, socket_path: str | None = None, *,
                 on_event: Callable[[dict], None] | None = None,
                 on_link: Callable[[bool, str | None], None] | None = None,
                 reconnect: bool = True, retry_max_s: float = 10.0):
        self.socket_path = socket_path or default_socket_path()
        self.on_event = on_event
        self.on_link = on_link
        self.reconnect = reconnect
        self.retry_max_s = retry_max_s
        self.connected = False
        self.last_problem: str | None = None
        self.daemon_info: dict | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, Future] = {}
        self._watches: set[str] = set()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()

    # -- lifecycle ---------------------------------------------------
    def start(self) -> "DaemonClient":
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="muse-linux-client")
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(5)

    def retry_now(self) -> None:
        """Skip the remaining backoff and try to connect again."""
        self._wake.set()

    def wait_connected(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected:
                return True
            time.sleep(0.02)
        return self.connected

    # -- requests ----------------------------------------------------
    def call(self, cmd: str, **args) -> Future:
        future: Future = Future()
        sock = self._sock
        if sock is None or not self.connected:
            future.set_exception(DaemonUnavailable(
                self.last_problem or "not connected to the daemon"))
            return future
        request_id = next(self._ids)
        line = json.dumps({"id": request_id, "cmd": cmd, "args": args},
                          ensure_ascii=False).encode() + b"\n"
        if len(line) > MAX_LINE_BYTES:
            future.set_exception(DaemonError("bad_request", "request too large"))
            return future
        with self._lock:
            self._pending[request_id] = future
        try:
            with self._send_lock:
                sock.sendall(line)
        except OSError as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            future.set_exception(DaemonUnavailable(str(exc)))
        return future

    def request(self, cmd: str, timeout: float = 90.0, **args):
        return self.call(cmd, **args).result(timeout)

    def watch(self, chat: str) -> Future:
        self._watches.add(chat)
        return self.call("watch", chat=chat)

    def unwatch(self, chat: str) -> Future:
        self._watches.discard(chat)
        return self.call("unwatch", chat=chat)

    @property
    def watched(self) -> set[str]:
        return set(self._watches)

    # -- the socket thread -------------------------------------------
    def _run(self) -> None:
        delay = 0.5
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.socket_path)
            except OSError as exc:
                sock.close()
                self._set_link(False, describe_socket_problem(
                    self.socket_path, exc))
                if not self.reconnect:
                    return
                self._wake.wait(delay)
                self._wake.clear()
                delay = min(delay * 2, self.retry_max_s)
                continue
            delay = 0.5
            self._sock = sock
            reader = threading.Thread(target=self._after_connect,
                                      daemon=True)
            self.connected = True
            reader.start()
            self._read(sock)
            self._sock = None
            self.connected = False
            with self._lock:
                pending, self._pending = self._pending, {}
            for future in pending.values():
                if not future.done():
                    future.set_exception(DaemonUnavailable(
                        "the daemon connection closed", in_flight=True))
            try:
                sock.close()
            except OSError:
                pass
            if self._stop.is_set():
                break
            self._set_link(False, "The connection to the daemon closed; "
                                  "reconnecting.")
            if not self.reconnect:
                return
            self._wake.wait(delay)
            self._wake.clear()

    def _after_connect(self) -> None:
        try:
            info = self.request("hello", timeout=10)
        except Exception as exc:
            self._set_link(False, f"The daemon did not answer: {exc}")
            return
        self.daemon_info = info
        if info.get("protocol") != PROTOCOL_VERSION:
            self._set_link(False, (
                f"The daemon speaks protocol {info.get('protocol')}, this "
                f"client speaks {PROTOCOL_VERSION}. Update both together."))
            return
        for chat in list(self._watches):
            self.call("watch", chat=chat)
        self._set_link(True, None)

    def _set_link(self, up: bool, problem: str | None) -> None:
        changed = problem != self.last_problem or up
        self.last_problem = problem
        if self.on_link and changed:
            try:
                self.on_link(up, problem)
            except Exception:
                pass

    def _read(self, sock: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            if len(buffer) > MAX_LINE_BYTES and b"\n" not in buffer:
                return
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    self._dispatch(line)

    def _dispatch(self, line: bytes) -> None:
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        if "id" in message and "ok" in message:
            with self._lock:
                future = self._pending.pop(message.get("id"), None)
            if future is None or future.done():
                return
            if message.get("ok"):
                future.set_result(message.get("result"))
            else:
                error = message.get("error") or {}
                future.set_exception(DaemonError(
                    str(error.get("code") or "internal"),
                    str(error.get("message") or "request failed")))
            return
        if "event" in message and self.on_event:
            try:
                self.on_event(message)
            except Exception:
                pass
