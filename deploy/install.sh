#!/usr/bin/env bash
# Install or update the muse-linux service on a paired Linux host.
#
# What it does:
#   1. Creates (or reuses) the service venv at /services/muse-linux/venv
#      and installs the Muse Gadget SDK (from $SDK_DIR) plus this
#      package with the TUI extra.
#   2. Builds the web GUI (npm ci + npm run build) and copies the
#      bundle to /services/muse-linux/gui, so the deployed GUI is a
#      reproducible build of the repo, never a hand-carried artifact.
#   3. Installs the systemd unit, reloads systemd, restarts the
#      service, and smoke-checks /api/status.
#
# Usage:  sudo ./deploy/install.sh [REPO_DIR]
# Env:    SDK_DIR   path to the muse-gadget-sdk checkout (default:
#                   ~/musegadget-sdk)
#         PORT      service port (default 8770)
set -euo pipefail

REPO_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
SDK_DIR="${SDK_DIR:-$HOME/musegadget-sdk}"
PORT="${PORT:-8770}"
PREFIX=/services/muse-linux
VENV="$PREFIX/venv"

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo: the pairing store and the service are root-owned" >&2
  exit 1
fi
if [[ ! -d "$SDK_DIR/linux" ]]; then
  echo "SDK checkout not found at $SDK_DIR (set SDK_DIR)" >&2
  exit 1
fi

echo "==> venv + packages"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "$SDK_DIR/linux"
"$VENV/bin/pip" install -q --force-reinstall --no-deps "$REPO_DIR"
"$VENV/bin/pip" install -q "$REPO_DIR[tui]"

echo "==> web GUI build"
(cd "$REPO_DIR/gui" && npm ci --no-audit --no-fund && npm run build)
rm -rf "$PREFIX/gui"
cp -r "$REPO_DIR/gui/dist" "$PREFIX/gui"

echo "==> systemd unit"
sed "s|--port 8770|--port $PORT|" "$REPO_DIR/deploy/muse-linux.service" \
  > /etc/systemd/system/muse-linux.service
systemctl daemon-reload
systemctl enable muse-linux.service >/dev/null
systemctl restart muse-linux.service

echo "==> smoke check"
for i in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:$PORT/api/status" >/tmp/muse-linux-status.json 2>/dev/null; then
    cat /tmp/muse-linux-status.json
    echo
    echo "install complete: service on port $PORT"
    exit 0
  fi
  sleep 1
done
echo "service did not answer on port $PORT; check: systemctl status muse-linux" >&2
exit 1
