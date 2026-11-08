# Changes

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
