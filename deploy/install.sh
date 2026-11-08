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
#         ALLOWED_HOSTS  comma-separated Host names a reverse proxy
#                   forwards; detected from tailscale serve if unset
set -euo pipefail

REPO_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
# Under sudo, $HOME is root's; the SDK checkout usually lives in the
# invoking user's home.
CALLER_HOME="$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)"
SDK_DIR="${SDK_DIR:-${CALLER_HOME:-$HOME}/musegadget-sdk}"
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
"$VENV/bin/pip" install -q "${REPO_DIR}[tui]"

echo "==> web GUI build"
(cd "$REPO_DIR/gui" && npm ci --no-audit --no-fund && npm run build)
rm -rf "$PREFIX/gui"
cp -r "$REPO_DIR/gui/dist" "$PREFIX/gui"

echo "==> allowed host names"
# The API refuses Host names other than loopback unless they are listed
# in /etc/default/muse-linux. Write that file once: from ALLOWED_HOSTS
# if given, else from a tailscale serve proxy to this port, if any.
if [[ ! -f /etc/default/muse-linux ]]; then
  HOSTS="${ALLOWED_HOSTS:-}"
  if [[ -z "$HOSTS" ]] && command -v tailscale >/dev/null; then
    HOSTS="$(tailscale serve status --json 2>/dev/null | python3 -c '
import json, sys
port = sys.argv[1]
try:
    status = json.load(sys.stdin)
except Exception:
    status = {}
web = status.get("Web") or {}
funnel = {key for key, on in (status.get("AllowFunnel") or {}).items() if on}
names, public = set(), set()
for key, conf in web.items():
    for handler in (conf.get("Handlers") or {}).values():
        if str(handler.get("Proxy", "")).rstrip("/").endswith(":" + port):
            (public if key in funnel else names).add(key.rsplit(":", 1)[0])
for name in sorted(public):
    print("warning: " + name + " is on Tailscale Funnel (the public "
          "internet); not allowing it", file=sys.stderr)
print(",".join(sorted(names)))
' "$PORT" || true)"
    if [[ -z "$HOSTS" ]]; then
      echo "note: no tailscale serve entry proxies port $PORT; if you put the"
      echo "      API behind one later, add its name to /etc/default/muse-linux"
      echo "      as MUSE_LINUX_ALLOWED_HOSTS=<name>"
    fi
  fi
  if [[ -n "$HOSTS" ]]; then
    printf 'MUSE_LINUX_ALLOWED_HOSTS=%s\n' "$HOSTS" > /etc/default/muse-linux
    echo "allowed host names: $HOSTS (in /etc/default/muse-linux)"
  fi
fi

echo "==> systemd unit"
sed "s|--port 8770|--port $PORT|" "$REPO_DIR/deploy/muse-linux.service" \
  > /etc/systemd/system/muse-linux.service
systemctl daemon-reload
systemctl enable muse-linux.service >/dev/null
systemctl restart muse-linux.service

echo "==> smoke check"
for _ in $(seq 1 15); do
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
