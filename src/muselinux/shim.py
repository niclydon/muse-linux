"""An OpenAI-compatible shim in front of the Muse backend.

Purpose: any chat front end that can be pointed at a custom
OpenAI-compatible endpoint (Jan, LibreChat, Open WebUI and many others)
can talk to Muse through this shim with no code changes of its own.

Endpoints:
  GET  /v1/models             one model per known chat
  POST /v1/chat/completions   stream or non-stream

Model ids encode the target chat, because Chat Completions is
stateless and has no conversation field: ``muse`` is the main chat
and ``muse-chat-<session-id>`` is a side chat. The backend owns the
conversation history, so the shim sends only the last user message of
each request; earlier messages in the request are the client's local
transcript, not something to replay.

Known limits of the shim shape (see docs/ARCHITECTURE.md): working
status, task state and approvals have no representation in Chat
Completions and are flattened away; clients that integrate the
``muselinux`` library directly get those events natively.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .index import ChatIndex
from .localguard import allowed_hosts, host_allowed, is_json, origin_allowed

MODEL_PREFIX = "muse-chat-"


def models_for_index(index: ChatIndex) -> list[dict]:
    models = [{"id": "muse", "object": "model",
               "created": 0, "owned_by": "muse-linux"}]
    for chat in index.list():
        if chat.get("session_id"):
            models.append({"id": MODEL_PREFIX + chat["session_id"],
                           "object": "model", "created": 0,
                           "owned_by": "muse-linux"})
    return models


def session_for_model(model: str | None) -> str | None:
    if model and model.startswith(MODEL_PREFIX):
        return model[len(MODEL_PREFIX):]
    return None


def last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [part.get("text", "") for part in content
                     if isinstance(part, dict)
                     and part.get("type") in ("text", "input_text")]
            return "\n".join(p for p in parts if p)
    return ""


async def _completion(session_id: str | None, text: str,
                      on_reply=None) -> str:
    from .ask import ask
    from .link import open_session

    async with open_session() as session:
        result = await ask(session, session_id, text,
                           on_reply=on_reply)
    if result.error:
        raise RuntimeError(result.error)
    return result.reply_text


def run_completion(session_id: str | None, text: str,
                   on_reply=None) -> str:
    return asyncio.run(_completion(session_id, text, on_reply))


def make_handler(index: ChatIndex | None = None, bound_host="127.0.0.1",
                 extra_hosts=()):
    index = index or ChatIndex()
    hosts = allowed_hosts(bound_host, extra_hosts)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the terminal clean
            pass

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _guard(self) -> bool:
            """Refuse requests a web page could have forged (see
            muselinux.localguard)."""
            if not host_allowed(self.headers.get("Host"), hosts) or \
                    not origin_allowed(self.headers.get("Origin"), hosts):
                self._json(403, {"error": {"message": "forbidden"}})
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            if self.path.rstrip("/") in ("/v1/models", "/models"):
                self._json(200, {"object": "list",
                                 "data": models_for_index(index)})
            elif self.path in ("/", "/health"):
                self._json(200, {"ok": True, "service": "muse-linux-shim",
                                 "version": __version__})
            else:
                self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if not self._guard():
                return
            if not is_json(self.headers.get("Content-Type")):
                self._json(415, {"error": {
                    "message": "Content-Type must be application/json"}})
                return
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._json(404, {"error": {"message": "not found"}})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": {"message": "invalid JSON"}})
                return
            model = request.get("model") or "muse"
            session_id = session_for_model(model)
            text = last_user_text(request.get("messages") or [])
            if not text:
                self._json(400, {"error": {
                    "message": "no user message in request"}})
                return
            completion_id = "chatcmpl-" + uuid.uuid4().hex
            created = int(time.time())
            if request.get("stream"):
                self._stream(completion_id, created, model,
                             session_id, text)
            else:
                try:
                    reply = run_completion(session_id, text)
                except Exception as exc:
                    self._json(502, {"error": {"message":
                                               f"{type(exc).__name__}: {exc}"}})
                    return
                self._json(200, {
                    "id": completion_id, "object": "chat.completion",
                    "created": created, "model": model,
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant",
                                             "content": reply},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0}})

        def _stream(self, completion_id, created, model, session_id, text):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # No Content-Length is possible on a live stream; without
            # an explicit close some HTTP/1.1 clients wait past [DONE].
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            sent = {"text": ""}

            def chunk(delta: str, finish: str | None = None) -> None:
                payload = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0,
                                 "delta": ({"content": delta}
                                           if delta else {}),
                                 "finish_reason": finish}]}
                self.wfile.write(
                    f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()

            def on_reply(snapshot: str) -> None:
                if snapshot.startswith(sent["text"]):
                    delta = snapshot[len(sent["text"]):]
                else:
                    delta = snapshot
                if delta:
                    chunk(delta)
                sent["text"] = snapshot

            try:
                run_completion(session_id, text, on_reply=on_reply)
                chunk("", finish="stop")
            except Exception as exc:
                chunk(f"\n[error: {type(exc).__name__}: {exc}]",
                      finish="stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, allow_hosts=()) -> None:
    server = ThreadingHTTPServer(
        (host, port), make_handler(bound_host=host, extra_hosts=allow_hosts))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"muse-linux shim listening on http://{host}:{port}/v1")
    try:
        thread.join()
    except KeyboardInterrupt:
        server.shutdown()
