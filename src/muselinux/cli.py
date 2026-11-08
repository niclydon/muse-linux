"""muse-linux command line: doctor, chats, history, ask, chat, probe, shim."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__
from .index import ChatIndex


def _resolve(index: ChatIndex, ref: str) -> dict:
    chat = index.get(ref)
    if chat is None:
        raise SystemExit(f"unknown chat: {ref!r} (see: muse-linux chats)")
    return chat


async def _history(args) -> int:
    from .history import fetch_history
    from .link import open_session

    index = ChatIndex()
    chat = _resolve(index, args.chat)
    async with open_session() as session:
        messages, has_more = await fetch_history(
            session, chat["session_id"], args.limit)
    for message in messages:
        speaker = "you" if message.role == "user" else "muse"
        print(f"{speaker}: {message.text}")
    if has_more:
        print("[older history exists beyond this window]")
    return 0


async def _ask_once(args) -> int:
    from .ask import ask
    from .link import open_session

    index = ChatIndex()
    chat = _resolve(index, args.chat)
    printed = {"n": 0}

    def on_reply(snapshot: str) -> None:
        sys.stdout.write(snapshot[printed["n"]:])
        sys.stdout.flush()
        printed["n"] = len(snapshot)

    def on_status(code: str) -> None:
        print(f"\n[working: {code}]", file=sys.stderr)

    async with open_session() as session:
        result = await ask(session, chat["session_id"], args.text,
                           on_status=on_status, on_reply=on_reply)
    print()
    if result.task_status:
        print(f"[task: {result.task_status}]", file=sys.stderr)
    return 0 if result.ok else 1


async def _repl(args) -> int:
    from .ask import ask
    from .link import open_session

    index = ChatIndex()
    current = _resolve(index, args.chat)
    print(f"muse-linux {__version__} - chatting in {current['label']!r}. "
          "Commands: /chats /new <label> /switch <id-or-label> /quit")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line == "/quit":
            return 0
        if line == "/chats":
            for chat in index.list():
                marker = "*" if chat["id"] == current["id"] else " "
                print(f"{marker} {chat['id']}  {chat['label']}")
            continue
        if line.startswith("/new "):
            current = index.create(line[5:].strip() or "Untitled chat")
            print(f"new chat: {current['id']} ({current['label']})")
            continue
        if line.startswith("/switch "):
            ref = line[8:].strip()
            target = index.get(ref) or next(
                (c for c in index.list() if c["label"] == ref), None)
            if target is None:
                print(f"unknown chat: {ref}")
            else:
                current = target
                print(f"switched to {current['label']}")
            continue
        printed = {"n": 0}

        def on_reply(snapshot: str) -> None:
            sys.stdout.write(snapshot[printed["n"]:])
            sys.stdout.flush()
            printed["n"] = len(snapshot)

        async with open_session() as session:
            result = await ask(session, current["session_id"], line,
                               on_reply=on_reply)
        print()
        if not result.ok:
            print("[no reply received]", file=sys.stderr)


async def _probe(args) -> int:
    from .link import open_session
    from .probe import run_probes

    index = ChatIndex()
    side = next((c for c in index.list() if c.get("session_id")), None)
    async with open_session() as session:
        results = await run_probes(
            session, side["session_id"] if side else None)
    for row in results:
        print(f"{row['probe']:<18} {row.get('status')!s:<6} "
              f"{row['classification']:<16} {row['path']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="muse-linux",
        description="Unofficial Linux client for Meta Muse")
    parser.add_argument("--version", action="version",
                        version=f"muse-linux {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="pairing and VM reachability check")

    chats = sub.add_parser("chats", help="list chats in the local index")
    chats_sub = chats.add_subparsers(dest="chats_command")
    chats_sub.add_parser("list")
    add = chats_sub.add_parser("add", help="add a known side chat id")
    add.add_argument("session_id")
    add.add_argument("label")
    new = chats_sub.add_parser("new", help="create a side chat entry")
    new.add_argument("label")
    rename = chats_sub.add_parser("rename")
    rename.add_argument("chat")
    rename.add_argument("label")

    history = sub.add_parser("history", help="print a chat's history")
    history.add_argument("chat", nargs="?", default="main")
    history.add_argument("--limit", type=int, default=20)

    ask = sub.add_parser("ask", help="send one message and print the reply")
    ask.add_argument("text")
    ask.add_argument("--chat", default="main")

    chat = sub.add_parser("chat", help="interactive terminal chat")
    chat.add_argument("--chat", default="main")

    sub.add_parser("probe", help="run the parity-ceiling probes")

    shim = sub.add_parser("shim", help="serve the OpenAI-compatible shim")
    shim.add_argument("--host", default="127.0.0.1")
    shim.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)

    if args.command == "doctor":
        from .link import doctor
        print(json.dumps(asyncio.run(doctor()), indent=2))
        return 0
    if args.command == "chats":
        index = ChatIndex()
        if args.chats_command == "add":
            chat = index.add_known(args.session_id, args.label)
            print(f"added {chat['id']} ({chat['label']})")
        elif args.chats_command == "new":
            chat = index.create(args.label)
            print(f"created {chat['id']} ({chat['label']}); the chat "
                  "itself starts on its first message")
        elif args.chats_command == "rename":
            if not index.rename(args.chat, args.label):
                raise SystemExit(f"unknown chat: {args.chat}")
            print("renamed")
        else:
            for chat in index.list():
                print(f"{chat['id']:<40} {chat['label']}")
        return 0
    if args.command == "history":
        return asyncio.run(_history(args))
    if args.command == "ask":
        return asyncio.run(_ask_once(args))
    if args.command == "chat":
        return asyncio.run(_repl(args))
    if args.command == "probe":
        return asyncio.run(_probe(args))
    if args.command == "shim":
        from .shim import serve
        serve(args.host, args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
