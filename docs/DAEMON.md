# The muse-linux daemon

`muse-linux daemon` (systemd unit `muse-linuxd.service`) is the one
process on a desktop that touches the Muse credential. The desktop
clients talk to it over a local socket and never see a token.

## Why a daemon

The Muse Gadget SDK's installer keeps the pairing store at
`/var/lib/musegadget` readable by root only (mode 0700). A desktop app
runs as a normal user, so it cannot open a Link session itself.
Loosening the store's permissions would hand the device credential to
every process the user runs; asking for a password on every launch
would be worse to live with. So one small root service reads the
pairing, and everything else asks it for chat operations.

That also gives one long-lived session instead of one per turn, so a
client can show live status and replies arriving in chats it is not
typing in, and two front ends (GNOME now, KDE later) share one
session and one chat index.

## What it does

- Reads the pairing in process, fetches the leased VM, and opens one
  ephemeral Link session (`register_device=False`). It never sends
  `link.register`, so it never displaces the machine's own gadget
  service and cannot receive device commands. It does not refresh
  tokens; the `musegadget` service does that, and the daemon picks up
  the refreshed pairing the next time it connects.
- Opens the session on demand: while at least one client is
  connected, and for five minutes after the last one leaves.
- Reconnects with exponential backoff (1 s doubling to 60 s, with
  jitter) when the session drops, and resubscribes every watched chat.
- Keeps one chat subscription per chat that any client is watching,
  shared between clients, at most 16 at a time. A client that starts
  watching a chat late gets the latest status, task and approvals
  state straight away.
- Translates Muse's raw chat events into typed events (`status` with
  the human activity text, `task`, `approvals`, `user_message`,
  `reply` snapshots) and serves commands for the chat index, history
  and sending. See [PROTOCOL.md](PROTOCOL.md).

## Security model

| Boundary | How it is held |
|---|---|
| Who can use the daemon | The socket is `/run/muse-linux/socket`, mode 0660, group `muse-linux`, inside `/run/muse-linux` (0750, root:muse-linux). Only root and members of `muse-linux` can connect. The daemon logs each client's uid. |
| What a client can learn | Chat content, the local chat index and a health report. Never the pairing, tokens, the VM id or the device node id (the doctor report strips it). |
| What a client can do | Exactly what the paired device can do in chat: send messages, read history, watch chats, manage the local index. Approvals cannot be decided at all (the credential is refused every decision route). |
| Network exposure | None. The daemon listens on the Unix socket only. Its only outbound traffic is to the Muse service. |
| Input handling | Requests are size-capped (1 MiB per line, 32 KiB per message, 200 characters per label); chat ids are validated; control characters are stripped from everything shown to clients. |
| Process | Runs as root without any capabilities (`CapabilityBoundingSet=` empty) and `NoNewPrivileges`. Root can read any root-owned file, so the sandbox hides what it does not need: no home directories, `/var/lib` replaced by an empty read-only tmpfs with only the pairing store (read-only) and its own state directory mounted in, `/etc/shadow`, `/etc/gshadow`, `/etc/ssh` and `/root` inaccessible, other processes invisible, `ProtectSystem=strict`, private /tmp and devices, a system call filter, restricted address families and namespaces, `MemoryMax=512M`. |
| Slow or stuck clients | Each client has its own bounded outgoing queue and writer; a client that stops reading is disconnected instead of slowing the others. Each client can have at most 8 requests in flight. |

Anyone in the `muse-linux` group can act as the paired device in chat.
That is the same trust boundary as the existing web API and shim on
localhost, made explicit with a group instead of "any local process".

Messages sent through any client are attributed by Muse to the paired
device, like any gadget-originated message.

## Files

| Path | What |
|---|---|
| `/opt/muse-linux/venv` | The daemon and clients (a venv with system site packages, so PyGObject comes from the distro) |
| `/etc/systemd/system/muse-linuxd.service` | The unit |
| `/run/muse-linux/socket` | The socket |
| `/var/lib/muse-linux/chats.json` | The chat index, shared with the web service and the terminal tools. Writes take a lock and re-read the file, so the processes never drop each other's chats. |

## Install, update, remove

```sh
sudo ./install.sh                    # install or update, starts the daemon
sudo ./install.sh --uninstall        # remove; keeps the chat index
sudo ./install.sh --uninstall --purge
./install.sh --dry-run               # show what would happen
```

The installer creates the `muse-linux` group and adds the user who ran
`sudo` to it. Group membership applies at the next login.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Client says the daemon is not running | `sudo systemctl start muse-linuxd`; logs with `journalctl -u muse-linuxd` |
| Client says you are not allowed to use the socket | `sudo usermod -aG muse-linux $USER`, then log out and back in |
| Status shows "not paired" | Pair the machine with the Muse app using the Muse Gadget SDK for Linux |
| Status shows the VM lookup failing (HTTP 401/403) | Make sure the `musegadget` service is running so it can refresh its tokens, or re-pair |
| Muse session keeps reconnecting | Check network reachability to the Muse service; the reason is in the Status dialog and in the journal |

To run the daemon by hand for development (it needs read access to the
pairing store, so as root), point it at a scratch index and a socket
your user can reach:

```sh
sudo MUSE_LINUX_HOME=/tmp/ml-dev .venv/bin/muse-linux daemon \
  --socket /tmp/ml-dev.sock --socket-group "$(id -gn)"
```

`tools/demo_daemon.py` runs the real daemon against an in-memory Muse
with made-up chats, which needs no pairing at all.
