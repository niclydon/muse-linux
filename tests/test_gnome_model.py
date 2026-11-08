"""The GNOME client's view state and Markdown renderer (no GTK needed)."""

import pytest

from muselinux.gnome import markdown
from muselinux.gnome.model import ChatState


def history(*items):
    return {"messages": [{"role": r, "text": t, "message_id": i,
                          "occurred_at_ms": n}
                         for n, (r, t, i) in enumerate(items, 1)],
            "has_more": False, "limit": 50}


def test_history_then_a_streamed_reply():
    state = ChatState({"id": "main", "label": "Main chat"})
    state.load_history(history(("user", "hi", "u1"),
                               ("assistant", "hello", "r1")))
    sent = state.add_pending("plan saturday")
    assert sent.pending
    state.apply({"event": "status", "chat": "main", "code": "working",
                 "text": "Thinking"})
    assert state.working and state.status_text == "Thinking"
    change = state.apply({"event": "reply", "message_id": "r2",
                          "text": "Here", "done": False})
    assert change.reply_finished is None
    change = state.apply({"event": "reply", "message_id": "r2",
                          "text": "Here is a plan", "done": True})
    assert change.reply_finished is not None
    assert [m.text for m in state.messages] == [
        "hi", "hello", "plan saturday", "Here is a plan"]
    state.apply({"event": "status", "code": "idle"})
    state.apply({"event": "task", "status": "completed"})
    assert not state.working


def test_live_user_event_and_ack_do_not_duplicate_a_send():
    state = ChatState({"id": "main", "label": "Main chat"})
    state.load_history(history())
    sent = state.add_pending("hello")
    # the live event can arrive before the acknowledgement...
    state.apply({"event": "user_message", "message_id": "u9", "text": "hello"})
    state.acknowledge(sent, "u9")
    assert [m.message_id for m in state.messages] == ["u9"]
    # ...or after it
    other = state.add_pending("again")
    state.acknowledge(other, "u10")
    state.apply({"event": "user_message", "message_id": "u10", "text": "again"})
    assert [m.message_id for m in state.messages] == ["u9", "u10"]


def test_messages_from_other_devices_appear():
    state = ChatState({"id": "main", "label": "Main chat"})
    state.load_history(history())
    state.apply({"event": "user_message", "message_id": "p1",
                 "text": "sent from my phone"})
    assert state.messages[-1].text == "sent from my phone"


def test_reload_keeps_in_flight_and_live_messages_once():
    state = ChatState({"id": "main", "label": "Main chat"})
    state.load_history(history(("user", "a", "u1")))
    state.apply({"event": "reply", "message_id": "r1", "text": "b",
                 "done": True})
    pending = state.add_pending("c")
    state.load_history(history(("user", "a", "u1"), ("assistant", "b", "r1")))
    texts = [m.text for m in state.messages]
    assert texts == ["a", "b", "c"]
    assert pending in state.messages


def test_failed_send_is_marked():
    state = ChatState({"id": "main", "label": "Main chat"})
    sent = state.add_pending("x")
    state.fail(sent, "not connected")
    assert sent.error == "not connected" and not sent.pending


def test_approvals_report_only_new_ones():
    state = ChatState({"id": "main", "label": "Main chat"})
    first = state.apply({"event": "approvals", "pending": [
        {"id": "a1", "title": "Run a command", "detail": "ls"}]})
    assert [a["id"] for a in first.new_approvals] == ["a1"]
    again = state.apply({"event": "approvals", "pending": [
        {"id": "a1", "title": "Run a command", "detail": "ls"}]})
    assert again.new_approvals == []
    assert state.approvals


# -- markdown ------------------------------------------------------------

def test_markdown_escapes_everything_before_formatting():
    blocks = markdown.render('<span foreground="red">x</span> & **bold**')
    kind, markup = blocks[0]
    assert kind == "markup"
    assert "<span" not in markup.replace('<span size', '')
    assert "&lt;span" in markup and "&amp;" in markup
    assert "<b>bold</b>" in markup


def test_markdown_inline_and_blocks():
    text = ("# Title\n\nSome *em* and `code <x>` and ~~gone~~.\n\n"
            "- one\n- two\n  1. nested\n\n> quoted\n\n"
            "```python\nprint('<hi>')\n```\n| a | b |\n|---|---|")
    blocks = markdown.render(text)
    kinds = [b[0] for b in blocks]
    assert kinds == ["markup", "code", "code"]
    markup = blocks[0][1]
    assert '<span size="x-large" weight="bold">Title</span>' in markup
    assert "<i>em</i>" in markup and "<tt>code &lt;x&gt;</tt>" in markup
    assert "<s>gone</s>" in markup and "• one" in markup
    assert "1. nested" in markup and "<i>quoted</i>" in markup
    assert blocks[1] == ("code", "print('<hi>')", "python")
    assert blocks[2][2] == "table"


def test_markdown_links_only_for_safe_schemes():
    markup = markdown.inline("[site](https://example.com/a?b=1&c=2) "
                             "[bad](javascript:alert(1)) https://x.org/p.")
    assert '<a href="https://example.com/a?b=1&amp;c=2">site</a>' in markup
    assert 'href="javascript' not in markup
    assert '<a href="https://x.org/p">https://x.org/p</a>.' in markup


def test_snake_case_is_not_italic():
    assert "<i>" not in markdown.inline("call some_function_name now")


def test_to_plain_for_notifications():
    assert markdown.to_plain("**Hi** [there](https://x.y)\n```\ncode\n```") \
        == "Hi there [code]"


def test_markdown_output_is_valid_pango():
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Pango", "1.0")
        from gi.repository import Pango
    except (ValueError, ImportError):
        pytest.skip("Pango introspection not available")
    samples = ["**a** _b_ `c` [d](https://e.f) <g> & h",
               "# H\n- x\n- **y**\n> q & r", "~~s~~ *t* __u__",
               "unbalanced **bold and `code", "a < b > c \"d\" 'e'"]
    for sample in samples:
        for block in markdown.render(sample):
            if block[0] == "markup":
                # Gtk.Label handles <a>; Pango itself does not know it
                Pango.parse_markup(markdown.without_links(block[1]), -1, "\0")
