# muse-linux

An unofficial Linux client for Meta Muse, built on the
[Muse Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk).

> **Unofficial.** This is a community project. It is not made by,
> endorsed by, or affiliated with Meta. "Muse" is used only to describe
> what the software talks to. You need a Muse account and a Linux
> machine paired as a Muse gadget (see Quickstart).

The Muse apps exist for Mac, iOS and the web. This project is the
missing Linux piece: a terminal client today, an OpenAI-compatible
shim that lets existing chat front ends talk to Muse today, and a
small Python library (`muselinux`) that a desktop GUI can be built on.

## What it does

- **Chat in the terminal** with your actual Muse: the main chat and
  side chats addressed by their real session ids, with streaming
  replies and live working status.
- **History**: read a chat's canonical transcript.
- **Side chats**: create new ones, keep a local index of the chats you
  use, rename them locally.
- **A local OpenAI-compatible shim** (`/v1/models`,
  `/v1/chat/completions`, streaming included) so front ends such as
  Jan, LibreChat or Open WebUI can use Muse as their backend with no
  code changes on their side.
- **Probes** that map exactly which parts of the Muse VM API the
  gadget credential can reach, so the client's limits are measured,
  not guessed (see [docs/PROBE-RESULTS.md](docs/PROBE-RESULTS.md)).
  The whole stack is smoke-tested end to end against the production
  service (see [docs/SMOKE-TEST.md](docs/SMOKE-TEST.md)).

## How it works, in one paragraph

A Linux machine paired as a Muse gadget holds a device credential.
That credential can open *ephemeral* Link sessions (the SDK's
`LinkSession` with device registration off) that never displace the
machine's gadget service. Over those sessions the client sends chat
messages, subscribes to chat events, and reads history through the
same protocol the gadget SDK documents. The credential's scopes are
narrow, and the client is designed around exactly what they allow;
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the full picture.

## Quickstart

Prerequisites: Python 3.11+, a Muse account, and this machine (or
another Linux machine you control) already paired as a Muse gadget
with the Muse Gadget SDK for Linux, so a pairing store exists at
`/var/lib/musegadget` or `~/.musegadget`.

```sh
# the SDK provides the Link transport
pip install "musegadget @ git+https://github.com/facebookincubator/muse-gadget-sdk#subdirectory=linux"

git clone <this repo> && cd muse-linux
pip install -e .

muse-linux doctor          # pairing + VM reachability, no secrets printed
muse-linux chats           # your local chat index
muse-linux chat            # interactive terminal chat
muse-linux ask "summarize my morning" --chat main
muse-linux shim            # OpenAI-compatible shim on 127.0.0.1:8765
```

Point any OpenAI-compatible front end at `http://127.0.0.1:8765/v1`
with any placeholder API key. Models: `muse` is the main chat;
`muse-chat-<session-id>` is a side chat.

## GUI

A desktop-style client lives in [gui/](gui/): an
[assistant-ui](https://www.assistant-ui.com) front end over a native
backend API (`muse-linux serve`), chosen over the shim so working
status, task state and approval snapshots stay visible. It runs as a
web app served by the backend itself, or as a Tauri desktop app
(.deb / AppImage) from `gui/src-tauri/`. See
[gui/README.md](gui/README.md).

## Known limits

- **No chat enumeration.** The gadget credential is refused every
  enumeration route (HTTP 403, "route not allowed for this
  credential's scopes"), so the chat list is a local index of chats
  this client created or was given. Chats made in the phone or Mac
  app appear once you add their id with `muse-linux chats add`.
- **No VM file access.** `/fs/read` and `/fs/write` are outside the
  credential's scopes as well.
- **Shim flattening.** Working status, task state and approval
  requests have no representation in the OpenAI Chat Completions
  format; shim users get clean streaming chat without them. The
  `muselinux` library exposes them natively for front ends that
  integrate directly.
- **Voice** (voice notes out, TTS and realtime calls) is in the SDK
  and on the roadmap; it is not wired into the CLI yet.

## Development

```sh
pip install -e ".[test]"
pytest
```

Tests run anywhere: they drive a fake session that speaks the SDK's
event shapes, no pairing or network needed.

## License

Apache-2.0. See [LICENSE](LICENSE).
