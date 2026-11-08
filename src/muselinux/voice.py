"""Voice messages and spoken replies over the Link session.

``send_voice`` delivers a WAV recording (at most 2 MiB) as a voice
message in a chat; the turn machinery that follows it is the same
subscription flow as a text turn. ``stream_tts`` yields MP3 chunks
for one assistant message, which fetch_tts collects into bytes a
client can play or save.

Realtime voice calls are a separate SDK entry point (the
``musegadget voice-call`` program, PCM16LE mono 24 kHz, one call
per VM); see the ``voice-call`` CLI command, which launches it with
the leased VM id.
"""

from __future__ import annotations

MAX_WAV_BYTES = 2 * 1024 * 1024


async def send_voice_message(session, session_id: str | None,
                             wav_bytes: bytes) -> dict:
    """Send a WAV voice message; returns the SDK send result.

    The result matters: turn followers acknowledge it to bind the
    turn's replies, exactly as with send_chat.
    """
    if len(wav_bytes) > MAX_WAV_BYTES:
        raise ValueError(
            f"voice message is {len(wav_bytes)} bytes; "
            f"the limit is {MAX_WAV_BYTES}")
    # Voice modality asks Muse to reply in voice, which is also
    # what makes the reply speakable through stream_tts.
    return await session.send_voice(wav_bytes, session_id=session_id,
                                    output_modality="voice")


async def fetch_tts(session, message_id: str) -> bytes:
    chunks: list[bytes] = []
    async for chunk in session.stream_tts(message_id):
        if isinstance(chunk, (bytes, bytearray)):
            chunks.append(bytes(chunk))
    return b"".join(chunks)
