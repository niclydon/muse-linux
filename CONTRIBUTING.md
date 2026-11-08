# Contributing

- Run the tests before sending changes: `pip install -e ".[test]" && pytest`.
- Tests must not need a pairing, a network, or a Muse account. Drive
  the fake session in `tests/fakes.py` for anything Link-shaped.
- Never commit pairing material, tokens, chat transcripts, or real
  chat ids. Examples use placeholder ids.
- Keep the public surface small: `link`, `ask`, `history`, `index`,
  `probe`, `shim`. New modules need a reason in the pull request.
- Match the SDK's vocabulary: a *chat* in the UI, `session_id` on the
  wire for chat targeting, `thread_id` only for voice-call targeting.
