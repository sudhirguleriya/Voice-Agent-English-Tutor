"""Small WAV helpers (the browser sends 16 kHz mono 16-bit PCM WAV segments)."""
import io
import wave

import numpy as np


def read_wav(data: bytes) -> tuple[int, np.ndarray]:
    """(sample rate, float32 samples in -1..1, mono)."""
    with wave.open(io.BytesIO(data)) as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit PCM")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return rate, x


def duration_ms(data: bytes) -> int:
    try:
        rate, x = read_wav(data)
        return int(len(x) * 1000 / rate)
    except Exception:
        return 0


def pcm16_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()
