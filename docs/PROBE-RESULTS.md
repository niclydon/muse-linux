# Probe results: what the gadget credential can reach

Measured 2026-11-08 against a production Muse VM, from a Linux host
paired as a Muse gadget, through ephemeral Link sessions
(`register_device=False`). Probes are read-only and record structure
and status only, never message content. Re-run them any time with
`muse-linux probe`.

## Allowed (HTTP 200)

| Route | Result |
|---|---|
| `GET /chat/history?limit=N` | Main chat transcript. Envelope keys: `channel`, `chat_events`, `has_more`, `transcript_revision`. Canonical events: `message.user`, `message.assistant`, `delta.presentation`; each carries `chat_context` with `chat_id` and `chat_kind`. |
| `GET /chat/history?limit=N&session_id=<id>` | Same, scoped to any side chat whose id the client supplies. |
| `POST /chat/subscribe` | Live event stream per chat: reply deltas plus `agent.status`, `task.status`, `approvals.snapshot`, `model_lock.updated` and others. Opens with a state bundle restating the previous turn. |
| `POST /chat/stream` | Send a message (SDK `send_chat`); documented SDK path, exercised by the smoke test. |
| Voice and TTS paths | Documented SDK paths (`voice-call`, `stream_tts`); server admission is per-credential. |

## Refused (HTTP 403, body: "route not allowed for this credential's scopes")

| Route | Note |
|---|---|
| `GET /chat/list`, `/chat/chats`, `/chat/threads`, `/chat/sessions` | Enumeration candidates. All refused identically. |
| `POST /fs/read` | VM file access, documented in the SDK README but outside this credential's scopes in production. |
| `GET /chat/nonexistent-route-zzz123` | Control probe. A made-up route returns the identical 403 and body, proving the refusal is a scope gate on the route set, not evidence about whether a route exists for other credentials. |

## Conclusions

1. The conversational core is fully reachable: send, subscribe,
   history, voice. A faithful chat client is buildable today.
2. Chat discovery is not. The client's chat list is necessarily a
   local index of known ids, enriched from history's `chat_context`.
3. Anything beyond chat (files, goals, app surfaces) is outside this
   credential's scopes and would need a differently-scoped credential
   or server-side changes by Meta.
