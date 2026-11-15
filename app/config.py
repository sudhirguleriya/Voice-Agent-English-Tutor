"""Settings, read once from .env. Every model is swappable through an environment variable.

Two providers are supported: OpenRouter (OPENROUTER_API_KEY) or OpenAI directly (OPENAI_API_KEY).
Defaults are the models this app was tuned and benchmarked with.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    """Read a setting. A variable that is present but empty means "turned off"."""
    value = os.getenv(key)
    return default if value is None else value.strip()


@dataclass(frozen=True)
class Settings:
    openrouter: bool
    api_key: str
    base_url: str | None
    # speech to text: dedicated transcriber first (chat models obey spoken instructions)
    stt_model: str
    stt_fallback: str
    # per-sentence analysis (mistakes + suggestions), summary and pattern analysis
    analysis_model: str
    analysis_reasoning: str  # "minimal" | "low" | "medium" | "" (model default)
    summary_model: str
    pattern_model: str
    # fast hints while the user is still mid-sentence (browser partial transcript)
    hint_model: str
    # practice agent: a voice-native chat model, with a text model + TTS as fallback
    agent_model: str
    agent_voice: str
    agent_text_fallback: str
    # text to speech (drills, fallback agent voice); the browser voice is the last resort
    tts_model: str
    tts_voice: str
    tts_fallback: str
    tts_fallback_voice: str
    language: str
    data_dir: Path
    db_path: Path
    speaker_model: str
    speaker_threshold: float  # 0 = use the threshold calibrated during voice enrolment


def load_settings() -> Settings:
    or_key, oa_key = os.getenv("OPENROUTER_API_KEY", ""), os.getenv("OPENAI_API_KEY", "")
    openrouter = bool(or_key) and not oa_key
    # OpenRouter model ids carry a vendor prefix ("openai/..."); OpenAI's own API does not
    p = (lambda vendor, m: f"{vendor}/{m}") if openrouter else (lambda vendor, m: m)
    data_dir = ROOT / _env("DATA_DIR", "data")
    return Settings(
        openrouter=openrouter,
        api_key=or_key if openrouter else oa_key,
        base_url=_env("OPENAI_BASE_URL", "https://openrouter.ai/api/v1" if openrouter else "") or None,
        stt_model=_env("STT_MODEL", p("openai", "gpt-4o-mini-transcribe")),
        stt_fallback=_env("STT_FALLBACK", p("openai", "gpt-audio-mini")),
        analysis_model=_env("ANALYSIS_MODEL", p("openai", "gpt-5-mini")),
        analysis_reasoning=_env("ANALYSIS_REASONING", "minimal"),
        summary_model=_env("SUMMARY_MODEL", p("openai", "gpt-5-mini")),
        pattern_model=_env("PATTERN_MODEL", p("openai", "gpt-5-mini")),
        hint_model=_env("HINT_MODEL", "google/gemini-2.5-flash-lite" if openrouter else "gpt-4.1-nano"),
        agent_model=_env("AGENT_MODEL", p("openai", "gpt-audio-mini")),
        agent_voice=_env("AGENT_VOICE", "alloy"),
        agent_text_fallback=_env("AGENT_TEXT_FALLBACK", "google/gemini-2.5-flash-lite" if openrouter else "gpt-4.1-mini"),
        tts_model=_env("TTS_MODEL", p("openai", "gpt-audio-mini")),
        tts_voice=_env("TTS_VOICE", "alloy"),
        tts_fallback=_env("TTS_FALLBACK", "fish-audio/s2-pro" if openrouter else "gpt-4o-mini-tts"),
        tts_fallback_voice=_env("TTS_FALLBACK_VOICE", "" if openrouter else "alloy"),
        language=_env("TARGET_LANGUAGE", "English"),
        data_dir=data_dir,
        db_path=data_dir / _env("DB_FILE", "voice_coach.db"),
        speaker_model=_env("SPEAKER_MODEL", "wespeaker_en_voxceleb_CAM++.onnx"),
        speaker_threshold=float(_env("SPEAKER_THRESHOLD", "0") or 0),
    )


settings = load_settings()
