# Architecture

## Layers

```
GUI / front ends (Jan, LibreChat, a native client, ...)
        |                     |
        | OpenAI-compatible   | direct integration
        v                     v
   muselinux.shim      muselinux library (ask, history, index, probe)
                              |
                       musegadget LinkSession
                    (ephemeral, register_device=False)
                              |
                    Noise session to the Muse VM
```

- `muselinux.link` owns connections: pairing-store loading, VM lookup,
  and the ephemeral-session context manager.
- `muselinux.ask` owns one conversational turn: subscribe first, wait
  out the subscribe-time state bundle, then send and follow the turn
  with the SDK's `MuseTurn`. The bundle ordering rule is load-bearing;
  it is documented in the module.
- `muselinux.history` parses `GET /chat/history` envelopes into
  messages.
- `muselinux.index` is the local chat index (JSON under
  `~/.local/share/muse-linux/`). It exists because the server offers
  this credential no enumeration route.
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
  never printed, logged or written anywhere by this project.
- The shim binds to localhost by default. It has no authentication of
  its own because the trust boundary is the machine; do not expose it
  on a network without putting an authenticating proxy in front.
- Messages sent through this client are attributed by the server to
  the paired device, like any gadget-originated message.
