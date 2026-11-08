# Muse for Linux: GUI

A desktop-style chat client built on
[assistant-ui](https://www.assistant-ui.com), talking to the
`muselinux` backend's native API (not the OpenAI shim), so the
Muse-specific surface survives: real chats and side chats by id,
canonical history, live working status, task state, and approval
snapshots.

## Surfaces

- **Web app (zero build on the client):** `muse-linux serve
  --gui-dir <dist>` serves this UI and the API from one process on
  the paired host.
- **Desktop app:** the Tauri shell in `src-tauri/` wraps the same
  build, starts the backend if installed, and packages as .deb and
  AppImage.

## Layout

- Sidebar: the local chat index (the server offers this credential
  no chat enumeration; see ../docs/PROBE-RESULTS.md), with a filter
  box for large indexes. Create chats, add known chats by id,
  rename locally.
- Header: connection state from `/api/status`, plus a live
  working-status pill while Muse is on a turn.
- Chat pane: assistant-ui thread with history seeded from the
  canonical transcript and replies streamed from the backend's SSE
  message endpoint. A load-older button extends the window toward
  the 200-message ceiling. Assistant messages carry a listen
  button that plays the spoken reply (TTS) when the reply is
  voice-modality.
- Composer: text, plus a mic button that records a voice note
  (converted to 16 kHz WAV in the browser) and streams the reply
  like a text turn.
- Approvals arriving during a turn surface as a dismissible card
  above the thread, listing each pending approval. The card is
  read-only: this credential cannot decide approvals (measured;
  see ../docs/PROBE-RESULTS.md), so it says where they are decided
  instead of offering buttons that would not work.
- Light and dark themes, persisted per browser.

## Develop

```sh
npm install
npm run dev        # vite; /api proxied to a local muse-linux serve
npm run build      # dist/ for --gui-dir or Tauri
npm run tauri build
```
