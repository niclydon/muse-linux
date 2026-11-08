"""Full-screen terminal UI: the Muse app layout in a terminal.

A Textual app in the style of the modern coder CLIs and the chat
clients before them: a chat sidebar on the left, the conversation in
the middle, a composer pinned to the bottom, live working status in
the header while Muse is on a turn, and replies rendered as
Markdown as they stream.

Keys:
    enter        send the composed message
    ctrl+n       new side chat
    ctrl+t       toggle light / dark theme
    ctrl+r       reload the current chat's history
    tab          move focus between sidebar and composer
    ctrl+q       quit

The two backend seams (_fetch_history, _run_turn) are module-level
so tests can drive the app headless with fakes.
"""

from __future__ import annotations

import asyncio

from .index import ChatIndex

# ------------------------------------------------------------------
# Backend seams
# ------------------------------------------------------------------


async def _fetch_history(session_id, limit=40):
    from .history import fetch_history
    from .link import open_session

    async with open_session() as session:
        messages, _has_more = await fetch_history(session, session_id, limit)
    return messages


async def _run_turn(session_id, text, on_status, on_reply, on_approvals):
    from .ask import ask
    from .link import open_session

    async with open_session() as session:
        return await ask(session, session_id, text, on_status=on_status,
                         on_reply=on_reply, on_approvals=on_approvals)


# ------------------------------------------------------------------
# The app
# ------------------------------------------------------------------


def run_tui(index: ChatIndex | None = None) -> None:
    from textual.app import App

    app = _build_app(index or ChatIndex())
    app.run()


def _build_app(index: ChatIndex):
    from rich.text import Text
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Input, Label, ListItem, ListView, \
        Markdown, Static

    class ChatItem(ListItem):
        def __init__(self, chat: dict) -> None:
            super().__init__()
            self.chat = chat

        def compose(self) -> ComposeResult:
            label = self.chat["label"]
            if self.chat["id"] == "main":
                label += "  · main"
            yield Label(label)

    class MuseTui(App):
        TITLE = "Muse for Linux"
        SUB_TITLE = "unofficial terminal client"

        CSS = """
        Screen { layout: horizontal; }
        #sidebar {
            width: 30; min-width: 24;
            border-right: solid $primary;
            background: $panel;
        }
        #sidebar-title { padding: 1 1 0 1; text-style: bold; }
        #sidebar-sub { padding: 0 1 1 1; color: $text-muted; }
        #chat-list { height: 1fr; background: $panel; }
        #conn { padding: 1; color: $text-muted; border-top: solid $primary; }
        #main { width: 1fr; }
        #chat-header {
            height: 3; padding: 0 1;
            border-bottom: solid $primary;
            background: $panel;
        }
        #chat-name { text-style: bold; }
        #status { color: $warning; }
        #approvals {
            display: none; height: auto; max-height: 6;
            margin: 1 1 0 1; padding: 1;
            border: solid $warning; background: $panel;
        }
        #messages { height: 1fr; padding: 0 1; }
        .msg { height: auto; margin: 1 0 0 0; }
        .msg-user {
            background: $primary 15%;
            border-left: thick $primary;
            padding: 0 1;
        }
        .msg-user-label { color: $primary; text-style: bold; }
        .msg-assistant { padding: 0 1; }
        .msg-assistant-label { color: $text-muted; text-style: bold; }
        #composer-row {
            height: 3; padding: 0 1;
            border-top: solid $primary; background: $panel;
        }
        #composer { width: 1fr; }
        """

        BINDINGS = [
            ("ctrl+q", "quit", "Quit"),
            ("ctrl+n", "new_chat", "New chat"),
            ("ctrl+t", "toggle_theme", "Theme"),
            ("ctrl+r", "reload_history", "Reload"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.index = index
            self.current: dict = index.list()[0]
            self.sending = False

        # -- layout ------------------------------------------------
        def compose(self) -> ComposeResult:
            with Horizontal():
                with Vertical(id="sidebar"):
                    yield Static("Muse for Linux", id="sidebar-title")
                    yield Static("unofficial terminal client",
                                 id="sidebar-sub")
                    yield ListView(
                        *[ChatItem(c) for c in self.index.list()],
                        id="chat-list")
                    yield Static("connecting...", id="conn")
                with Vertical(id="main"):
                    with Vertical(id="chat-header"):
                        yield Static(self.current["label"], id="chat-name")
                        yield Static("", id="status")
                    yield Static("", id="approvals")
                    yield VerticalScroll(id="messages")
                    with Horizontal(id="composer-row"):
                        yield Input(
                            placeholder="Message Muse...  "
                                        "(enter to send)",
                            id="composer")
            yield Footer()

        async def on_mount(self) -> None:
            self.theme = "textual-dark"
            self.query_one("#composer", Input).focus()
            self.load_history()
            self.check_connection()

        # -- backend workers ----------------------------------------
        @work(exclusive=False, group="conn")
        async def check_connection(self) -> None:
            from .link import doctor

            try:
                info = await doctor()
                text = ("paired · 1 VM" if info.get("vm_count") == 1
                        else f"paired · {info.get('vm_count')} VMs") \
                    if info.get("paired") else "not paired"
            except Exception as exc:
                text = f"backend error: {type(exc).__name__}"
            self.query_one("#conn", Static).update(text)

        @work(exclusive=True, group="history")
        async def load_history(self) -> None:
            pane = self.query_one("#messages", VerticalScroll)
            await pane.remove_children()
            try:
                messages = await _fetch_history(
                    self.current.get("session_id"))
            except Exception as exc:
                await pane.mount(Static(
                    Text(f"history unavailable: {exc}", style="red"),
                    classes="msg"))
                return
            for message in messages:
                await pane.mount(self.render_message(
                    message.role, message.text))
            pane.scroll_end(animate=False)

        @work(exclusive=True, group="turn")
        async def send_turn(self, text: str) -> None:
            pane = self.query_one("#messages", VerticalScroll)
            status = self.query_one("#status", Static)
            reply_widget = Markdown("", classes="msg msg-assistant")
            await pane.mount(reply_widget)
            pane.scroll_end(animate=False)

            def on_status(code: str) -> None:
                status.update(f"● Muse is {code}")

            def on_reply(snapshot: str) -> None:
                reply_widget.update(_assistant_md(snapshot))
                pane.scroll_end(animate=False)

            def on_approvals(data: dict) -> None:
                if not data:
                    return
                box = self.query_one("#approvals", Static)
                box.update("Approval state: "
                           + str(data)[:400])
                box.styles.display = "block"

            self.sending = True
            try:
                result = await _run_turn(
                    self.current.get("session_id"), text,
                    on_status, on_reply, on_approvals)
                if not result.ok and not result.reply_text:
                    reply_widget.update(
                        "_No reply received ("
                        + result.completion + ")._")
            finally:
                self.sending = False
                status.update("")

        # -- rendering ------------------------------------------------
        def render_message(self, role: str, text: str):
            if role == "user":
                body = Vertical(
                    Static("you", classes="msg-user-label"),
                    Static(text),
                    classes="msg msg-user")
                return body
            return Markdown(_assistant_md(text),
                            classes="msg msg-assistant")

        def select_chat(self, chat: dict) -> None:
            ids = [c["id"] for c in self.index.list()]
            if chat["id"] in ids:
                self.query_one("#chat-list", ListView).index = \
                    ids.index(chat["id"])
            if chat["id"] == self.current["id"]:
                return
            self.current = chat
            self.query_one("#chat-name", Static).update(chat["label"])
            self.query_one("#approvals", Static).styles.display = "none"
            self.load_history()

        # -- events ---------------------------------------------------
        @on(ListView.Selected, "#chat-list")
        def pick_chat(self, event: ListView.Selected) -> None:
            item = event.item
            if isinstance(item, ChatItem):
                self.select_chat(item.chat)

        @on(Input.Submitted, "#composer")
        def submit(self, event: Input.Submitted) -> None:
            text = event.value.strip()
            if not text or self.sending:
                return
            event.input.value = ""
            pane = self.query_one("#messages", VerticalScroll)
            pane.mount(self.render_message("user", text))
            pane.scroll_end(animate=False)
            self.send_turn(text)

        # -- actions ----------------------------------------------------
        def action_new_chat(self) -> None:
            chat = self.index.create("New chat")
            item = ChatItem(chat)
            self.query_one("#chat-list", ListView).append(item)
            self.current = chat
            self.query_one("#chat-name", Static).update(chat["label"])
            self.load_history()
            self.query_one("#composer", Input).focus()

        def action_toggle_theme(self) -> None:
            self.theme = ("textual-light" if self.theme == "textual-dark"
                          else "textual-dark")

        def action_reload_history(self) -> None:
            self.load_history()

    return MuseTui()


def _assistant_md(text: str) -> str:
    return "**Muse**\n\n" + text if text else "**Muse**\n\n..."
