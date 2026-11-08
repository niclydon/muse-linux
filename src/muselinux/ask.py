"""One conversational turn: subscribe, send, follow the reply.

Ordering is the whole correctness story here, and it follows the flow
proven in production Link tooling:

1. Subscribe to the chat's event stream first.
2. Wait for the subscribe-time state bundle (the first agent.status,
   task.status and approvals.snapshot) to finish arriving. The bundle
   restates the PREVIOUS turn; its parentless terminal task.status is
   indistinguishable from the new turn's and would end a fresh turn
   vacuously if it were fed after the send.
3. Only then send, acknowledge the turn with the send response, and
   follow events until the reply is done and the turn settles, the
   reply is done and the turn has been quiet for a grace period, or
   the timeout elapses.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

BUNDLE_EVENTS = {"agent.status", "task.status", "approvals.snapshot"}
QUIET_GRACE_S = 8.0


@dataclass
class AskResult:
    ok: bool
    reply_text: str = ""
    reply_message_id: str | None = None
    send_message_id: str | None = None
    agent_statuses: list[str] = field(default_factory=list)
    task_status: str | None = None
    completion: str = "not_started"   # turn_complete | reply_done_quiet | timeout
    error: str | None = None


async def ask(session, session_id: str | None, text: str,
              timeout_s: float = 180,
              on_status: Callable[[str], None] | None = None,
              on_reply: Callable[[str], None] | None = None,
              on_approvals: Callable[[dict], None] | None = None,
              sender: Callable | None = None
              ) -> AskResult:
    """Run one turn on an open session.

    ``session_id`` targets a side chat; None targets the main chat.
    ``on_status`` receives each agent activity code as it is seen;
    ``on_reply`` receives the reply text snapshot whenever it grows;
    ``on_approvals`` receives each post-send approvals snapshot
    payload, for front ends that render approval requests.
    ``sender`` overrides the send step: an async callable taking the
    session and returning the send result dict, used for voice
    messages (send_voice) instead of send_chat.
    """
    from musegadget.link_client import acknowledgement
    from musegadget.muse_turn import MuseTurn

    turn = MuseTurn(session_id)
    raw_events: list[dict] = []
    send_issued = False
    bundle_seen: set[str] = set()
    last_snapshot = ""
    seen_codes: set[str] = set()

    async def _pump() -> None:
        nonlocal last_snapshot
        try:
            async for event in session.subscribe_chat(session_id):
                raw_events.append(event)
                name = event.get("event")
                if name in BUNDLE_EVENTS:
                    bundle_seen.add(name)
                if not send_issued:
                    continue  # pre-send events never belong to this turn
                if name == "approvals.snapshot" and on_approvals:
                    payload = event.get("payload")
                    on_approvals(payload if isinstance(payload, dict)
                                 else {})
                if on_status and name == "agent.status":
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        code = payload.get("activity_code")
                        if code and code not in seen_codes:
                            seen_codes.add(code)
                            on_status(code)
                try:
                    turn.feed(event)
                except Exception:
                    pass
                if on_reply and turn.replies:
                    snapshot = "\n\n".join(
                        r.text for r in turn.replies.values() if r.text)
                    if snapshot != last_snapshot:
                        last_snapshot = snapshot
                        on_reply(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    pump = asyncio.create_task(_pump())
    completion = "timeout"
    send_result: dict = {}
    try:
        try:
            await asyncio.wait_for(session.chat_subscribed.wait(), 10)
        except asyncio.TimeoutError:
            pass
        bundle_deadline = time.monotonic() + 8
        while time.monotonic() < bundle_deadline:
            if BUNDLE_EVENTS <= bundle_seen:
                break
            await asyncio.sleep(0.05)
        send_issued = True
        if sender is not None:
            send_result = await sender(session) or {}
        elif session_id:
            send_result = await session.send_chat(text, session_id=session_id)
        else:
            send_result = await session.send_chat(text)
        try:
            turn.acknowledge(acknowledgement(send_result))
        except Exception:
            pass
        deadline = time.monotonic() + timeout_s
        while True:
            now = time.monotonic()
            if (turn.acknowledged and turn.complete(now)
                    and (turn.replies or turn.task_finished)):
                completion = "turn_complete"
                break
            if (turn.replies
                    and all(r.done for r in turn.replies.values())
                    and now - turn.last_activity >= QUIET_GRACE_S):
                completion = "reply_done_quiet"
                break
            if now >= deadline:
                break
            await asyncio.sleep(0.25)
    finally:
        pump.cancel()
        try:
            # Bounded: closing a subscription stream can stall on its
            # reset path; a client must never hang on cleanup.
            await asyncio.wait_for(
                asyncio.gather(pump, return_exceptions=True), 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    statuses: list[str] = []
    task_status = None
    for raw in raw_events:
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            continue
        if raw.get("event") == "agent.status":
            code = payload.get("activity_code")
            if code and code not in statuses:
                statuses.append(code)
        if raw.get("event") == "task.status" and payload.get("status"):
            task_status = payload["status"]
    replies = list(turn.replies.values())
    reply_text = "\n\n".join(r.text for r in replies if r.text)
    send_message_id = None
    response = send_result.get("response")
    if isinstance(response, dict):
        inner = response.get("result")
        if isinstance(inner, dict) and inner.get("message_id"):
            send_message_id = inner["message_id"]
        else:
            send_message_id = response.get("message_id")
    return AskResult(
        ok=bool(reply_text),
        reply_text=reply_text,
        reply_message_id=replies[-1].message_id if replies else None,
        send_message_id=send_message_id,
        agent_statuses=statuses,
        task_status=task_status,
        completion=completion,
    )
