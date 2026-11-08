"""Full-screen terminal UI: the Muse app layout in a terminal.

A Textual app in the style of the modern coder CLIs and the chat
clients before them: a chat sidebar on the left, the conversation in
the middle, a composer pinned to the bottom, live working status in
the header while Muse is on a turn, and replies rendered as
Markdown as they stream.

Keys:
    enter            newline in the composer
    ctrl+enter       send the composed message (also ctrl+s)
    tab              toggle focus between the chat list and composer
    ctrl+1           focus the chat list
    ctrl+2           focus the composer
    ctrl+k           filter the chat list
    ctrl+o           load older history
    ctrl+n           new side chat
    ctrl+t           toggle light / dark theme
    ctrl+r           reload the current chat's history
    ctrl+q           quit

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
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Button, Footer, Input, Label, ListItem, \
        ListView, Markdown, Static, TextArea

    from .approvals import format_pending, parse_snapshot
    from .history import MAX_HISTORY_LIMIT

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
        #chat-filter { margin: 0 1 1 1; }
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
        #load-older { width: 100%; margin: 1 0 0 0; }
        .msg { height: auto; margin: 1 0 0 0; }
        .msg-user {
            background: $primary 15%;
            border-left: thick $primary;
            padding: 0 1;
        }
        .msg-user-label { color: $primary; text-style: bold; }
        .msg-reaction { color: $text-muted; }
        .msg-assistant { padding: 0 1; }
        .msg-assistant-label { color: $text-muted; text-style: bold; }
        #composer-area {
            height: 6; padding: 0 1;
            border-top: solid $primary; background: $panel;
        }
        #composer { height: 4; width: 1fr; }
        #composer-hint { height: 1; color: $text-muted; }
        """

        BINDINGS = [
            # Priority: the tab toggle must win over the focused
            # widget (TextArea indents on tab, the messages scroll
            # pane is a tab stop), and ctrl+k must win over the
            # composer's own ctrl+k editing shortcut.
            Binding("tab", "cycle_focus", "Switch pane", priority=True),
            Binding("ctrl+k", "focus_filter", "Filter", priority=True),
            ("ctrl+1", "focus_list", "Chat list"),
            ("ctrl+2", "focus_composer", "Composer"),
            Binding("ctrl+enter", "send_message", "Send"),
            Binding("ctrl+s", "send_message", "Send", show=False),
            ("ctrl+o", "load_older", "Load older"),
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
            self.history_limit = 40

        # -- layout ------------------------------------------------
        def compose(self) -> ComposeResult:
            with Horizontal():
                with Vertical(id="sidebar"):
                    yield Static("Muse for Linux", id="sidebar-title")
                    yield Static("unofficial terminal client",
                                 id="sidebar-sub")
                    yield Input(placeholder="Filter chats...",
                                id="chat-filter")
                    yield ListView(
                        *[ChatItem(c) for c in self.index.list()],
                        id="chat-list")
                    yield Static("connecting...", id="conn")
                with Vertical(id="main"):
                    with Vertical(id="chat-header"):
                        yield Static(self.current["label"], id="chat-name")
                        yield Static("", id="status")
                    yield Static("", id="approvals")
                    # Non-interactive containers stay out of the tab
                    # chain (Vertical, Horizontal and Static already
                    # default to can_focus=False); the scroll pane is
                    # the one container that would take a tab stop.
                    messages = VerticalScroll(id="messages")
                    messages.can_focus = False
                    yield messages
                    with Vertical(id="composer-area"):
                        yield TextArea(placeholder="Message Muse...",
                                       id="composer")
                        yield Static(
                            "enter: newline · ctrl+enter / ctrl+s: send",
                            id="composer-hint")
            yield Footer()

        async def on_mount(self) -> None:
            self.theme = "textual-dark"
            self.query_one("#composer", TextArea).focus()
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
                    self.current.get("session_id"), self.history_limit)
            except Exception as exc:
                await pane.mount(Static(
                    Text(f"history unavailable: {exc}", style="red"),
                    classes="msg"))
                return
            if len(messages) == self.history_limit \
                    and self.history_limit < MAX_HISTORY_LIMIT:
                await pane.mount(Button("Load older messages",
                                        id="load-older"))
            for message in messages:
                await pane.mount(self.render_message(
                    message.role, message.text, message.reaction))
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
                summary = format_pending(parse_snapshot(data))
                if not summary:
                    return
                box = self.query_one("#approvals", Static)
                box.update(summary)
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
        def render_message(self, role: str, text: str,
                           reaction: str | None = None):
            if role == "user":
                children = [
                    Static("you", classes="msg-user-label"),
                    Static(text),
                ]
                if reaction:
                    children.append(
                        Static(reaction, classes="msg-reaction"))
                return Vertical(*children, classes="msg msg-user")
            markdown = _assistant_md(text)
            if reaction:
                markdown += f"\n\n{reaction}"
            return Markdown(markdown, classes="msg msg-assistant")

        def select_chat(self, chat: dict) -> None:
            ids = [c["id"] for c in self.index.list()]
            if chat["id"] in ids:
                self.query_one("#chat-list", ListView).index = \
                    ids.index(chat["id"])
            if chat["id"] == self.current["id"]:
                return
            self.current = chat
            self.history_limit = 40
            self.query_one("#chat-name", Static).update(chat["label"])
            self.query_one("#approvals", Static).styles.display = "none"
            self.load_history()

        # -- events ---------------------------------------------------
        @on(ListView.Selected, "#chat-list")
        def pick_chat(self, event: ListView.Selected) -> None:
            item = event.item
            if isinstance(item, ChatItem):
                self.select_chat(item.chat)

        @on(Input.Changed, "#chat-filter")
        def filter_changed(self, event: Input.Changed) -> None:
            self._apply_filter(event.value)

        @on(Button.Pressed, "#load-older")
        def load_older_pressed(self, event: Button.Pressed) -> None:
            self.action_load_older()

        def _apply_filter(self, query: str) -> None:
            needle = query.strip().lower()
            chat_list = self.query_one("#chat-list", ListView)
            for item in chat_list.children:
                if isinstance(item, ChatItem):
                    item.display = not needle or \
                        needle in item.chat["label"].lower()

        # -- actions ----------------------------------------------------
        def action_cycle_focus(self) -> None:
            chat_list = self.query_one("#chat-list", ListView)
            if self.focused is chat_list:
                self.query_one("#composer", TextArea).focus()
            else:
                chat_list.focus()

        def action_focus_list(self) -> None:
            self.query_one("#chat-list", ListView).focus()

        def action_focus_composer(self) -> None:
            self.query_one("#composer", TextArea).focus()

        def action_focus_filter(self) -> None:
            self.query_one("#chat-filter", Input).focus()

        def action_send_message(self) -> None:
            composer = self.query_one("#composer", TextArea)
            text = composer.text.strip()
            if not text or self.sending:
                return
            composer.clear()
            pane = self.query_one("#messages", VerticalScroll)
            pane.mount(self.render_message("user", text))
            pane.scroll_end(animate=False)
            self.send_turn(text)

        def action_load_older(self) -> None:
            if self.history_limit >= MAX_HISTORY_LIMIT:
                return
            self.history_limit = min(
                MAX_HISTORY_LIMIT, self.history_limit * 2)
            self.load_history()

        def action_new_chat(self) -> None:
            chat = self.index.create("New chat")
            item = ChatItem(chat)
            self.query_one("#chat-list", ListView).append(item)
            self._apply_filter(
                self.query_one("#chat-filter", Input).value)
            self.current = chat
            self.history_limit = 40
            self.query_one("#chat-name", Static).update(chat["label"])
            self.load_history()
            self.query_one("#composer", TextArea).focus()

        def action_toggle_theme(self) -> None:
            self.theme = ("textual-light" if self.theme == "textual-dark"
                          else "textual-dark")

        def action_reload_history(self) -> None:
            self.load_history()

    return MuseTui()


def _assistant_md(text: str) -> str:
    return "**Muse**\n\n" + text if text else "**Muse**\n\n..."
