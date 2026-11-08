# Verification record, 2026-11-08

End-to-end verification of the client against the production Muse
service, run on a Linux host paired as a Muse gadget (x86_64,
Python 3.12, SDK 0.1.0). The conversation used a new side chat
created by this client; no existing chat was written to.

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

## Addendum: 0.2.0 verification, 2026-11-08

The 0.2.0 build was deployed to the reference host with the repo's
own deploy/install.sh and verified live after deployment:

- Test suite: 25 passing (fake session; no pairing needed).
- History window: the main chat returned 198 messages at the new
  200 limit (previously capped at 50); has_more still true.
- Voice note: a synthesized WAV posted to the voice endpoint was
  answered in voice modality ("voice note received"), and the
  reply was fetched through the TTS endpoint as a valid MP3
  (12 KB, MPEG layer III, 24 kHz mono). A TTS request for a
  text-modality reply correctly fails: the endpoint only speaks
  voice-modality replies.
- TUI: headless pilot against the live pairing rendered the test
  chat's history, one Tab moved focus from the composer to the
  chat list and back (the 0.1.0 client needed four), and a send
  through the new multiline composer completed (reply verified in
  canonical history).
- Server: 97 chats in the seeded index served by /api/chats; the
  GUI bundle served is the install script's fresh build.
