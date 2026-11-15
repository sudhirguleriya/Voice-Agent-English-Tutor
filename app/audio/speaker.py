"""Voice match: is this segment the enrolled user, or someone else in the room?

Uses a small local speaker-embedding model (sherpa-onnx + WeSpeaker CAM++, about 28 MB, downloaded on first use,
about 0.1 s per segment on CPU). Nothing leaves the laptop. If the model or a voice profile is missing, every
segment is treated as the user, so the app still works without enrolment.
"""
import logging
import threading
import urllib.request

import numpy as np

from ..config import settings
from ..storage import repo
from .wav import read_wav

log = logging.getLogger("speaker")
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{name}"
MIN_SECONDS = 1.0  # shorter segments are too unreliable to judge; they count as "me"

_extractor = None
_lock = threading.Lock()


def _model_path():
    return settings.data_dir / "models" / settings.speaker_model


def ensure_model() -> bool:
    """Download the model once. Returns False if it is unavailable (feature then stays off)."""
    path = _model_path()
    if path.exists():
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("downloading speaker model %s", settings.speaker_model)
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL.format(name=settings.speaker_model), tmp)
        tmp.replace(path)
        return True
    except Exception as e:
        log.warning("speaker model download failed: %s", e)
        return False


def _get_extractor():
    global _extractor
    with _lock:
        if _extractor is None:
            import sherpa_onnx  # imported lazily: optional at runtime
            if not ensure_model():
                raise RuntimeError("speaker model unavailable")
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(_model_path()), num_threads=2)
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        return _extractor


def embed(wav: bytes) -> np.ndarray:
    """L2-normalised speaker embedding of one WAV segment."""
    rate, x = read_wav(wav)
    ex = _get_extractor()
    with _lock:
        stream = ex.create_stream()
        stream.accept_waveform(rate, x)
        stream.input_finished()
        e = np.array(ex.compute(stream), dtype=np.float32)
    return e / (np.linalg.norm(e) or 1.0)


def enroll(clips: list[bytes]) -> dict:
    """Build the profile from a few clips of the user reading aloud, and calibrate the threshold:
    the user's own clips must match the profile; we sit well below their lowest score."""
    embs = [embed(c) for c in clips if len(read_wav(c)[1]) / read_wav(c)[0] >= MIN_SECONDS]
    if len(embs) < 2:
        raise ValueError("Please record a bit longer: at least two clear sentences are needed.")
    profile = np.mean(embs, axis=0)
    profile /= np.linalg.norm(profile)
    own = [float(profile @ e) for e in embs]
    # other voices score about 0.5 or lower with this model; own voice usually 0.65+
    threshold = settings.speaker_threshold or round(max(0.45, min(0.6, min(own) - 0.15)), 2)
    repo.save_voice_profile(profile, threshold)
    return {"clips": len(embs), "self_scores": [round(s, 2) for s in own], "threshold": threshold}


def classify(wav: bytes) -> tuple[str, float | None]:
    """("me" | "other", similarity). Without a profile (or a usable model) everything is "me"."""
    profile = repo.get_voice_profile()
    if not profile or not profile["enabled"]:
        return "me", None
    rate, x = read_wav(wav)
    if len(x) / rate < MIN_SECONDS:
        return "me", None
    try:
        score = float(profile["embedding"] @ embed(wav))
    except Exception as e:
        log.warning("voice match failed, assuming it is the user: %s", e)
        return "me", None
    return ("me" if score >= profile["threshold"] else "other"), round(score, 3)
