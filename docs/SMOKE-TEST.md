# Smoke test, 2026-11-08

Run on a Linux host paired as a Muse gadget (x86_64, Python 3.12,
SDK 0.1.0), against the production Muse service. The test chat was a
new side chat created by this client; no existing chat was written to.

| Step | Result |
|---|---|
| `muse-linux doctor` | Paired: true. VM fetch: HTTP 200, 1 VM. |
| `muse-linux chats new` | Local index entry created with a fresh UUID session id. |
| `muse-linux ask` (first message) | Reply received verbatim as requested; completion via task terminal status. First turn of a brand-new chat is slow (agent provisioning); subsequent turns complete in seconds. |
| `muse-linux ask` (second message) | Streamed reply with live `agent.status` codes on stderr; ~8 s end to end. |
| `muse-linux history` | Both turns present, in order, correct roles. |
| `muse-linux probe` | Matches PROBE-RESULTS.md exactly: history allowed (main and side), enumeration and `/fs/read` scope-refused, control route identically refused. |
| Shim `GET /v1/models` | `muse` plus one `muse-chat-<id>` per indexed chat. |
| Shim completion, non-streaming | Correct `chat.completion` JSON with the reply text. |
| Shim completion, streaming | SSE content chunks, a `finish_reason: "stop"` chunk, then `[DONE]`; connection closes cleanly. |

## Defects found and fixed during the smoke

1. The identity store on a real pairing may hold only a MAC address,
   not a node id. `load_node_id` now lets the SDK derive the id, with
   the plain file fields as fallback.
2. Turn cleanup could hang: cancelling the subscription pump and
   closing the session are now both time-bounded, so a client process
   always exits.
3. The shim's streaming responses did not close the connection after
   `[DONE]` for HTTP/1.1 clients; they now send `Connection: close`.
