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

Message-stream events are JSON, one per SSE message:
    {"type": "status", "code": "working"}
    {"type": "reply", "text": "<snapshot so far>"}
    {"type": "approvals", "data": {...}}
    {"type": "done", "completion": "...", "task_status": "..."}
    {"type": "error", "error": "..."}

Same trust boundary as the shim: anyone who can reach the port can
act as the paired device. Localhost only, unless an authenticating
proxy sits in front.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import link
from .ask import ask
from .history import fetch_history
from .index import ChatIndex

DEFAULT_PORT = 8766


def chat_public(chat: dict) -> dict:
    return {"id": chat["id"], "label": chat["label"],
            "session_id": chat.get("session_id"),
            "is_main": chat["id"] == "main"}


def message_public(message) -> dict:
    return {"role": message.role, "text": message.text,
            "message_id": message.message_id,
            "occurred_at_ms": message.occurred_at_ms}


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
                on_approvals=lambda data: emit({"type": "approvals",
                                                "data": data}))
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


def make_handler(index: ChatIndex, gui_dir=None):
    from pathlib import Path

    gui_root = Path(gui_dir).resolve() if gui_dir else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the console clean
            pass

        # -- helpers -------------------------------------------------
        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
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
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
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
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
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
                self._json(chat_public(index.rename(chat["id"], label)))
            elif len(parts) == 4 and parts[:2] == ["api", "chats"] \
                    and parts[3] == "messages":
                chat = self._resolve(parts[2])
                if chat is None:
                    return
                text = str(self._body().get("text") or "")
                if not text:
                    self._json({"error": "text required"}, 400)
                    return
                self._stream_turn(chat, text)
            else:
                self._json({"error": "not found"}, 404)

        def _stream_turn(self, chat, text):
            events: queue.Queue = queue.Queue()
            result_box: dict = {}

            def emit(payload):
                events.put(payload)

            def worker():
                try:
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
            self.send_header("Access-Control-Allow-Origin", "*")
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
          index: ChatIndex | None = None, gui_dir=None):
    server = ThreadingHTTPServer(
        (host, port), make_handler(index or ChatIndex(), gui_dir))
    print(f"muse-linux native API on http://{host}:{port}/api")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
