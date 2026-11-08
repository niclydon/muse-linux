"""End-to-end UI test: the GTK client against the real daemon and an
in-memory Muse. Needs GTK 4 + libadwaita introspection and a display
(CI runs it under xvfb-run); skipped otherwise."""

import asyncio
import os
import threading
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk  # noqa: E402
except (ValueError, ImportError):
    pytest.skip("GTK 4 / libadwaita introspection not available",
                allow_module_level=True)
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("no display", allow_module_level=True)
if not Gtk.init_check():
    pytest.skip("GTK cannot open the display", allow_module_level=True)

from muselinux.daemon import Daemon  # noqa: E402
from muselinux.index import ChatIndex  # noqa: E402
from muselinux.testing import FakeMuse, fake_doctor  # noqa: E402


@pytest.fixture()
def daemon_socket(tmp_path):
    muse = FakeMuse(lambda text: f"**Echo:** {text}",
                    activity="Reading your message")
    muse.add_history(None, "user", "earlier")
    muse.add_history(None, "assistant", "an earlier *answer*")
    index = ChatIndex(tmp_path / "chats.json")
    side = index.create("Side one")
    sock = str(tmp_path / "d.sock")
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    daemon = Daemon(sock, index, connect=muse.connect, doctor=fake_doctor)

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.run(stop))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(200):
        if os.path.exists(sock):
            break
        time.sleep(0.01)
    yield sock, muse, side
    loop.call_soon_threadsafe(stop.set)
    thread.join(5)


def drive(app, steps, timeout=20.0):
    """Run the app, stepping through a generator of conditions."""
    deadline = time.monotonic() + timeout
    failure = []
    gen = steps()
    state = {"wait": next(gen)}

    def tick():
        try:
            if time.monotonic() > deadline:
                failure.append("timed out waiting for: " + state["wait"].__doc__
                               if state["wait"].__doc__ else "timed out")
                app.quit_now()
                return GLib.SOURCE_REMOVE
            if state["wait"]():
                state["wait"] = gen.send(None)
        except StopIteration:
            app.quit_now()
            return GLib.SOURCE_REMOVE
        except Exception as exc:  # surface assertion errors
            failure.append(repr(exc))
            app.quit_now()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(50, tick)
    app.run([])
    assert not failure, failure[0]


def test_open_chat_send_and_stream(daemon_socket, tmp_path, monkeypatch):
    from muselinux.gnome.app import MuseApp

    sock, muse, side = daemon_socket
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    app = MuseApp(sock, initial_chat="main")
    app.set_application_id(None)  # no D-Bus uniqueness in tests
    seen = {}

    def steps():
        win = lambda: app.window  # noqa: E731

        def history_loaded():
            "main chat history"
            w = win()
            return w is not None and "main" in w.states and \
                w.states["main"].loaded and w.chat_view.rows
        yield history_loaded
        w = win()
        assert set(w.chats) == {"main", side["id"]}
        assert [m.text for m in w.states["main"].messages] == [
            "earlier", "an earlier *answer*"]
        assert w.chat_view.mirror_note.get_visible()
        w.chat_view.entry.get_buffer().set_text("hello from GTK")
        w.chat_view.send()

        def reply_done():
            "the streamed reply"
            msgs = w.states["main"].messages
            return msgs and msgs[-1].role == "assistant" and msgs[-1].done \
                and msgs[-1].text == "**Echo:** hello from GTK"
        yield reply_done
        assert muse.sent == [("hello from GTK", None)]
        texts = [m.text for m in w.states["main"].messages]
        assert texts.count("hello from GTK") == 1
        seen["rows"] = len(w.chat_view.rows)

        w.select(side["id"])

        def side_ready():
            "the side chat"
            s = w.states[side["id"]]
            return s.loaded and w.chat_view.state is s
        yield side_ready
        assert w.chat_view.empty.get_visible()
        assert "first message" in w.chat_view.title.get_subtitle()

    drive(app, steps)
    assert seen["rows"] == 4
