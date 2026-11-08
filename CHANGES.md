# Changes

## 0.3.0 (unreleased)

- Daemon (`muse-linux daemon`, systemd unit `muse-linuxd`): the one
  process that reads the root-only pairing store. One long-lived
  ephemeral session with reconnect and backoff, a shared subscription
  per watched chat, typed events (status with activity text, task,
  approvals, user messages, streaming replies), opened on demand and
  closed when idle. Serves desktop clients over a Unix socket that
  only root and the `muse-linux` group can use; runs without
  capabilities under systemd sandboxing that hides everything but the
  pairing store and its own state. A client that stops reading is
  disconnected instead of slowing the others, requests per client are
  capped, and history that would not fit one protocol line is trimmed
  to the newest messages. Protocol in docs/PROTOCOL.md.
- GNOME client (`muse-linux-gnome`, "Muse for Linux"; GTK 4 +
  libadwaita 1.5+): chat list with filter and unread dots, chat view
  with streaming Markdown replies, working status and task state,
  side chats (new, add by ID, rename, remove from list), history with
  load older, view-only approvals banner, desktop notifications,
  status and settings dialog, keyboard shortcuts, light and dark.
- `muselinux.client`: toolkit-agnostic socket client with automatic
  reconnect, shared by the desktop clients.
- `install.sh`: installs or removes the daemon and the GNOME client
  (venv in /opt/muse-linux, systemd unit, group, desktop entry).
- Chat index: changes lock and re-read the file, so the daemon, the
  web service and the terminal tools sharing it no longer drop each
  other's chats; a damaged index is never overwritten; `remove` and
  last-activity ordering added.
- Live events through the daemon drop Muse-app widget embed tokens,
  like history does since 0.2.1.
- Fixed: the web API's rename route returned a server error.
- `tools/demo_daemon.py` (the real daemon against made-up chats) and
  `tools/smoke_gnome.py` (recorded end-to-end GNOME smoke test). The
  desktop smoke test against the production service passed; see
  docs/SMOKE-TEST.md, with screenshots and a screen recording.

## 0.2.1 (2026-11-08)

- Display text is cleaned of Muse-app widget embed tokens
  (`[[hatch_widget:...]]`), which render natively only inside the
  Muse app; every surface (history, live replies, TUI, GUI, shim)
  now shows the reply text without the raw token.
- Documentation pass for the public flip: README brought current
  with the 0.2.0 surfaces (screenshots of both GUIs in both
  themes, the service install, the voice commands), architecture
  doc extended to the server, TUI, voice and approvals modules,
  GUI README updated, and the 0.2.0 live verification recorded in
  docs/SMOKE-TEST.md.

## 0.2.0 (2026-11-08)

- Full-screen terminal UI (`muse-linux tui`, Textual): Muse app
  layout with a chat sidebar, streaming Markdown replies, working
  status in the header, approvals surface, light/dark themes, and
  keyboard actions (ctrl+n new chat, ctrl+t theme, ctrl+r reload,
  ctrl+q quit). Verified live against a paired host, including a
  real send through the UI.
- Native GUI backend API (`muse-linux serve`): status, chats,
  history and an SSE message stream carrying working status, task
  state and approval snapshots; can also serve the built GUI.
- GUI in gui/: assistant-ui front end (sidebar of real chats, history
  seeding, streaming, working-status pill, approvals card, light and
  dark themes) plus a Tauri shell packaging it as a desktop app.
- TUI navigation rework: Tab toggles directly between the chat list
  and the composer (ctrl+1 and ctrl+2 also focus them), a sidebar
  filter (ctrl+k), and a multiline composer (enter for a newline,
  ctrl+enter or ctrl+s to send).
- Sidebar filter in the web GUI as well; both sidebars stay usable
  with large local indexes.
- History window raised from 50 to 200 messages (the measured
  server ceiling) with load-older controls in the TUI and the web
  GUI; reactions on messages are now parsed and displayed.
- Voice: voice notes out (`muse-linux voice-note`, plus a mic
  button in the web GUI), spoken replies (`muse-linux say`, a
  TTS endpoint, and listen buttons in the web GUI), and a
  `voice-call` command that hands off to the SDK's realtime call
  program.
- Approvals are rendered as a structured pending list in both
  GUIs. Decision routes are scope-refused for this credential
  (measured 2026-11-08), so approvals remain decided in the Muse
  app; the client does not fake action buttons.
- One shared chat index by default: terminal clients prefer the
  installed service's index home when one exists.
- Reproducible deployment: deploy/install.sh plus a systemd unit
  install or update the service, rebuilding the web GUI from the
  repo; CI workflow (pytest, GUI build) and Dependabot config;
  textual pinned below the next major.

## 0.1.0 (2026-11-08)

- Initial proof of concept.
- Ephemeral Link session layer over the Muse Gadget SDK pairing store.
- Terminal client: doctor, local chat index, history, one-shot ask,
  interactive chat, parity-ceiling probes.
- OpenAI-compatible shim with streaming, model-per-chat addressing.
- Probe results document: conversational core allowed, enumeration
  and VM file routes scope-refused.
- Test suite driven by a fake Link session; no pairing or network
  needed.
- Live smoke test passed end to end (docs/SMOKE-TEST.md); three
  defects found there fixed in this release: SDK-derived node id,
  time-bounded turn/session cleanup, streaming response close.
