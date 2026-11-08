#!/usr/bin/env bash
# Install muse-linux for the desktop: the daemon (a system service that
# holds the Muse session) and the GNOME client.
#
#   sudo ./install.sh                 install or update
#   sudo ./install.sh --uninstall     remove (keeps the chat index)
#   sudo ./install.sh --uninstall --purge   also remove the chat index
#   ./install.sh --dry-run            print what would happen
#
# Options:
#   --user NAME     add NAME to the muse-linux group (default: the
#                   user who ran sudo)
#   --no-gnome      daemon only, no desktop entry
#   --no-start      install but do not enable or start the daemon
#   PYTHON=...      interpreter to build the venv from (default python3)
#   --sdk PATH      a Muse Gadget SDK checkout to install the Link
#                   transport from (default: $SDK_DIR, then
#                   ~/musegadget-sdk, then the SDK's GitHub repo)
#
# Prerequisites: Python 3.11+, systemd, and this machine paired as a
# Muse gadget with the Muse Gadget SDK for Linux. For the GNOME client:
# GTK 4 and libadwaita 1.5+ with their GObject introspection data
# (Ubuntu/Debian: python3-gi gir1.2-gtk-4.0 gir1.2-adw-1;
#  Fedora: python3-gobject gtk4 libadwaita).
set -euo pipefail

PREFIX=/opt/muse-linux
VENV="$PREFIX/venv"
UNIT=/etc/systemd/system/muse-linuxd.service
BIN_DIR=/usr/local/bin
APPS_DIR=/usr/local/share/applications
ICON_DIR=/usr/local/share/icons/hicolor/scalable/apps
APP_ID=io.github.niclydon.MuseLinux
GROUP=muse-linux
STATE_DIR=/var/lib/muse-linux
SOCKET=/run/muse-linux/socket

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION=install
PURGE=0
DRY=0
GNOME=1
START=1
TARGET_USER="${SUDO_USER:-}"
SDK="${SDK_DIR:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall) ACTION=uninstall ;;
    --purge) PURGE=1 ;;
    --dry-run) DRY=1 ;;
    --no-gnome) GNOME=0 ;;
    --no-start) START=0 ;;
    --user|--sdk)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "$1 needs a value" >&2; exit 2
      fi
      if [[ $1 == --user ]]; then TARGET_USER="$2"; else SDK="$2"; fi
      shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '==> %s\n' "$*"; }
run() {
  if [[ $DRY -eq 1 ]]; then printf '    would run: %s\n' "$*"; else "$@"; fi
}

if [[ $DRY -eq 0 && $EUID -ne 0 ]]; then
  echo "run with sudo: the daemon is a system service" >&2
  exit 1
fi

uninstall() {
  say "stopping and removing the daemon"
  if command -v systemctl >/dev/null; then
    run systemctl disable --now muse-linuxd.service 2>/dev/null || true
  fi
  run rm -f "$UNIT" "$BIN_DIR/muse-linux-gnome" \
    "$APPS_DIR/$APP_ID.desktop" "$ICON_DIR/$APP_ID.svg"
  run rm -rf "$PREFIX"
  if command -v systemctl >/dev/null; then run systemctl daemon-reload; fi
  if command -v update-desktop-database >/dev/null; then
    run update-desktop-database -q "$APPS_DIR" || true
  fi
  if command -v gtk-update-icon-cache >/dev/null; then
    run gtk-update-icon-cache -q /usr/local/share/icons/hicolor || true
  fi
  if [[ $PURGE -eq 1 ]]; then
    say "removing the chat index in $STATE_DIR"
    say "(the web service and terminal tools share it, if you use them)"
    run rm -rf "$STATE_DIR"
  else
    say "kept $STATE_DIR (the chat index); --purge removes it"
  fi
  say "kept the $GROUP group; remove it with: groupdel $GROUP"
}

if [[ $ACTION == uninstall ]]; then
  uninstall
  exit 0
fi

# -- checks ---------------------------------------------------------------
PYTHON="${PYTHON:-$(command -v python3 || true)}"
if [[ -z "$PYTHON" ]]; then echo "python3 not found" >&2; exit 1; fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required" >&2; exit 1
fi
if ! command -v systemctl >/dev/null; then
  echo "systemd is required for the daemon" >&2; exit 1
fi
if ! "$PYTHON" -c 'import ensurepip, venv' 2>/dev/null; then
  echo "Python's venv module is missing:" >&2
  echo "  Ubuntu/Debian: sudo apt install python3-venv" >&2
  exit 1
fi
if [[ ! -e /var/lib/musegadget/pairing.json && $DRY -eq 0 ]]; then
  echo "note: no pairing found at /var/lib/musegadget yet; the daemon will"
  echo "      report 'not paired' until this machine is paired with Muse."
fi
if [[ $GNOME -eq 1 ]]; then
  if ! "$PYTHON" - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5)
PY
  then
    echo "The GNOME client needs GTK 4 and libadwaita 1.5+ for Python:" >&2
    echo "  Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" >&2
    echo "  Fedora:        sudo dnf install python3-gobject gtk4 libadwaita" >&2
    echo "Install them and run this again, or pass --no-gnome." >&2
    exit 1
  fi
fi

if [[ -z "$SDK" ]]; then
  CALLER_HOME="$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)"
  if [[ -d "${CALLER_HOME:-/nonexistent}/musegadget-sdk/linux" ]]; then
    SDK="$CALLER_HOME/musegadget-sdk"
  fi
fi
if [[ -n "$SDK" ]]; then
  SDK_SPEC="$SDK/linux"
  [[ -d "$SDK_SPEC" || $DRY -eq 1 ]] || { echo "no linux/ in $SDK" >&2; exit 1; }
else
  SDK_SPEC="musegadget @ git+https://github.com/facebookincubator/muse-gadget-sdk#subdirectory=linux"
  if ! command -v git >/dev/null; then
    echo "git is needed to fetch the Muse Gadget SDK (or pass --sdk DIR)" >&2
    exit 1
  fi
fi

# -- install --------------------------------------------------------------
say "group $GROUP"
if ! getent group "$GROUP" >/dev/null; then
  run groupadd --system "$GROUP"
fi
if [[ -n "$TARGET_USER" && "$TARGET_USER" != root ]]; then
  if id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$GROUP"; then
    say "$TARGET_USER is already in $GROUP"
  else
    run usermod -aG "$GROUP" "$TARGET_USER"
    NEW_MEMBER=1
  fi
fi

say "virtualenv at $VENV (system site packages, for PyGObject)"
run mkdir -p "$PREFIX"
run "$PYTHON" -m venv --system-site-packages "$VENV"
run "$VENV/bin/pip" install -q --upgrade pip
run "$VENV/bin/pip" install -q "$SDK_SPEC"
# build from a copy so root never writes build files into the checkout
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
run cp -a "$REPO_DIR/." "$BUILD_DIR/src"
run rm -rf "$BUILD_DIR/src/.git" "$BUILD_DIR/src/docs/media"
run "$VENV/bin/pip" install -q --force-reinstall --no-deps "$BUILD_DIR/src"

say "systemd unit"
run install -m 0644 "$REPO_DIR/deploy/muse-linuxd.service" "$UNIT"
run systemctl daemon-reload

if [[ $GNOME -eq 1 ]]; then
  say "GNOME client"
  run ln -sf "$VENV/bin/muse-linux-gnome" "$BIN_DIR/muse-linux-gnome"
  run install -D -m 0644 "$REPO_DIR/data/$APP_ID.desktop" "$APPS_DIR/$APP_ID.desktop"
  run install -D -m 0644 \
    "$REPO_DIR/src/muselinux/gnome/data/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$ICON_DIR/$APP_ID.svg"
  if command -v update-desktop-database >/dev/null; then
    run update-desktop-database -q "$APPS_DIR" || true
  fi
  if command -v gtk-update-icon-cache >/dev/null; then
    run gtk-update-icon-cache -q /usr/local/share/icons/hicolor || true
  fi
fi

if [[ $START -eq 1 ]]; then
  say "starting the daemon"
  run systemctl enable muse-linuxd.service
  run systemctl restart muse-linuxd.service
  if [[ $DRY -eq 0 ]]; then
    for _ in $(seq 1 30); do
      [[ -S "$SOCKET" ]] && break
      sleep 0.5
    done
    if [[ ! -S "$SOCKET" ]]; then
      echo "the daemon did not open $SOCKET; check: journalctl -u muse-linuxd" >&2
      exit 1
    fi
    "$VENV/bin/python" - "$SOCKET" <<'PY'
import sys
from muselinux.client import DaemonClient
client = DaemonClient(sys.argv[1], reconnect=False).start()
client.wait_connected(5)
info = client.request("hello", timeout=10)
print(f"daemon answering: version {info['version']}, protocol {info['protocol']}")
client.close()
PY
  fi
fi

say "done"
if [[ "${NEW_MEMBER:-0}" -eq 1 ]]; then
  echo "$TARGET_USER was added to the $GROUP group. Log out and back in"
  echo "(or reboot) before opening Muse for Linux, so the group applies."
fi
