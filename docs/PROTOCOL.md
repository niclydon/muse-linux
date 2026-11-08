# Daemon socket protocol, version 1

Desktop clients talk to the daemon over a Unix stream socket
(`/run/muse-linux/socket` by default; override with
`MUSE_LINUX_SOCKET`). Each message is one JSON object on one line,
UTF-8, newline terminated, at most 1 MiB. The names, limits and event
parsing live in `src/muselinux/protocol.py`, and
`src/muselinux/client.py` is a ready-made client.

## Requests and responses

```json
{"id": 7, "cmd": "send", "args": {"chat": "main", "text": "hi"}}
{"id": 7, "ok": true, "result": {"chat": "main", "message_id": "..."}}
{"id": 7, "ok": false, "error": {"code": "not_connected", "message": "..."}}
```

Requests may be pipelined; responses carry the request's `id` and can
arrive in any order. A line that is not valid JSON gets a response with
`"id": null`, and the connection stays open.

Error codes: `bad_request`, `unknown_command`, `unknown_chat`,
`not_connected` (no Muse session yet; retry), `upstream` (Muse
answered with an error), `internal`.

A `chat` argument is a chat's index id or its session id. The main chat
is `main`.

## Commands

| Command | Arguments | Result |
|---|---|---|
| `hello` | none | `protocol`, `version`, `connection`, `error` |
| `doctor` | none | `daemon` (version, protocol, connection, error, connected_since, watches, clients) and `pairing` (state_dir, paired, vm_fetch_status, vm_count, error). Never includes tokens, VM or node ids. |
| `chats.list` | none | `chats`: a list of chats |
| `chats.new` | `label` | the new side chat. It exists on the server once it gets its first message. |
| `chats.add` | `session_id`, `label` | adds a chat known by id (for example copied from the phone app) |
| `chats.rename` | `chat`, `label` | the renamed chat (labels are local) |
| `chats.remove` | `chat` | removes a side chat from the local index only |
| `history` | `chat`, `limit` (1 to 200, default 50) | `messages`, `has_more`, `limit`; `not_started: true` for a side chat with no first message |
| `watch` | `chat` | starts (or joins) the chat's live subscription; returns the watch `state` |
| `unwatch` | `chat` | stops this client's watch |
| `send` | `chat`, `text` (at most 32 KiB) | `message_id` of the sent message, once Muse accepts it. The reply arrives as events on a watched chat. |

A chat object: `id`, `label`, `session_id` (null for main), `is_main`,
`created_at` (seconds), `last_activity_ms`.

A history message: `role` (`user` or `assistant`), `text`,
`message_id`, `occurred_at_ms`, `reaction`.

## Events

Events have an `event` field and no `id`. Chat events go to the
clients watching that chat; the others go to every client.

| Event | Fields | Meaning |
|---|---|---|
| `connection` | `state` (`idle`, `connecting`, `connected`, `disconnected`), `error` | The daemon's Muse session |
| `chats.changed` | `chat` and/or `removed` | The index changed (by any client, or new activity) |
| `watch` | `chat`, `state` (`pending`, `live`, `not_started`, `error`), `error` | A watched chat's subscription |
| `status` | `chat`, `code`, `text` | Muse's activity, with its human text (for example "is working"). `idle` and `online` mean not working. |
| `task` | `chat`, `status` | Task state (`running`, `completed`, `failed`, ...) |
| `approvals` | `chat`, `pending` (`id`, `title`, `detail`) | The current pending approvals; an empty list clears them. They can only be decided in the Muse app. |
| `user_message` | `chat`, `message_id`, `text`, `occurred_at_ms` | A user message, from this or any other device |
| `reply` | `chat`, `message_id`, `text`, `done`, `occurred_at_ms` | An assistant message as it stands: the full text so far, not a delta |

When a client starts watching a chat it first receives the chat's
current `watch` state and its latest `status`, `task` and `approvals`
events. A watch is a live mirror of the chat: the main chat's replies
do not say which message they answer, so a client should show events
in arrival order rather than pairing replies with its own sends.

## Versioning

`hello` returns the protocol version. A client must refuse to work
with a daemon whose major protocol differs and say so (the bundled
client does). New fields and new event types can be added within a
version; clients ignore what they do not know.
