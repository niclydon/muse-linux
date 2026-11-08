#!/usr/bin/env python3
"""Recorded end-to-end smoke test of the GNOME client.

Starts a throwaway headless GNOME Shell (X11 on Xvfb), records the
screen with ffmpeg, opens Muse for Linux against a running daemon,
and drives it with xdotool: it sends two messages in a dedicated side
chat, waits for the replies through a second daemon connection,
renames the chat, opens the status dialog, and takes a screenshot at
each step. Everything is torn down at the end.

The chat it uses is created fresh for the run, and the client opens
straight into it, so no other chat's history ever appears on screen.

    python3 tools/smoke_gnome.py --socket /run/muse-linux/socket --out out/

Needs: Xvfb, gnome-shell, dbus-run-session, xdotool, ffmpeg and
ImageMagick's import, plus the client's own GTK 4 / libadwaita.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from muselinux.client import DaemonClient  # noqa: E402

APP_ID = "io.github.niclydon.MuseLinux"
MESSAGES = [
    "This is an automated smoke test of the muse-linux desktop client. "
    "Please reply with exactly: Smoke test received.",
    "Thanks. Now reply with only a Markdown list of two items: the word "
    "alpha in bold, then the word beta in italics.",
]


class Session:
    def __init__(self, display: str, size: str, workdir: Path):
        self.display = display
        self.size = size
        self.workdir = workdir
        self.procs: list[subprocess.Popen] = []
        self.env = dict(os.environ)
        runtime = workdir / "run"
        runtime.mkdir(mode=0o700)
        data = workdir / "share"
        (data / "applications").mkdir(parents=True)
        icons = data / "icons/hicolor/scalable/apps"
        icons.mkdir(parents=True)
        shutil.copy(ROOT / "data" / f"{APP_ID}.desktop",
                    data / "applications" / f"{APP_ID}.desktop")
        shutil.copy(ROOT / "src/muselinux/gnome/data/icons/hicolor/scalable"
                    f"/apps/{APP_ID}.svg", icons / f"{APP_ID}.svg")
        self.env.update({
            "DISPLAY": display, "XDG_SESSION_TYPE": "x11",
            "XDG_CURRENT_DESKTOP": "GNOME", "GDK_BACKEND": "x11",
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_DATA_HOME": str(data),
            "XDG_DATA_DIRS": f"{data}:/usr/local/share:/usr/share",
            "XDG_CONFIG_HOME": str(workdir / "config"),
            "LIBGL_ALWAYS_SOFTWARE": "1", "GTK_A11Y": "none",
            "NO_AT_BRIDGE": "1",
        })

    def start(self):
        w, h = self.size.split("x")
        self.procs.append(subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", f"{w}x{h}x24",
             "-nolisten", "tcp"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True))
        time.sleep(1.5)
        bus_file = self.workdir / "bus"
        shell = subprocess.Popen(
            ["dbus-run-session", "--", "sh", "-c",
             f'echo "$DBUS_SESSION_BUS_ADDRESS" > {bus_file}; '
             "exec gnome-shell --x11 --replace"],
            env=self.env, stdout=open(self.workdir / "shell.log", "w"),
            stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(shell)
        for _ in range(100):
            if bus_file.exists() and bus_file.read_text().strip():
                break
            time.sleep(0.1)
        self.env["DBUS_SESSION_BUS_ADDRESS"] = bus_file.read_text().strip()
        time.sleep(6)
        # Under load, Xvfb key repeat turns xdotool's key presses into
        # doubled letters; switch repeat off in this throwaway session's
        # own settings (XDG_CONFIG_HOME points into the work directory).
        self.run("gsettings", "set", "org.gnome.desktop.peripherals.keyboard",
                 "repeat", "false")
        self.run("xset", "r", "off")
        self.key("Escape")      # leave the overview the shell starts in
        time.sleep(1)

    def run(self, *args, check=False):
        return subprocess.run(list(args), env=self.env, check=check,
                              capture_output=True, text=True)

    def key(self, *keys):
        self.run("xdotool", "key", "--clearmodifiers", *keys)

    def type(self, text):
        self.run("xdotool", "type", "--delay", "35", text)

    def screenshot(self, path: Path, window=None):
        # the whole screen, so the GNOME Shell top bar is in the picture
        self.run("import", "-window", "root", str(path))

    def find_window(self, name, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self.run("xdotool", "search", "--name", name).stdout.split()
            if out:
                return out[-1]
            time.sleep(0.3)
        raise RuntimeError(f"no window named {name!r}")

    def stop(self):
        for proc in reversed(self.procs):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(5)
            except Exception:
                proc.kill()


class Watcher:
    """A second daemon connection that waits for replies."""

    def __init__(self, socket_path):
        self.events = []
        self.cond = threading.Condition()
        self.client = DaemonClient(socket_path, on_event=self._on).start()
        if not self.client.wait_connected(10):
            raise RuntimeError(self.client.last_problem or "daemon unreachable")
        time.sleep(0.5)

    def _on(self, event):
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def wait_reply(self, chat, after, timeout=240):
        deadline = time.time() + timeout
        with self.cond:
            while time.time() < deadline:
                for event in self.events[after:]:
                    if event.get("event") == "reply" and \
                            event.get("chat") == chat and event.get("done") \
                            and event.get("text"):
                        return event
                self.cond.wait(1)
        raise RuntimeError("no reply arrived")

    def wait_idle(self, chat, timeout=60):
        """Until the status line would hide again."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.cond:
                status = [e for e in self.events if e.get("chat") == chat
                          and e.get("event") in ("status", "task")]
            if status:
                last = {e["event"]: e for e in status}
                code = last.get("status", {}).get("code")
                task = last.get("task", {}).get("status")
                if code in (None, "idle", "online") and \
                        task in (None, "completed", "failed", "errored",
                                 "cancelled", "canceled"):
                    return
            time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--display", default=":99")
    parser.add_argument("--size", default="1440x900")
    parser.add_argument("--label", default="Smoke test")
    parser.add_argument("--client", default=str(Path(sys.executable).parent
                                                 / "muse-linux-gnome"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="muse-smoke-"))
    log = []
    ok = False

    def note(step, **extra):
        entry = {"t": round(time.time() - started, 1), "step": step, **extra}
        log.append(entry)
        print(json.dumps(entry), flush=True)

    watcher = Watcher(args.socket)
    chat = watcher.client.request("chats.new", label=args.label)
    watcher.client.watch(chat["id"]).result(30)
    session = Session(args.display, args.size, workdir)
    recorder = app = None
    started = time.time()
    try:
        session.start()
        note("gnome shell up")
        recorder = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "x11grab",
             "-video_size", args.size, "-framerate", "12",
             "-i", args.display, "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "30", "-pix_fmt", "yuv420p",
             str(out / "smoke-test.mp4")],
            env=session.env, stdin=subprocess.PIPE)
        app = subprocess.Popen(
            [args.client, "--socket", args.socket, "--chat", chat["id"]],
            env=session.env, stdout=open(workdir / "app.log", "w"),
            stderr=subprocess.STDOUT)
        win = session.find_window("Muse for Linux")
        session.run("xdotool", "windowactivate", "--sync", win)
        time.sleep(3)
        note("client open")
        session.screenshot(out / "01-new-side-chat.png", win)

        for n, text in enumerate(MESSAGES, 1):
            before = len(watcher.events)
            session.key("ctrl+l")
            session.type(text)
            time.sleep(0.4)
            session.key("Return")
            note(f"message {n} sent")
            time.sleep(2.5)
            session.screenshot(out / f"0{n + 1}-working-{n}.png", win)
            reply = watcher.wait_reply(chat["id"], before)
            note(f"reply {n} done", chars=len(reply["text"]))
            watcher.wait_idle(chat["id"])
            time.sleep(2)
        session.screenshot(out / "04-replies.png", win)

        session.key("F2")
        time.sleep(1)
        session.key("ctrl+a")
        session.type("Smoke test, renamed")
        time.sleep(0.5)
        session.screenshot(out / "05-rename.png", win)
        session.key("Return")
        time.sleep(1.5)
        note("renamed")

        session.key("ctrl+r")
        time.sleep(4)
        session.screenshot(out / "06-history-reloaded.png", win)
        note("history reloaded")

        session.key("ctrl+comma")
        time.sleep(4)
        session.screenshot(out / "07-status.png", win)
        session.key("Escape")
        time.sleep(1)
        note("status dialog")

        history = watcher.client.request("history", chat=chat["id"], limit=20)
        roles = [m["role"] for m in history["messages"]]
        note("history check", roles=roles)
        ok = roles.count("user") >= 2 and roles.count("assistant") >= 2
        time.sleep(1.5)
    finally:
        if recorder is not None:
            try:
                recorder.communicate(b"q", timeout=20)
            except Exception:
                recorder.kill()
        if app is not None:
            app.terminate()
            try:
                app.wait(5)
            except Exception:
                app.kill()
        session.stop()
        watcher.client.close()
        (out / "smoke-log.json").write_text(json.dumps({
            "chat_label": args.label, "steps": log}, indent=2))
        shutil.rmtree(workdir, ignore_errors=True)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
