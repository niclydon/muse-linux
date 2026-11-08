"""Muse for Linux, the GNOME client (GTK 4 + libadwaita).

A thin front end over the muse-linux daemon: it renders daemon events
and sends daemon commands, and never touches Link or the credential.
Requires GTK 4.14+ and libadwaita 1.5+ (Ubuntu 24.04, Fedora 40 and
later). Run with ``muse-linux-gnome`` or ``muse-linux gnome``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .. import __version__  # noqa: E402
from ..client import DaemonClient, DaemonError  # noqa: E402
from ..protocol import MAX_TEXT_CHARS  # noqa: E402
from . import markdown  # noqa: E402
from .model import Change, ChatState, Message  # noqa: E402

APP_ID = "io.github.niclydon.MuseLinux"
APP_NAME = "Muse for Linux"
MAX_BACKGROUND_WATCHES = 8

CSS = """
.message-user {
  background-color: @accent_bg_color;
  color: @accent_fg_color;
  border-radius: 18px;
  padding: 8px 14px;
}
.message-user label { color: @accent_fg_color; }
.message-user link, .message-user label link { color: @accent_fg_color; }
.message-assistant { padding: 4px 2px; }
.message-failed {
  background-color: alpha(@error_color, 0.15);
  color: @window_fg_color;
}
.message-failed label { color: @window_fg_color; }
.message-meta { font-size: 0.82em; opacity: 0.6; }
.code-block {
  background-color: alpha(currentColor, 0.07);
  border-radius: 8px;
  padding: 8px 10px;
}
.code-block label { font-family: monospace; font-size: 0.93em; }
.code-lang { font-size: 0.78em; opacity: 0.55; }
.composer {
  background-color: @view_bg_color;
  border-radius: 14px;
  padding: 6px 6px 6px 12px;
  border: 1px solid alpha(currentColor, 0.15);
}
.composer textview, .composer textview text { background: transparent; }
.status-line { font-size: 0.9em; opacity: 0.8; }
.task-chip {
  font-size: 0.78em;
  padding: 1px 8px;
  border-radius: 99px;
  background-color: alpha(currentColor, 0.1);
}
.unread-dot {
  min-width: 8px; min-height: 8px; border-radius: 99px;
  background-color: @accent_bg_color;
}
.mirror-note { font-size: 0.85em; opacity: 0.6; }
"""


def on_main(fn, *args):
    """Run fn(*args) on the GTK main loop, from any thread."""
    def _run():
        fn(*args)
        return GLib.SOURCE_REMOVE
    GLib.idle_add(_run)


def then(future, ok, err=None):
    """Deliver a daemon request's result to ok/err on the main loop."""
    def _done(f):
        try:
            result = f.result()
        except Exception as exc:  # DaemonError, DaemonUnavailable
            if err is not None:
                on_main(err, exc)
            return
        on_main(ok, result)
    future.add_done_callback(_done)


def error_text(exc: BaseException) -> str:
    if isinstance(exc, DaemonError):
        return exc.message
    return str(exc)


def fmt_time(ms: int | None) -> str:
    if not ms:
        return ""
    when = datetime.fromtimestamp(ms / 1000)
    if when.date() == datetime.now().date():
        return when.strftime("%H:%M")
    return when.strftime("%b %d, %H:%M")


def relative(ms: int | None) -> str:
    if not ms:
        return ""
    delta = time.time() - ms / 1000
    if delta < 60:
        return "Just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return datetime.fromtimestamp(ms / 1000).strftime("%b %d")


# -- settings -----------------------------------------------------------

class Settings:
    """Small JSON settings file under ~/.config/muse-linux/."""

    DEFAULTS = {"notifications": True, "background": False,
                "history_limit": 50, "last_chat": "main"}

    def __init__(self):
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        self.path = Path(base) / "muse-linux" / "gnome.json"
        self.values = dict(self.DEFAULTS)
        try:
            stored = json.loads(self.path.read_text())
            if isinstance(stored, dict):
                self.values.update({k: v for k, v in stored.items()
                                    if k in self.DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.values[key]

    def __setitem__(self, key, value):
        self.values[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.values, indent=2))
        except OSError:
            pass


# -- message widgets ----------------------------------------------------

def markdown_widget(text: str, user: bool) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    blocks = markdown.render(text) if not user else [
        ("markup", markdown.escape(text))]
    for block in blocks or [("markup", "")]:
        if block[0] == "code":
            frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            frame.add_css_class("code-block")
            if block[2] and block[2] != "table":
                lang = Gtk.Label(label=block[2], xalign=0)
                lang.add_css_class("code-lang")
                frame.append(lang)
            code = Gtk.Label(label=block[1], xalign=0, selectable=True,
                             wrap=True, wrap_mode=Pango.WrapMode.CHAR)
            frame.append(code)
            box.append(frame)
            continue
        label = Gtk.Label(xalign=0, selectable=True, wrap=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR)
        try:
            Pango.parse_markup(markdown.without_links(block[1]), -1, "\0")
            label.set_markup(block[1])
        except GLib.Error:
            label.set_text(text)
        box.append(label)
    return box


class MessageRow(Gtk.Box):
    def __init__(self, message: Message, on_retry=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.message = message
        self.on_retry = on_retry
        self._content = None
        self._rendered = None
        self.bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.meta = Gtk.Box(spacing=6)
        self.meta_label = Gtk.Label(xalign=0)
        self.meta_label.add_css_class("message-meta")
        self.meta.append(self.meta_label)
        self.retry = Gtk.Button(label="Retry")
        self.retry.add_css_class("flat")
        self.retry.add_css_class("message-meta")
        self.retry.connect("clicked", lambda *_: on_retry and on_retry(message))
        self.meta.append(self.retry)
        if message.role == "user":
            self.set_halign(Gtk.Align.END)
            self.set_margin_start(64)
            self.meta.set_halign(Gtk.Align.END)
            self.bubble.add_css_class("message-user")
        else:
            self.set_halign(Gtk.Align.FILL)
            self.set_margin_end(24)
            self.bubble.add_css_class("message-assistant")
        self.append(self.bubble)
        self.append(self.meta)
        self.update()

    def update(self):
        m = self.message
        shown = m.text if (m.text or m.done) else "…"
        if shown != self._rendered:
            if self._content is not None:
                self.bubble.remove(self._content)
            self._content = markdown_widget(shown, m.role == "user")
            self.bubble.append(self._content)
            self._rendered = shown
        bits = []
        if m.pending:
            bits.append("Sending…")
        elif m.error:
            bits.append(f"Not sent: {m.error}")
        else:
            if m.occurred_at_ms:
                bits.append(fmt_time(m.occurred_at_ms))
            if m.reaction:
                bits.append(m.reaction)
            if m.role == "assistant" and not m.done:
                bits.append("Writing…")
        self.meta_label.set_text("  ·  ".join(bits))
        self.meta.set_visible(bool(bits))
        self.retry.set_visible(bool(m.error) and self.on_retry is not None)
        if m.error:
            self.bubble.add_css_class("message-failed")
        else:
            self.bubble.remove_css_class("message-failed")


# -- the chat view -------------------------------------------------------

class ChatView(Adw.Bin):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.state: ChatState | None = None
        self.rows: dict[int, MessageRow] = {}

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="", subtitle="")
        header.set_title_widget(self.title)
        menu = Gio.Menu()
        menu.append("Rename…", "win.rename-chat")
        menu.append("Copy chat ID", "win.copy-chat-id")
        menu.append("Reload history", "win.reload-history")
        section = Gio.Menu()
        section.append("Remove from list…", "win.remove-chat")
        menu.append_section(None, section)
        self.menu_button = Gtk.MenuButton(icon_name="view-more-symbolic",
                                          menu_model=menu,
                                          tooltip_text="Chat menu")
        header.pack_end(self.menu_button)
        toolbar.add_top_bar(header)

        self.approvals_banner = Adw.Banner(button_label="Details")
        self.approvals_banner.connect("button-clicked",
                                      lambda *_: self.show_approvals())
        toolbar.add_top_bar(self.approvals_banner)

        self.scroller = Gtk.ScrolledWindow(vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                            margin_top=12, margin_bottom=12,
                            margin_start=16, margin_end=16)
        self.older = Gtk.Button(label="Load older messages",
                                halign=Gtk.Align.CENTER)
        self.older.add_css_class("pill")
        self.older.connect("clicked", lambda *_: self.load_older())
        self.mirror_note = Gtk.Label(
            label="This is a live mirror of your main chat: messages from "
                  "your other devices appear here too.",
            wrap=True, justify=Gtk.Justification.CENTER)
        self.mirror_note.add_css_class("mirror-note")
        self.empty = Adw.StatusPage(
            icon_name="muse-side-chat-symbolic", title="A new side chat",
            description="It starts on your first message. Muse can take "
                        "a little longer to answer the first one.")
        self.empty.add_css_class("compact")
        self.loading = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER,
                                   height_request=32)
        self.list.append(self.older)
        self.list.append(self.mirror_note)
        self.list.append(self.empty)
        self.list.append(self.loading)
        clamp.set_child(self.list)
        self.scroller.set_child(clamp)
        self._stick = True
        vadj = self.scroller.get_vadjustment()
        vadj.connect("value-changed", self._on_scroll)
        vadj.connect("changed", self._on_range)
        toolbar.set_content(self.scroller)

        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         margin_start=12, margin_end=12, margin_bottom=12)
        self.status_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        status = Gtk.Box(spacing=8, margin_start=6)
        self.status_spinner = Gtk.Spinner()
        self.status_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                      hexpand=True)
        self.status_label.add_css_class("status-line")
        self.task_chip = Gtk.Label()
        self.task_chip.add_css_class("task-chip")
        status.append(self.status_spinner)
        status.append(self.status_label)
        status.append(self.task_chip)
        self.status_revealer.set_child(status)
        bottom.append(self.status_revealer)

        composer_clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        composer = Gtk.Box(spacing=6)
        composer.add_css_class("composer")
        entry_scroll = Gtk.ScrolledWindow(
            hexpand=True, propagate_natural_height=True, max_content_height=180,
            min_content_height=24, valign=Gtk.Align.CENTER,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            # a scrollbar's minimum length would make a one-line box tall;
            # show it only once the text is long enough to need it
            vscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        self.entry_scroll = entry_scroll
        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                  accepts_tab=False, top_margin=4,
                                  bottom_margin=4)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], ["Message"])
        self.placeholder = Gtk.Label(label="Message Muse", xalign=0,
                                     can_target=False)
        self.placeholder.add_css_class("dim-label")
        overlay = Gtk.Overlay()
        overlay.set_child(self.entry)
        overlay.add_overlay(self.placeholder)
        self.placeholder.set_valign(Gtk.Align.CENTER)
        entry_scroll.set_child(overlay)
        self.entry.get_buffer().connect("changed", self._on_text_changed)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.entry.add_controller(keys)
        self.send_button = Gtk.Button(icon_name="go-up-symbolic",
                                      valign=Gtk.Align.END,
                                      tooltip_text="Send (Enter)")
        self.send_button.add_css_class("suggested-action")
        self.send_button.add_css_class("circular")
        self.send_button.connect("clicked", lambda *_: self.send())
        composer.append(entry_scroll)
        composer.append(self.send_button)
        composer_clamp.set_child(composer)
        bottom.append(composer_clamp)
        toolbar.add_bottom_bar(bottom)
        self.set_child(toolbar)
        self._on_text_changed(self.entry.get_buffer())

    # -- binding -------------------------------------------------------
    def bind(self, state: ChatState):
        self.state = state
        self.entry.get_buffer().set_text("")
        self.rebuild()
        self.refresh_header()
        self.refresh_status()
        self.refresh_approvals()
        self._stick = True
        GLib.idle_add(self._scroll_to_end)

    def rebuild(self):
        for row in self.rows.values():
            self.list.remove(row)
        self.rows.clear()
        if self.state is None:
            return
        for message in self.state.messages:
            self._add_row(message)
        self.refresh_chrome()

    def _add_row(self, message: Message):
        row = MessageRow(message, on_retry=self.retry)
        self.rows[message.key] = row
        self.list.insert_child_after(row, self._last_widget())
        return row

    def _last_widget(self):
        child = self.list.get_last_child()
        if child is self.loading:
            return child.get_prev_sibling()
        return child

    def refresh_chrome(self):
        s = self.state
        if s is None:
            return
        self.older.set_visible(s.loaded and s.has_more and s.limit < 200)
        self.mirror_note.set_visible(s.is_main and s.loaded)
        self.empty.set_visible(s.loaded and not s.messages and not s.loading)
        self.loading.set_visible(s.loading)
        self.loading.set_spinning(s.loading)

    def apply_change(self, change):
        if change.reset:
            self.rebuild()
        for message in change.removed:
            row = self.rows.pop(message.key, None)
            if row is not None:
                self.list.remove(row)
        for message in change.messages:
            row = self.rows.get(message.key)
            if row is None:
                self._add_row(message)
            else:
                row.update()
        if change.status:
            self.refresh_status()
            self.refresh_header()
        if change.approvals:
            self.refresh_approvals()
        self.refresh_chrome()
        if self._stick:
            GLib.idle_add(self._scroll_to_end)

    def refresh_header(self):
        s = self.state
        if s is None:
            return
        self.title.set_title(s.chat["label"])
        if s.is_main:
            subtitle = "Live mirror"
        elif s.not_started:
            subtitle = "Side chat, starts on your first message"
        else:
            subtitle = "Side chat"
        muse = self.window.muse_state
        if not self.window.daemon_up:
            subtitle = "Not connected to the daemon"
        elif muse in ("connecting", "disconnected") or s.watch_state == "error":
            subtitle += " · reconnecting to Muse"
        self.title.set_subtitle(subtitle)
        self.window.lookup_action("remove-chat").set_enabled(not s.is_main)

    def refresh_status(self):
        s = self.state
        if s is None:
            return
        working = s.working
        self.status_revealer.set_reveal_child(working)
        self.status_spinner.set_spinning(working)
        text = s.status_text or {
            "working": "Muse is working", "thinking": "Muse is thinking",
            "responding": "Muse is replying",
            "researching": "Muse is researching"}.get(
                s.status_code or "", "Muse is working")
        self.status_label.set_text(text)
        task = s.task_status
        self.task_chip.set_visible(bool(task) and working)
        self.task_chip.set_text(f"Task {task}" if task else "")

    def refresh_approvals(self):
        s = self.state
        pending = s.approvals if s else []
        if pending:
            first = pending[0].get("title") or "an action"
            more = f" and {len(pending) - 1} more" if len(pending) > 1 else ""
            self.approvals_banner.set_title(
                f"Muse is waiting on your approval for “{first}”{more}. "
                "Approve it in the Muse app.")
        self.approvals_banner.set_revealed(bool(pending))

    def show_approvals(self):
        s = self.state
        if not s or not s.approvals:
            return
        dialog = Adw.AlertDialog(
            heading="Waiting on your approval",
            body="Approvals can only be decided in the Muse app on your "
                 "phone or Mac. This client shows them so you know why "
                 "Muse is paused.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        group = Adw.PreferencesGroup()
        for item in s.approvals:
            row = Adw.ActionRow(title=GLib.markup_escape_text(
                item.get("title") or "Approval requested"))
            if item.get("detail"):
                row.set_subtitle(GLib.markup_escape_text(item["detail"][:300]))
            row.add_prefix(Gtk.Image(icon_name="dialog-question-symbolic"))
            group.add(row)
        box.append(group)
        dialog.set_extra_child(box)
        dialog.add_response("close", "Close")
        dialog.present(self.window)

    # -- scrolling -----------------------------------------------------
    def _on_scroll(self, adj):
        self._stick = adj.get_value() + adj.get_page_size() >= adj.get_upper() - 48

    def _on_range(self, adj):
        if self._stick:
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def _scroll_to_end(self):
        adj = self.scroller.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return GLib.SOURCE_REMOVE

    # -- composing -----------------------------------------------------
    def _on_text_changed(self, buffer):
        has_text = buffer.get_char_count() > 0
        self.entry_scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC if buffer.get_line_count() > 6
            else Gtk.PolicyType.EXTERNAL)
        self.placeholder.set_visible(not has_text)
        self.send_button.set_sensitive(has_text and self.window.daemon_up)

    def _on_key(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                return False
            self.send()
            return True
        return False

    def focus_composer(self):
        self.entry.grab_focus()

    def send(self):
        buffer = self.entry.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(),
                               False).strip()
        if not text or self.state is None:
            return
        if len(text) > MAX_TEXT_CHARS:
            self.window.toast(f"Messages are limited to {MAX_TEXT_CHARS:,} characters.")
            return
        buffer.set_text("")
        self.window.send(self.state, text)
        self._stick = True

    def retry(self, message: Message):
        if self.state is None:
            return
        self.state.messages.remove(message)
        row = self.rows.pop(message.key, None)
        if row is not None:
            self.list.remove(row)
        self.window.send(self.state, message.text)

    def load_older(self):
        if self.state is not None:
            self.window.load_history(self.state,
                                     min(200, max(self.state.limit, 50) * 2))


# -- sidebar --------------------------------------------------------------

class ChatRow(Gtk.ListBoxRow):
    def __init__(self, chat: dict):
        super().__init__()
        self.chat = chat
        box = Gtk.Box(spacing=10, margin_top=8, margin_bottom=8,
                      margin_start=6, margin_end=6)
        icon = Gtk.Image(icon_name="user-home-symbolic" if chat["is_main"]
                         else "muse-side-chat-symbolic")
        box.append(icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.subtitle.add_css_class("caption")
        self.subtitle.add_css_class("dim-label")
        texts.append(self.title)
        texts.append(self.subtitle)
        box.append(texts)
        self.dot = Gtk.Box(valign=Gtk.Align.CENTER)
        self.dot.add_css_class("unread-dot")
        box.append(self.dot)
        self.set_child(box)
        self.refresh(chat, unread=False, working=False)

    def refresh(self, chat: dict, unread: bool, working: bool):
        self.chat = chat
        self.title.set_text(chat["label"])
        if working:
            sub = "Muse is working…"
        elif chat["is_main"]:
            sub = "Your main chat"
        else:
            sub = relative(chat.get("last_activity_ms")) or "Side chat"
        self.subtitle.set_text(sub)
        self.dot.set_visible(unread)


def chat_sort_key(chat: dict):
    if chat["is_main"]:
        return (0, 0)
    return (1, -(chat.get("last_activity_ms")
                 or (chat.get("created_at") or 0) * 1000))


# -- the window -----------------------------------------------------------

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: "MuseApp"):
        super().__init__(application=app, title=APP_NAME,
                         default_width=1100, default_height=760)
        self.app = app
        self.client = app.client
        self.settings = app.settings
        self.chats: dict[str, dict] = {}
        self.states: dict[str, ChatState] = {}
        self.rows: dict[str, ChatRow] = {}
        self.current: str | None = None
        self.daemon_up = False
        self.daemon_problem: str | None = None
        self.muse_state = "idle"
        self.background_watches: list[str] = []
        self.pairing_problem: str | None = None

        self._install_actions()
        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=260,
                                             max_sidebar_width=340)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        # sidebar
        side = Adw.ToolbarView()
        side_header = Adw.HeaderBar()
        side_header.set_title_widget(Adw.WindowTitle(title="Muse"))
        main_menu = Gio.Menu()
        main_menu.append("Status and settings", "app.preferences")
        main_menu.append("Keyboard shortcuts", "win.show-help-overlay")
        main_menu.append(f"About {APP_NAME}", "app.about")
        side_header.pack_start(Gtk.MenuButton(icon_name="open-menu-symbolic",
                                              menu_model=main_menu,
                                              primary=True,
                                              tooltip_text="Main menu"))
        add_menu = Gio.Menu()
        add_menu.append("New side chat", "win.new-chat")
        add_menu.append("Add chat by ID…", "win.add-chat")
        side_header.pack_end(Gtk.MenuButton(icon_name="list-add-symbolic",
                                            menu_model=add_menu,
                                            tooltip_text="New chat"))
        side.add_top_bar(side_header)
        self.search = Gtk.SearchEntry(placeholder_text="Filter chats",
                                      margin_start=12, margin_end=12,
                                      margin_bottom=6)
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
        self.search.connect("activate", lambda *_: self._open_first_match())
        self.search.connect("stop-search", lambda *_: self.search.set_text(""))
        side.add_top_bar(self.search)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.set_sort_func(self._sort_rows)
        self.listbox.set_filter_func(self._filter_rows)
        self.listbox.connect("row-selected", self._on_row_selected)
        scroll = Gtk.ScrolledWindow(vexpand=True,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.listbox)
        side.set_content(scroll)
        self.link_banner = Adw.Banner(button_label="Retry")
        self.link_banner.connect("button-clicked", lambda *_: self.client.retry_now())
        side.add_bottom_bar(self.link_banner)
        self.split.set_sidebar(Adw.NavigationPage(title="Chats", child=side))

        # content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.chat_view = ChatView(self)
        self.stack.add_named(self.chat_view, "chat")
        self.problem_page = Adw.StatusPage(icon_name="network-offline-symbolic")
        retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda *_: self.recheck())
        self.problem_page.set_child(retry)
        problem_view = Adw.ToolbarView()
        problem_view.add_top_bar(Adw.HeaderBar(show_title=False))
        problem_view.set_content(self.problem_page)
        self.stack.add_named(problem_view, "problem")
        welcome = Adw.StatusPage(icon_name=APP_ID, title=APP_NAME,
                                 description="Connecting to the muse-linux daemon…")
        welcome_view = Adw.ToolbarView()
        welcome_view.add_top_bar(Adw.HeaderBar(show_title=False))
        welcome_view.set_content(welcome)
        self.stack.add_named(welcome_view, "welcome")
        self.stack.set_visible_child_name("welcome")
        self.content_page = Adw.NavigationPage(title="Chat", child=self.stack)
        self.split.set_content(self.content_page)

        self.connect("notify::is-active", self._on_active)

    # -- actions -------------------------------------------------------
    def _install_actions(self):
        for name, handler in (
                ("new-chat", self.new_chat), ("add-chat", self.add_chat),
                ("rename-chat", self.rename_chat),
                ("remove-chat", self.remove_chat),
                ("copy-chat-id", self.copy_chat_id),
                ("reload-history", self.reload_history),
                ("focus-search", lambda: self.search.grab_focus()),
                ("focus-composer", lambda: self.chat_view.focus_composer())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)

    def toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text),
                                        timeout=4))

    # -- daemon link -----------------------------------------------------
    def on_link(self, up: bool, problem: str | None):
        self.daemon_up = up
        self.daemon_problem = problem
        self.chat_view._on_text_changed(self.chat_view.entry.get_buffer())
        if up:
            self.link_banner.set_revealed(False)
            # anything could have happened while we were away: reload
            # every chat's history the next time it is shown
            for state in self.states.values():
                state.loaded = False
            self.load_chats()
            self.check_pairing()
            if self.current in self.states:
                self.reload_history()
        else:
            self.link_banner.set_title("Daemon not reachable")
            self.link_banner.set_revealed(True)
            self.show_problem("Can’t reach the muse-linux daemon",
                              problem or "")
        if self.chat_view.state is not None:
            self.chat_view.refresh_header()

    def recheck(self):
        self.client.retry_now()
        if self.daemon_up:
            self.check_pairing()

    def show_problem(self, title: str, description: str):
        self.problem_page.set_title(title)
        self.problem_page.set_description(GLib.markup_escape_text(description))
        self.stack.set_visible_child_name("problem")
        self.content_page.set_title(title)

    def check_pairing(self):
        def ok(report):
            pairing = report.get("pairing") or {}
            problem = pairing_problem(pairing)
            self.pairing_problem = problem
            if problem:
                self.show_problem(problem[0], problem[1])
            elif self.stack.get_visible_child_name() != "chat":
                self.select(self.current or self.settings["last_chat"])
        then(self.client.call("doctor"), ok, lambda exc: None)

    def on_event(self, event: dict):
        kind = event.get("event")
        if kind == "connection":
            previous, self.muse_state = self.muse_state, event.get("state") or "idle"
            if self.muse_state == "connected" and previous == "disconnected" \
                    and self.current in self.states:
                # the session dropped and came back: fill the gap
                self.reload_history()
            if self.chat_view.state is not None:
                self.chat_view.refresh_header()
            return
        if kind == "chats.changed":
            if event.get("removed"):
                self._forget_chat(event["removed"])
            if event.get("chat"):
                self._upsert_chat(event["chat"])
            return
        chat_id = event.get("chat")
        state = self.states.get(chat_id)
        if state is None:
            return
        change = state.apply(event)
        if chat_id == self.current:
            self.chat_view.apply_change(change)
        if change.reply_finished is not None:
            focused = chat_id == self.current and self.is_active()
            if not focused:
                state.unread = chat_id != self.current or not self.is_active()
                self.app.notify_reply(state, change.reply_finished)
        if change.new_approvals:
            self.app.notify_approval(state, change.new_approvals)
        self._refresh_row(chat_id)

    def _on_active(self, *_):
        if self.is_active() and self.current in self.states:
            self.states[self.current].unread = False
            self._refresh_row(self.current)
            self.app.withdraw(self.current)

    # -- chat list -------------------------------------------------------
    def load_chats(self):
        def ok(result):
            seen = set()
            for chat in result.get("chats") or []:
                self._upsert_chat(chat)
                seen.add(chat["id"])
            for chat_id in list(self.chats):
                if chat_id not in seen:
                    self._forget_chat(chat_id)
            if self.app.initial_chat and self.app.initial_chat in self.chats:
                self.select(self.app.initial_chat)
                self.app.initial_chat = None
            elif self.current is None and self.pairing_problem is None:
                self.select(self.settings["last_chat"]
                            if self.settings["last_chat"] in self.chats
                            else "main")
        then(self.client.call("chats.list"), ok,
             lambda exc: self.toast(error_text(exc)))

    def _upsert_chat(self, chat: dict):
        self.chats[chat["id"]] = chat
        state = self.states.get(chat["id"])
        if state is None:
            self.states[chat["id"]] = ChatState(chat)
        else:
            state.chat = chat
        row = self.rows.get(chat["id"])
        if row is None:
            row = ChatRow(chat)
            self.rows[chat["id"]] = row
            self.listbox.append(row)
        self._refresh_row(chat["id"])
        self.listbox.invalidate_sort()
        if chat["id"] == self.current:
            self.chat_view.refresh_header()

    def _forget_chat(self, chat_id: str):
        self.chats.pop(chat_id, None)
        self.states.pop(chat_id, None)
        row = self.rows.pop(chat_id, None)
        if row is not None:
            self.listbox.remove(row)
        if chat_id in self.background_watches:
            self.background_watches.remove(chat_id)
        if self.current == chat_id:
            self.current = None
            self.select("main")

    def _refresh_row(self, chat_id: str):
        row = self.rows.get(chat_id)
        state = self.states.get(chat_id)
        if row and state:
            row.refresh(state.chat, unread=state.unread, working=state.working)

    def _sort_rows(self, a, b):
        ka, kb = chat_sort_key(a.chat), chat_sort_key(b.chat)
        return (ka > kb) - (ka < kb)

    def _filter_rows(self, row):
        needle = self.search.get_text().strip().lower()
        return not needle or needle in row.chat["label"].lower()

    def _open_first_match(self):
        index = 0
        while (row := self.listbox.get_row_at_index(index)) is not None:
            if row.get_child_visible() and self._filter_rows(row):
                self.select(row.chat["id"])
                self.search.set_text("")
                return
            index += 1

    def _on_row_selected(self, listbox, row):
        if row is not None and row.chat["id"] != self.current:
            self.select(row.chat["id"])
        if row is not None:
            self.split.set_show_content(True)

    def select(self, chat_id: str):
        if chat_id not in self.states:
            chat_id = "main"
        if chat_id not in self.states:
            return
        previous = self.current
        self.current = chat_id
        state = self.states[chat_id]
        state.unread = False
        row = self.rows.get(chat_id)
        if row is not None and self.listbox.get_selected_row() is not row:
            self.listbox.select_row(row)
        self.settings["last_chat"] = chat_id
        if self.pairing_problem is None:
            self.stack.set_visible_child_name("chat")
        self.content_page.set_title(state.chat["label"])
        self.chat_view.bind(state)
        self.client.watch(chat_id)
        if previous and previous != chat_id and \
                previous not in self.background_watches:
            self.client.unwatch(previous)
        if not state.loaded and not state.loading:
            self.load_history(state, self.settings["history_limit"])
        self._refresh_row(chat_id)
        self.app.withdraw(chat_id)
        GLib.idle_add(lambda: (self.chat_view.focus_composer(), False)[1])

    def load_history(self, state: ChatState, limit: int):
        state.loading = True
        if state.chat_id == self.current:
            self.chat_view.refresh_chrome()

        def ok(result):
            state.loading = False
            change = state.load_history(result)
            if state.chat_id == self.current:
                self.chat_view.apply_change(change)
                self.chat_view.refresh_header()

        def err(exc):
            state.loading = False
            if state.chat_id == self.current:
                self.chat_view.refresh_chrome()
            self.toast(f"Couldn’t load history: {error_text(exc)}")
        then(self.client.call("history", chat=state.chat_id, limit=limit),
             ok, err)

    def reload_history(self):
        if self.current in self.states:
            state = self.states[self.current]
            self.load_history(state, max(state.limit,
                                         self.settings["history_limit"]))

    # -- sending ---------------------------------------------------------
    def send(self, state: ChatState, text: str):
        message = state.add_pending(text)
        if state.chat_id == self.current:
            self.chat_view.apply_change(Change(messages=[message], status=True))
        self._keep_watching(state.chat_id)

        def ok(result):
            change = state.acknowledge(message, result.get("message_id"))
            if state.chat_id == self.current:
                self.chat_view.apply_change(change)

        def err(exc):
            change = state.fail(message, error_text(exc))
            if state.chat_id == self.current:
                self.chat_view.apply_change(change)
        then(self.client.call("send", chat=state.chat_id, text=text), ok, err)

    def _keep_watching(self, chat_id: str):
        """Keep a chat you sent to watched, so its reply can notify you
        after you switch away."""
        if chat_id in self.background_watches:
            self.background_watches.remove(chat_id)
        self.background_watches.append(chat_id)
        while len(self.background_watches) > MAX_BACKGROUND_WATCHES:
            dropped = self.background_watches.pop(0)
            if dropped != self.current:
                self.client.unwatch(dropped)

    # -- chat management -------------------------------------------------
    def _ask_text(self, heading, body, label, initial, ok_label, done):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        entry = Adw.EntryRow(title=label, text=initial or "")
        group = Adw.PreferencesGroup()
        group.add(entry)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", ok_label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")
        entry.connect("entry-activated",
                      lambda *_: (dialog.close(), done(entry.get_text())))
        dialog.connect("response", lambda _d, r: r == "ok" and done(entry.get_text()))
        dialog.present(self)
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])
        return dialog

    def new_chat(self):
        def done(label):
            then(self.client.call("chats.new", label=label.strip() or "New chat"),
                 lambda chat: (self._upsert_chat(chat), self.select(chat["id"])),
                 lambda exc: self.toast(error_text(exc)))
        self._ask_text("New side chat",
                       "A side chat is a separate conversation with Muse. "
                       "It starts on your first message.",
                       "Name", "", "Create", done)

    def add_chat(self):
        def done(text):
            session_id = text.strip()
            if not session_id:
                return
            then(self.client.call("chats.add", session_id=session_id,
                                  label="Added chat"),
                 lambda chat: (self._upsert_chat(chat), self.select(chat["id"])),
                 lambda exc: self.toast(error_text(exc)))
        self._ask_text("Add a chat by ID",
                       "Muse doesn’t let this device list your chats, so "
                       "paste the ID of a side chat from the phone or Mac app.",
                       "Chat ID", "", "Add", done)

    def rename_chat(self):
        chat = self.chats.get(self.current or "")
        if not chat:
            return

        def done(label):
            if label.strip():
                then(self.client.call("chats.rename", chat=chat["id"],
                                      label=label.strip()),
                     self._upsert_chat, lambda exc: self.toast(error_text(exc)))
        self._ask_text("Rename chat", "The name is only stored on this computer.",
                       "Name", chat["label"], "Rename", done)

    def remove_chat(self):
        chat = self.chats.get(self.current or "")
        if not chat or chat["is_main"]:
            return
        dialog = Adw.AlertDialog(
            heading=f"Remove “{chat['label']}” from this list?",
            body="Only the local list changes. The chat stays in Muse, and "
                 "you can add it back with its ID.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def response(_d, choice):
            if choice == "remove":
                then(self.client.call("chats.remove", chat=chat["id"]),
                     lambda _r: self._forget_chat(chat["id"]),
                     lambda exc: self.toast(error_text(exc)))
        dialog.connect("response", response)
        dialog.present(self)

    def copy_chat_id(self):
        chat = self.chats.get(self.current or "")
        if not chat:
            return
        value = chat.get("session_id") or "main"
        self.get_clipboard().set(value)
        self.toast("Chat ID copied")


def pairing_problem(pairing: dict) -> tuple[str, str] | None:
    """The single most useful problem and its fix, or None when ready."""
    if not pairing.get("state_dir"):
        return ("This computer isn’t paired with Muse",
                "Pair it as a Muse gadget with the Muse Gadget SDK for "
                "Linux, then try again.")
    if pairing.get("paired") is False or (pairing.get("error") and not
                                          pairing.get("vm_fetch_status")):
        return ("The pairing can’t be read",
                "The daemon found a pairing store but could not load it "
                f"({pairing.get('error') or 'unknown error'}). Re-pair "
                "this computer with the Muse app.")
    status = pairing.get("vm_fetch_status")
    if status is not None and status != 200:
        return ("Muse didn’t accept this computer’s credentials",
                f"Looking up your Muse VM failed with HTTP {status}. "
                "Make sure the musegadget service is running so it can "
                "refresh its tokens, or re-pair.")
    if status == 200 and not pairing.get("vm_count"):
        return ("No Muse VM is available",
                "The account has no leased Muse VM right now. Open the Muse "
                "app once, then try again.")
    return None


# -- preferences ---------------------------------------------------------

class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app: "MuseApp"):
        super().__init__(title="Status and settings")
        self.app = app
        status = Adw.PreferencesPage(title="Status",
                                     icon_name="emblem-system-symbolic")
        self.summary = Adw.PreferencesGroup()
        self.daemon_group = Adw.PreferencesGroup(title="Daemon")
        self.pairing_group = Adw.PreferencesGroup(title="Pairing")
        status.add(self.summary)
        status.add(self.daemon_group)
        status.add(self.pairing_group)
        self._rows = []
        self.add(status)

        prefs = Adw.PreferencesPage(title="Settings",
                                    icon_name="preferences-system-symbolic")
        group = Adw.PreferencesGroup(title="Notifications and background")
        notify = Adw.SwitchRow(title="Notify me about replies and approvals",
                               subtitle="When the chat isn’t in front of you",
                               active=app.settings["notifications"])
        notify.connect("notify::active", lambda r, _p:
                       app.settings.__setitem__("notifications", r.get_active()))
        group.add(notify)
        background = Adw.SwitchRow(
            title="Keep running when the window is closed",
            subtitle="So replies you are waiting on can still notify you",
            active=app.settings["background"])
        background.connect("notify::active", lambda r, _p:
                           app.settings.__setitem__("background", r.get_active()))
        group.add(background)
        prefs.add(group)
        history = Adw.PreferencesGroup(title="History")
        limit = Adw.SpinRow.new_with_range(20, 200, 10)
        limit.set_title("Messages to load when opening a chat")
        limit.set_subtitle("Muse returns at most 200")
        limit.set_value(app.settings["history_limit"])
        limit.connect("notify::value", lambda r, _p:
                      app.settings.__setitem__("history_limit", int(r.get_value())))
        history.add(limit)
        prefs.add(history)
        self.add(prefs)
        self.refresh()

    def _row(self, group, title, value, ok):
        row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(str(value)))
        row.add_prefix(Gtk.Image(
            icon_name="emblem-ok-symbolic" if ok else "dialog-warning-symbolic"))
        group.add(row)
        self._rows.append((group, row))

    def refresh(self):
        for group, row in self._rows:
            group.remove(row)
        self._rows.clear()
        client = self.app.client
        if not client.connected:
            self.summary.set_description(GLib.markup_escape_text(
                client.last_problem or "Not connected to the daemon."))
            self._row(self.daemon_group, "Daemon", "Not reachable", False)
            self._row(self.daemon_group, "Socket", client.socket_path, False)
            return
        self.summary.set_description("Checking…")

        def ok(report):
            daemon = report.get("daemon") or {}
            pairing = report.get("pairing") or {}
            problem = pairing_problem(pairing)
            connection = daemon.get("connection") or "idle"
            if problem:
                self.summary.set_description(GLib.markup_escape_text(
                    f"{problem[0]}. {problem[1]}"))
            else:
                self.summary.set_description(
                    "Everything is ready. This computer is paired and the "
                    "daemon can reach Muse.")
            self._row(self.daemon_group, "Daemon",
                      f"Running, version {daemon.get('version')}, "
                      f"protocol {daemon.get('protocol')}", True)
            self._row(self.daemon_group, "Socket", client.socket_path, True)
            since = daemon.get("connected_since")
            session = {"connected": "Connected" + (
                f" since {datetime.fromtimestamp(since).strftime('%H:%M')}"
                if since else ""),
                "connecting": "Connecting…", "idle": "Idle",
                "disconnected": "Disconnected, retrying"}.get(connection, connection)
            if daemon.get("error") and connection != "connected":
                session += f" ({daemon['error']})"
            self._row(self.daemon_group, "Muse session", session,
                      connection in ("connected", "idle", "connecting"))
            self._row(self.daemon_group, "Chats being watched",
                      str(len(daemon.get("watches") or {})), True)
            self._row(self.pairing_group, "Pairing store",
                      pairing.get("state_dir") or "Not found",
                      bool(pairing.get("state_dir")))
            self._row(self.pairing_group, "Paired",
                      "Yes" if pairing.get("paired") else "No",
                      bool(pairing.get("paired")))
            status = pairing.get("vm_fetch_status")
            self._row(self.pairing_group, "Muse VM lookup",
                      f"HTTP {status}, {pairing.get('vm_count', 0)} VM"
                      if status else (pairing.get("error") or "Not checked"),
                      status == 200 and bool(pairing.get("vm_count")))

        def err(exc):
            self.summary.set_description(GLib.markup_escape_text(error_text(exc)))
        then(client.call("doctor"), ok, err)


# -- the application -------------------------------------------------------

class MuseApp(Adw.Application):
    def __init__(self, socket_path: str | None = None,
                 initial_chat: str | None = None):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name(APP_NAME)
        self.settings = Settings()
        self.initial_chat = initial_chat
        self.window: MainWindow | None = None
        self.client = DaemonClient(
            socket_path,
            on_event=lambda e: on_main(self._on_event, e),
            on_link=lambda up, why: on_main(self._on_link, up, why))
        self._held = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        icons = Path(__file__).resolve().parent / "data" / "icons"
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()) \
            .add_search_path(str(icons))
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, handler, accels in (
                ("preferences", self.show_preferences, ["<Ctrl>comma"]),
                ("about", self.show_about, []),
                ("quit", self.quit_now, ["<Ctrl>q"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        open_chat = Gio.SimpleAction.new("open-chat", GLib.VariantType.new("s"))
        open_chat.connect("activate", self._open_chat)
        self.add_action(open_chat)
        for action, accels in (("win.new-chat", ["<Ctrl>n"]),
                               ("win.add-chat", ["<Ctrl><Shift>n"]),
                               ("win.reload-history", ["<Ctrl>r", "F5"]),
                               ("win.rename-chat", ["F2"]),
                               ("win.focus-search", ["<Ctrl>k", "<Ctrl>f"]),
                               ("win.focus-composer", ["<Ctrl>l"])):
            self.set_accels_for_action(action, accels)
        self.client.start()

    def do_activate(self):
        if self.window is None:
            self.window = MainWindow(self)
            self.window.set_help_overlay(self._shortcuts())
            self.window.connect("close-request", self._on_close)
            if self.client.connected and self.client.daemon_info:
                self.window.on_link(True, None)
        self.window.present()

    def _shortcuts(self):
        builder = Gtk.Builder.new_from_string(SHORTCUTS_UI, -1)
        return builder.get_object("help_overlay")

    def _on_close(self, window):
        if self.settings["background"]:
            window.set_visible(False)
            if not self._held:
                self.hold()
                self._held = True
            return True
        return False

    def quit_now(self):
        if self._held:
            self.release()
            self._held = False
        self.client.close()
        self.quit()

    def _on_link(self, up, why):
        if self.window is not None:
            self.window.on_link(up, why)

    def _on_event(self, event):
        if self.window is not None:
            self.window.on_event(event)

    def _open_chat(self, _action, param):
        self.activate()
        if self.window is not None:
            self.window.select(param.get_string())

    def notify_reply(self, state: ChatState, message: Message):
        if not self.settings["notifications"] or not self.get_application_id():
            return
        note = Gio.Notification.new(state.chat["label"])
        body = markdown.to_plain(message.text)
        note.set_body(body[:180] + ("…" if len(body) > 180 else ""))
        note.set_default_action_and_target("app.open-chat",
                                           GLib.Variant.new_string(state.chat_id))
        self.send_notification(f"chat-{state.chat_id}", note)

    def notify_approval(self, state: ChatState, approvals: list[dict]):
        if not self.settings["notifications"] or not self.get_application_id():
            return
        first = approvals[0].get("title") or "an action"
        note = Gio.Notification.new("Muse is waiting on your approval")
        note.set_body(f"“{first}” in {state.chat['label']}. "
                      "Approve it in the Muse app.")
        note.set_priority(Gio.NotificationPriority.HIGH)
        note.set_default_action_and_target("app.open-chat",
                                           GLib.Variant.new_string(state.chat_id))
        self.send_notification(f"approval-{state.chat_id}", note)

    def withdraw(self, chat_id: str):
        if self.get_application_id():
            self.withdraw_notification(f"chat-{chat_id}")

    def show_preferences(self):
        if self.window is not None:
            PreferencesDialog(self).present(self.window)

    def show_about(self):
        about = Adw.AboutDialog(
            application_name=APP_NAME, application_icon=APP_ID,
            version=__version__, developer_name="muse-linux contributors",
            license_type=Gtk.License.APACHE_2_0,
            comments="An unofficial Linux client for Meta Muse. Not made "
                     "by, endorsed by or affiliated with Meta.",
            website="https://github.com/niclydon/muse-linux")
        about.present(self.window)


SHORTCUTS_UI = """<?xml version="1.0" encoding="UTF-8"?>
<interface>
  <object class="GtkShortcutsWindow" id="help_overlay">
    <property name="modal">True</property>
    <child>
      <object class="GtkShortcutsSection">
        <property name="section-name">shortcuts</property>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Chats</property>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">New side chat</property>
              <property name="accelerator">&lt;Ctrl&gt;n</property></object></child>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">Add a chat by ID</property>
              <property name="accelerator">&lt;Ctrl&gt;&lt;Shift&gt;n</property></object></child>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">Filter chats</property>
              <property name="accelerator">&lt;Ctrl&gt;k</property></object></child>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">Rename the chat</property>
              <property name="accelerator">F2</property></object></child>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">Reload history</property>
              <property name="accelerator">&lt;Ctrl&gt;r</property></object></child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Writing</property>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">Send</property>
              <property name="accelerator">Return</property></object></child>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">New line</property>
              <property name="accelerator">&lt;Shift&gt;Return</property></object></child>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">Focus the message box</property>
              <property name="accelerator">&lt;Ctrl&gt;l</property></object></child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">General</property>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">Status and settings</property>
              <property name="accelerator">&lt;Ctrl&gt;comma</property></object></child>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">Keyboard shortcuts</property>
              <property name="accelerator">&lt;Ctrl&gt;question</property></object></child>
            <child><object class="GtkShortcutsShortcut">
              <property name="title">Quit</property>
              <property name="accelerator">&lt;Ctrl&gt;q</property></object></child>
          </object>
        </child>
      </object>
    </child>
  </object>
</interface>
"""


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="muse-linux-gnome",
                                     description=f"{APP_NAME} (GNOME client)")
    parser.add_argument("--socket", help="daemon socket path")
    parser.add_argument("--chat", help="open this chat (id or session id)")
    parser.add_argument("--version", action="version",
                        version=f"muse-linux-gnome {__version__}")
    args, rest = parser.parse_known_args(argv if argv is not None else sys.argv[1:])
    app = MuseApp(args.socket, args.chat)
    return app.run([sys.argv[0], *rest])
