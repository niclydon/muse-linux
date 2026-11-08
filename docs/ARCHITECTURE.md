# Architecture

## Layers

```
Desktop:  GNOME client (GTK4/libadwaita)    KDE client (next)
                     \                        /
                      muselinux.client: Unix socket, JSON lines
                                  |
              muse-linux daemon (root, systemd): one long-lived
              session, a subscription per watched chat
                                  |
Others:   TUI (Textual)   Web GUI (assistant-ui)   External apps
              |                 | native API         | OpenAI
              |                 v                    v
              |          muselinux.server      muselinux.shim
              |                 |                    |
              +--------+--------+---------+----------+
                       |   (per-turn sessions; the daemon's one
                       |    session for desktop clients)
                 link / ask / history / voice / approvals / index
                       |
                musegadget LinkSession
             (ephemeral, register_device=False)
                       |
             Noise session to the Muse VM
```

- `muselinux.daemon` is the desktop's credential owner: it reads the
  root-only pairing, holds one session with reconnect and backoff,
  subscribes to watched chats and serves clients over a
  group-restricted Unix socket. See [DAEMON.md](DAEMON.md).
- `muselinux.protocol` defines the socket protocol
  ([PROTOCOL.md](PROTOCOL.md)) and turns raw chat events into typed
  ones; `muselinux.client` is the toolkit-agnostic client.
- `muselinux.gnome` is the GNOME client: `model` (per-chat state, no
  GTK), `markdown` (Markdown to escaped Pango markup, no GTK) and
  `app` (the widgets).
- `muselinux.testing` is an in-memory Muse for tests and the demo
  daemon.

- `muselinux.link` owns connections: pairing-store loading, VM lookup,
  and the ephemeral-session context manager.
- `muselinux.ask` owns one conversational turn: subscribe first, wait
  out the subscribe-time state bundle, then send and follow the turn
  with the SDK's `MuseTurn`. The bundle ordering rule is load-bearing;
  it is documented in the module. An optional `sender` callable
  replaces the text send, which is how voice turns reuse the same
  machinery.
- `muselinux.history` parses `GET /chat/history` envelopes into
  messages, including reactions. The route's measured window is
  200 events; the parser and the front ends treat that as the
  paging unit (see PROBE-RESULTS.md).
- `muselinux.index` is the local chat index (JSON; the service home
  `/var/lib/muse-linux/` when an installed service index exists,
  else `~/.local/share/muse-linux/`). It exists because the server
  offers this credential no enumeration route. Every change locks,
  re-reads and atomically rewrites the file, so the daemon, the web
  service and the terminal tools can share it.
- `muselinux.voice` sends WAV voice notes (requesting voice
  modality, which is what makes replies speakable) and collects
  TTS MP3 for an assistant message.
- `muselinux.approvals` parses `approvals.snapshot` payloads into
  display-ready pending approvals. It is read-only by measurement:
  decision routes are outside the credential's scopes.
- `muselinux.server` is the native API the web GUI talks to: JSON
  endpoints plus SSE turn streams carrying status, reply snapshots
  and structured approval events, a voice endpoint, a TTS endpoint,
  and optional static serving of the built GUI.
- `muselinux.tui` is the full-screen terminal UI, driving the same
  library directly.
- `muselinux.shim` is a stateless translation to OpenAI Chat
  Completions. Model ids carry the target chat (`muse`,
  `muse-chat-<session-id>`); only the last user message of a request
  is sent, because the backend owns conversation history.
- `muselinux.probe` re-runs the ceiling probes so the limits in
  PROBE-RESULTS.md can be re-measured after any server change.

## Why ephemeral sessions

The machine's gadget service holds a registered Link session so Muse
can run commands on the machine. A client that registered a second
device session for the same identity would fight it. Ephemeral
sessions skip `link.register` entirely: they are pure clients, they
cannot receive device commands, and any number can come and go without
disturbing the gadget.

## Security posture

- Pairing material is read from the SDK's own store, in process, and
  never printed, logged or written anywhere by this project. On a
  desktop only the daemon reads it; the clients hold no credential.
- The daemon's socket is usable by root and the `muse-linux` group
  only, and the daemon runs without capabilities under systemd
  sandboxing (see [DAEMON.md](DAEMON.md)).
- The shim and the web API bind to localhost by default. They have no
  authentication of their own because the trust boundary is local
  programs; do not expose them on a network without an authenticating
  proxy in front. Web pages the user opens are outside that boundary:
  `muselinux.localguard` refuses foreign `Host` names (DNS rebinding),
  foreign `Origin`s and cross-site requests, and writes need a JSON or
  WAV content type. See SECURITY.md.
- Messages sent through this client are attributed by the server to
  the paired device, like any gadget-originated message.
