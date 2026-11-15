"""Text to speech: gpt-audio-mini, then a dedicated TTS model (Fish S2 Pro), then the browser voice (client side).

gpt-audio-mini is a chat model: asked to *read* a sentence, it sometimes *answers* it instead. Its own transcript
is checked against the text and a mismatching clip is never used. Clips are cached on disk by text + voice chain.
"""
import base64
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from difflib import SequenceMatcher

from ..config import settings
from ..llm import fast_client
from ..audio.wav import pcm16_to_wav

log = logging.getLogger("tts")
BYTES_PER_SECOND = 48000  # 24 kHz * 16-bit mono


def is_chat_audio_model(model: str) -> bool:
    return bool(re.search(r"gpt-audio|audio-preview", model.lower()))


def similar(a: str, b: str) -> float:
    """Letter-level similarity, so 'veget able' still equals 'vegetable'."""
    letters = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    return SequenceMatcher(None, letters(a), letters(b), autojunk=False).ratio()


def stream_chat_audio(model: str, voice: str, messages: list[dict], max_seconds: float) -> tuple[bytes, str]:
    """Stream an audio reply from a chat model. Returns (WAV bytes, transcript of what it said)."""
    stream = fast_client().chat.completions.create(
        model=model, modalities=["text", "audio"], audio={"voice": voice or "alloy", "format": "pcm16"},
        stream=True, messages=messages)  # NB: no temperature=0, audio models can loop at very low temperature
    pcm, said = bytearray(), []
    limit = int(max_seconds * BYTES_PER_SECOND)
    for ch in stream:
        if len(pcm) > limit:
            stream.close()
            raise ValueError("audio ran far longer than expected")
        a = getattr(ch.choices[0].delta, "audio", None) if ch.choices else None
        if not a:
            continue
        get = (lambda k: a.get(k)) if isinstance(a, dict) else (lambda k: getattr(a, k, None))
        if get("data"):
            pcm += base64.b64decode(get("data"))
        if get("transcript"):
            said.append(get("transcript"))
    if len(pcm) < BYTES_PER_SECOND // 10:
        raise ValueError("no audio returned")
    # transcript arrives in pieces that carry their own spacing; tidy "Hi there !" -> "Hi there!"
    text = re.sub(r"\s+([,.!?;:’'])", r"\1", " ".join("".join(said).split()))
    return pcm16_to_wav(bytes(pcm)), text


def _speak_endpoint(model: str, voice: str, text: str) -> bytes:
    """Dedicated TTS via /audio/speech. Sent directly because some models (Fish) work best with no voice
    at all, while the SDK always requires one."""
    body = {"model": model, "input": text, "response_format": "mp3"}
    if voice:
        body["voice"] = voice
    base = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(f"{base}/audio/speech", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {settings.api_key}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read().decode(errors='replace')[:200]}") from None
    if len(data) < 500:
        raise ValueError("empty audio")
    return data


READ_PROMPTS = [
    lambda t: [{"role": "system", "content": "You are a text-to-speech voice. You never converse. You only read "
                                             "the script you are given aloud, exactly, word for word, then stop."},
               {"role": "user", "content": f'Script:\n"""\n{t}\n"""'}],
    lambda t: [{"role": "user", "content": f'Say exactly this and nothing else, then stop: "{t}"'}],
]


def synthesize(text: str) -> tuple[bytes, str, str]:
    """(audio bytes, mime type, model used). Raises if every AI voice fails (the browser then speaks)."""
    errors = []
    for model, voice in ((settings.tts_model, settings.tts_voice), (settings.tts_fallback, settings.tts_fallback_voice)):
        if not model:
            continue
        try:
            if not is_chat_audio_model(model):
                return _speak_endpoint(model, voice, text), "audio/mpeg", model
            for make in READ_PROMPTS:  # second prompt style if it answered instead of reading
                wav, said = stream_chat_audio(model, voice, make(text), 3 + 0.7 * len(text.split()))
                if not said or similar(said, text) >= 0.85:
                    return wav, "audio/wav", model
                log.info("%s did not read verbatim (said %r)", model, said[:60])
            raise ValueError("did not read the text verbatim")
        except Exception as e:
            log.warning("text-to-speech with %s failed: %s", model, e)
            errors.append(f"{model}: {e}")
    raise RuntimeError("; ".join(errors) or "No text-to-speech model configured")


def cached_speech(text: str) -> tuple[bytes, str, str]:
    """Same as synthesize, with a disk cache: replays are instant and free."""
    chain = f"{settings.tts_model}|{settings.tts_voice}|{settings.tts_fallback}|{settings.tts_fallback_voice}"
    key = hashlib.sha1(f"{chain}|{text}".encode()).hexdigest()
    cache = settings.data_dir / "tts_cache"
    for ext, mime in (("wav", "audio/wav"), ("mp3", "audio/mpeg")):
        p = cache / f"{key}.{ext}"
        if p.exists():
            return p.read_bytes(), mime, "cache"
    data, mime, model = synthesize(text)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{key}.{'wav' if mime == 'audio/wav' else 'mp3'}").write_bytes(data)
    return data, mime, model
