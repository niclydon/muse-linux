#!/usr/bin/env python3
"""Run the real daemon against an in-memory Muse, for screenshots and
offline UI work. Nothing here touches a pairing, the network or a real
chat: every message and reply is made up below.

    python3 tools/demo_daemon.py --socket /tmp/muse-demo.sock [--approval]

Point a client at it with MUSE_LINUX_SOCKET=/tmp/muse-demo.sock.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from muselinux.daemon import Daemon  # noqa: E402
from muselinux.index import ChatIndex  # noqa: E402
from muselinux.testing import FakeMuse, fake_doctor  # noqa: E402

DEMO_REPLY = """Here's a plan for **Saturday**:

1. Leave by *8:30* to beat the crowds at the trailhead
2. Take the coast loop, about 9 km
3. Lunch at the picnic area by the lighthouse

If it rains, swap in the museum. A packing list:

- water and snacks
- a light rain jacket
- `sunscreen`, because the coast is exposed

```text
Trailhead  08:30
Lighthouse 11:45
Back       14:00
```"""

SIDE_CHATS = [
    ("Weekend hike", [
        ("user", "Can you plan a relaxed coastal hike for Saturday?"),
        ("assistant", "Happy to. How long do you want to be out, and do "
                      "you want somewhere to stop for lunch?"),
    ]),
    ("Sourdough experiments", [
        ("user", "My starter smells like nail polish remover. Is it dead?"),
        ("assistant", "Not at all. That smell means it is **hungry**. Feed "
                      "it twice a day at a 1:1:1 ratio for a few days and "
                      "the smell will settle."),
    ]),
    ("Python packaging notes", [
        ("user", "What's the difference between `pip install -e .` and a "
                 "normal install?"),
        ("assistant", "An editable install points Python at your source "
                      "tree, so code changes show up without reinstalling."),
    ]),
]


def reply_for(text: str) -> str:
    lower = text.lower()
    if "saturday" in lower or "plan" in lower:
        return DEMO_REPLY
    if "smoke test" in lower:
        return "Smoke test received."
    return f"Got it: “{text}”. Anything else?"


def seed(muse: FakeMuse, index: ChatIndex) -> None:
    muse.add_history(None, "user", "Good morning! What's on my plate today?")
    muse.add_history(None, "assistant",
                     "Morning! You have a quiet day: one call at 2 PM and "
                     "a reminder to water the plants.")
    now = int(time.time() * 1000)
    for n, (label, turns) in enumerate(SIDE_CHATS):
        chat = index.create(label)
        for role, text in turns:
            muse.add_history(chat["session_id"], role, text)
        index.touch(chat["id"], now - (n + 1) * 3_600_000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/tmp/muse-demo.sock")
    parser.add_argument("--approval", action="store_true",
                        help="replies ask for an approval while they run")
    parser.add_argument("--step", type=float, default=0.06,
                        help="seconds between streamed words")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    home = Path(tempfile.mkdtemp(prefix="muse-demo-"))
    index = ChatIndex(home / "chats.json")
    muse = FakeMuse(reply_for, step_s=args.step,
                    activity="Checking the weather and trail conditions")
    if args.approval:
        muse.approvals = [{"id": "ap-demo", "title": "Add an event to your calendar",
                           "detail": "Saturday 08:30, Coastal hike"}]
    seed(muse, index)
    daemon = Daemon(args.socket, index, connect=muse.connect,
                    doctor=fake_doctor, socket_mode=0o600)

    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await daemon.run(stop)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
