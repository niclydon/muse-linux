"""The native local API the desktop GUI talks to.

Unlike the OpenAI shim, this API keeps the Muse-specific surface:
real chats by id, canonical history, live working status, task state
and approval snapshots. It binds to localhost and serves:

    GET  /api/status                 pairing and VM reachability
    GET  /api/chats                  the local chat index
    POST /api/chats                  {"label": "..."} create an entry
    POST /api/chats/add              {"session_id": "...", "label": ...}
    POST /api/chats/<id>/rename      {"label": "..."}
    GET  /api/chats/<id>/history     canonical transcript
    POST /api/chats/<id>/messages    {"text": "..."} -> SSE event stream
    POST /api/chats/<id>/voice       raw WAV body -> SSE event stream
    GET  /api/chats/<id>/tts/<message_id>
                                     spoken reply (audio/mpeg)

Message-stream events are JSON, one per SSE message:
    {"type": "status", "code": "working"}
    {"type": "reply", "text": "<snapshot so far>"}
    {"type": "approvals", "pending": [{"id": ..., "title": ...,
                                       "detail": ...}]}
    {"type": "done", "completion": "...", "task_status": "..."}
    {"type": "error", "error": "..."}

Same trust boundary as the shim: any local program that can reach the
port can act as the paired device. Web pages cannot: the server
refuses foreign Origin and non-loopback Host headers and requires a
JSON or WAV content type on writes (see muselinux.localguard).
Localhost only, unless an authenticating proxy sits in front.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import link
from .approvals import parse_snapshot
from .ask import ask
from .history import fetch_history
from .index import ChatIndex
from .localguard import allowed_hosts, cors_origin, is_json, refusal
from .voice import fetch_tts, send_voice_message

DEFAULT_PORT = 8766


def chat_public(chat: dict) -> dict:
    return {"id": chat["id"], "label": chat["label"],
            "session_id": chat.get("session_id"),
            "is_main": chat["id"] == "main"}


def message_public(message) -> dict:
    return {"role": message.role, "text": message.text,
            "message_id": message.message_id,
            "occurred_at_ms": message.occurred_at_ms,
            "reaction": message.reaction}


def approvals_event(snapshot) -> dict:
    """The structured approvals stream event for one snapshot payload."""
    return {"type": "approvals",
            "pending": [{"id": approval.approval_id,
                         "title": approval.title,
                         "detail": approval.detail}
                        for approval in parse_snapshot(snapshot)]}


def sse_pack(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def run_turn(session_id, text):
    """Run one turn on its own session; returns an AskResult."""
    async def _go():
        async with link.open_session() as session:
            return await ask(session, session_id, text)
    return asyncio.run(_go())


def run_turn_streaming(session_id, text, emit):
    """Run one turn, calling emit(payload) for each stream event."""
    async def _go():
        async with link.open_session() as session:
            result = await ask(
                session, session_id, text,
                on_status=lambda code: emit({"type": "status",
                                             "code": code}),
                on_reply=lambda snap: emit({"type": "reply",
                                            "text": snap}),
                on_approvals=lambda data: emit(approvals_event(data)))
            emit({"type": "done", "completion": result.completion,
                  "task_status": result.task_status,
                  "reply_message_id": result.reply_message_id,
                  "ok": result.ok})
            return result
    return asyncio.run(_go())


def fetch_history_blocking(session_id, limit):
    async def _go():
        async with link.open_session() as session:
            return await fetch_history(session, session_id, limit)
    return asyncio.run(_go())


def fetch_tts_blocking(message_id):
    async def _go():
        async with link.open_session() as session:
            return await fetch_tts(session, message_id)
    return asyncio.run(_go())


TERMINAL_TASK_STATUSES = {"completed", "failed", "errored",
                          "cancelled", "canceled"}


async def follow_voice_turn(session, session_id, wav_bytes, emit,
                            timeout_s: float = 180):
    """Follow one voice-message turn, mirroring ask.ask's flow.

    Subscribes first, gates the subscribe-time state bundle, sends
    the voice message, then follows events: agent activity becomes
    status events, reply deltas become reply snapshots, approvals
    snapshots become structured approvals events. The turn ends on a
    terminal task.status, on a finished reply followed by the quiet
    grace period, or on timeout. Returns a result dict shaped like
    the fields the done event carries.
    """
    from .ask import BUNDLE_EVENTS, QUIET_GRACE_S

    state: dict = {
        "bundle": set(),
        "send_issued": False,
        "replies": {},        # message_id -> {"text": str, "done": bool}
        "snapshot": "",
        "codes": set(),
        "task_status": None,
        "terminal_at": None,
        "last_reply_at": None,
    }

    def handle(event) -> None:
        name = event.get("event")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if name in BUNDLE_EVENTS:
            state["bundle"].add(name)
        if not state["send_issued"]:
            return  # pre-send events never belong to this turn
        if name == "approvals.snapshot":
            emit(approvals_event(payload))
            return
        if name == "agent.status":
            code = payload.get("activity_code")
            if code and code not in state["codes"]:
                state["codes"].add(code)
                emit({"type": "status", "code": code})
        if name == "task.status" and payload.get("status"):
            state["task_status"] = payload["status"]
            if payload["status"] in TERMINAL_TASK_STATUSES \
                    and state["terminal_at"] is None:
                state["terminal_at"] = time.monotonic()
        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return
        reply = state["replies"].setdefault(
            message_id, {"text": "", "done": False})
        changed = False
        if name == "delta.text_append":
            text = payload.get("text")
            if isinstance(text, str) and text:
                reply["text"] += text
                changed = True
        elif name == "delta.message_done":
            content = payload.get("content")
            if isinstance(content, str) and content:
                reply["text"] = content
            reply["done"] = True
            changed = True
        elif name == "message.assistant":
            text = payload.get("display_text") or payload.get("text") \
                or payload.get("content")
            if isinstance(text, str) and text:
                reply["text"] = text
            reply["done"] = True
            changed = True
        if changed:
            state["last_reply_at"] = time.monotonic()
            snapshot = "\n\n".join(
                r["text"] for r in state["replies"].values() if r["text"])
            if snapshot != state["snapshot"]:
                state["snapshot"] = snapshot
                emit({"type": "reply", "text": snapshot})

    async def _pump() -> None:
        try:
            async for event in session.subscribe_chat(session_id):
                if isinstance(event, dict):
                    handle(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    pump = asyncio.create_task(_pump())
    completion = "timeout"
    try:
        try:
            await asyncio.wait_for(session.chat_subscribed.wait(), 10)
        except asyncio.TimeoutError:
            pass
        bundle_deadline = time.monotonic() + 8
        while time.monotonic() < bundle_deadline:
            if BUNDLE_EVENTS <= state["bundle"]:
                break
            await asyncio.sleep(0.05)
        state["send_issued"] = True
        await send_voice_message(session, session_id, wav_bytes)
        deadline = time.monotonic() + timeout_s
        while True:
            now = time.monotonic()
            replies = state["replies"]
            if state["terminal_at"] is not None and replies:
                completion = "turn_complete"
                break
            if state["terminal_at"] is not None \
                    and now - state["terminal_at"] >= 1.0:
                completion = "turn_complete"
                break
            if (replies
                    and all(r["done"] for r in replies.values())
                    and state["last_reply_at"] is not None
                    and now - state["last_reply_at"] >= QUIET_GRACE_S):
                completion = "reply_done_quiet"
                break
            if now >= deadline:
                break
            await asyncio.sleep(0.25)
    finally:
        pump.cancel()
        try:
            # Bounded cleanup, as in ask.ask: never hang on teardown.
            await asyncio.wait_for(
                asyncio.gather(pump, return_exceptions=True), 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    reply_ids = list(state["replies"])
    return {"ok": bool(state["snapshot"]),
            "reply_text": state["snapshot"],
            "reply_message_id": reply_ids[-1] if reply_ids else None,
            "task_status": state["task_status"],
            "completion": completion}


def run_voice_turn_streaming(session_id, wav_bytes, emit):
    """Run one voice turn, calling emit(payload) for each event."""
    async def _go():
        async with link.open_session() as session:
            result = await follow_voice_turn(
                session, session_id, wav_bytes, emit)
            emit({"type": "done", "completion": result["completion"],
                  "task_status": result["task_status"],
                  "reply_message_id": result["reply_message_id"],
                  "ok": result["ok"]})
            return result
    return asyncio.run(_go())


VOICE_TYPES = {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}


def make_handler(index: ChatIndex, gui_dir=None, bound_host="127.0.0.1",
                 extra_hosts=()):
    from pathlib import Path

    hosts = allowed_hosts(bound_host, extra_hosts)

    gui_root = Path(gui_dir).resolve() if gui_dir else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the console clean
            pass

        # -- helpers -------------------------------------------------
        def _cors(self):
            allowed = cors_origin(self.headers.get("Origin"))
            if allowed:
                self.send_header("Access-Control-Allow-Origin", allowed)
                self.send_header("Vary", "Origin")

        def _guard(self) -> bool:
            """Refuse requests a web page could have forged."""
            why = refusal(self.headers, hosts)
            if why:
                self._json({"error": why}, 403)
                return False
            return True

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode() or "{}")
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}

        def _resolve(self, chat_id):
            chat = index.get(chat_id)
            if chat is None:
                self._json({"error": "unknown chat"}, 404)
            return chat

        # -- routing ---------------------------------------------------
        def do_OPTIONS(self):
            if not self._guard():
                return
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _static(self, raw_path: str) -> bool:
            if gui_root is None:
                return False
            rel = urllib.parse.unquote(raw_path).lstrip("/")
            target = (gui_root / rel) if rel else (gui_root / "index.html")
            if not target.is_file() or \
                    gui_root not in target.resolve().parents:
                target = gui_root / "index.html"  # SPA fallback
            if not target.is_file():
                return False
            import mimetypes

            body = target.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(target.name)[0]
                or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        def do_GET(self):
            if not self._guard():
                return
            parsed = urllib.parse.urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if not parts or parts[0] != "api":
                if self._static(parsed.path):
                    return
                self._json({"error": "not found"}, 404)
                return
            if parts == ["api", "status"]:
                try:
                    self._json(asyncio.run(link.doctor()))
                except Exception as exc:
                    self._json({"paired": False,
                                "error": f"{type(exc).__name__}: {exc}"})
            elif parts == ["api", "chats"]:
                self._json({"chats": [chat_public(c)
                                      for c in index.list()]})
            elif len(parts) == 4 and parts[:2] == ["api", "chats"] \
                    and parts[3] == "history":
                chat = self._resolve(parts[2])
                if chat is None:
                    return
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int((query.get("limit") or ["30"])[0])
                except ValueError:
                    limit = 30
                try:
                    messages, has_more = fetch_history_blocking(
                        chat.get("session_id"), limit)
                except Exception as exc:
                    self._json({"error": f"{type(exc).__name__}: {exc}"},
                               502)
                    return
                self._json({"messages": [message_public(m)
                                         for m in messages],
                            "has_more": has_more})
            elif len(parts) == 5 and parts[:2] == ["api", "chats"] \
                    and parts[3] == "tts":
                chat = self._resolve(parts[2])
                if chat is None:
                    return
                message_id = urllib.parse.unquote(parts[4])
                try:
                    audio = fetch_tts_blocking(message_id)
                except Exception as exc:
                    self._json({"error": f"{type(exc).__name__}: {exc}"},
                               502)
                    return
                if not audio:
                    self._json({"error": "no audio for that message"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(audio)))
                self._cors()
                self.end_headers()
                self.wfile.write(audio)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._guard():
                return
            parsed = urllib.parse.urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            content_type = self.headers.get("Content-Type")
            is_voice = len(parts) == 4 and parts[3] == "voice"
            if is_voice:
                base = (content_type or "").split(";", 1)[0].strip().lower()
                if base not in VOICE_TYPES:
                    self._json({"error": "send audio/wav"}, 415)
                    return
            elif not is_json(content_type):
                self._json({"error": "send application/json"}, 415)
                return
            if parts == ["api", "chats"]:
                label = str(self._body().get("label") or "New chat")
                self._json(chat_public(index.create(label)), 201)
            elif parts == ["api", "chats", "add"]:
                body = self._body()
                session_id = str(body.get("session_id") or "")
                if not session_id:
                    self._json({"error": "session_id required"}, 400)
                    return
                chat = index.add_known(session_id,
                                       str(body.get("label") or "Chat"))
                self._json(chat_public(chat), 201)
            elif len(parts) == 4 and parts[:2] == ["api", "chats"] \
                    and parts[3] == "rename":
                chat = self._resolve(parts[2])
                if chat is None:
                    return
                label = str(self._body().get("label") or chat["label"])
                index.rename(chat["id"], label)
                self._json(chat_public(index.get(chat["id"])))
            elif len(parts) == 4 and parts[:2] == ["api", "chats"] \
                    and parts[3] == "messages":
                chat = self._resolve(parts[2])
                if chat is None:
                    return
                text = str(self._body().get("text") or "")
                if not text:
                    self._json({"error": "text required"}, 400)
                    return
                self._stream_turn(chat, text=text)
            elif len(parts) == 4 and parts[:2] == ["api", "chats"] \
                    and parts[3] == "voice":
                chat = self._resolve(parts[2])
                if chat is None:
                    return
                length = int(self.headers.get("Content-Length") or 0)
                wav = self.rfile.read(length) if length else b""
                if not wav:
                    self._json({"error": "empty voice message"}, 400)
                    return
                self._stream_turn(chat, wav_bytes=wav)
            else:
                self._json({"error": "not found"}, 404)

        def _stream_turn(self, chat, text=None, wav_bytes=None):
            events: queue.Queue = queue.Queue()
            result_box: dict = {}

            def emit(payload):
                events.put(payload)

            def worker():
                try:
                    if wav_bytes is not None:
                        result_box["result"] = run_voice_turn_streaming(
                            chat.get("session_id"), wav_bytes, emit)
                    else:
                        result_box["result"] = run_turn_streaming(
                            chat.get("session_id"), text, emit)
                except Exception as exc:
                    emit({"type": "error",
                          "error": f"{type(exc).__name__}: {exc}"})
                finally:
                    events.put(None)

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self._cors()
            self.close_connection = True
            self.end_headers()
            while True:
                try:
                    payload = events.get(timeout=30)
                except queue.Empty:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    continue
                if payload is None:
                    return
                try:
                    self.wfile.write(sse_pack(payload))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

    return Handler


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          index: ChatIndex | None = None, gui_dir=None, allow_hosts=()):
    server = ThreadingHTTPServer(
        (host, port), make_handler(index or ChatIndex(), gui_dir,
                                   bound_host=host, extra_hosts=allow_hosts))
    print(f"muse-linux native API on http://{host}:{port}/api")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
