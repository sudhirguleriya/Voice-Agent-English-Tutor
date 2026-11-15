"""REST endpoints: sessions and summaries, focus areas and progress, voice profile, text to speech, health."""
import asyncio

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ..analysis.live import analyse_sentence
from ..audio import speaker
from ..config import settings
from ..patterns import engine as patterns
from ..patterns.progress import progress
from ..practice_agent.agent import TOPICS
from ..practice_agent.tts import cached_speech
from ..storage import repo
from ..summary.summarize import build_summary

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    prof = repo.get_voice_profile()
    return {"ok": True, "key_configured": bool(settings.api_key),
            "provider": "openrouter" if settings.openrouter else "openai",
            "models": {"speech_to_text": [m for m in (settings.stt_model, settings.stt_fallback) if m],
                       "analysis": settings.analysis_model, "hints": settings.hint_model,
                       "agent": settings.agent_model,
                       "text_to_speech": [m for m in (settings.tts_model, settings.tts_fallback) if m] + ["browser"]},
            "voice_profile": {"enrolled": bool(prof), "enabled": bool(prof and prof["enabled"]),
                              "threshold": prof["threshold"] if prof else None},
            "ai_voice": bool(settings.tts_model or settings.tts_fallback)}


@router.get("/topics")
def topics():
    return {"topics": TOPICS}


# ---------------------------------------------------------------- sessions
@router.get("/sessions")
def sessions():
    return repo.list_sessions()


@router.get("/sessions/{session_id}")
async def session(session_id: int):
    s = repo.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s["ended_at"] is None or s["corrected_paragraph"] is None:
        # tab was closed without Stop, or the summary failed earlier: build it now
        if any(u["speaker"] == "me" for u in s["utterances"]):
            repo.end_session(session_id)
            return await asyncio.to_thread(build_summary, session_id)
    return {**s, "transcript": " ".join(u["text"] for u in s["utterances"] if u["speaker"] == "me"),
            "all_mistakes": [m for u in s["utterances"] if u["speaker"] == "me" for m in u["mistakes"]]}


@router.post("/sessions/{session_id}/summary")
async def rebuild_summary(session_id: int):
    if not repo.get_session(session_id, full=False):
        raise HTTPException(404, "Session not found")
    return await asyncio.to_thread(build_summary, session_id)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int):
    repo.delete_session(session_id)
    return {"ok": True}


class SpeakerFix(BaseModel):
    speaker: str  # "me" | "other"


@router.patch("/utterances/{utterance_id}")
async def fix_speaker(utterance_id: int, body: SpeakerFix):
    """"Not me" / "This was me". Switching a line to "me" analyses it now."""
    if body.speaker not in ("me", "other"):
        raise HTTPException(400, "speaker must be 'me' or 'other'")
    u = repo.set_speaker(utterance_id, body.speaker)
    if not u:
        raise HTTPException(404, "Line not found")
    if body.speaker == "me" and not u["analysed"]:
        r = await asyncio.to_thread(analyse_sentence, u["text"], [], repo.known_rules(), patterns.focus_titles())
        u = repo.save_analysis(utterance_id, r["corrected"] or u["text"], r["mistakes"], r["suggestions"])
    return u


# ---------------------------------------------------------------- focus areas + progress
@router.get("/focus")
def focus():
    total, max_id = repo.mistake_count()
    run = repo.latest_pattern_run()
    return {"run": run, "total_mistakes": total,
            "new_since_run": max_id - (run["last_mistake_id"] if run else 0),
            "totals": repo.focus_totals(),
            "progress": progress(repo.daily_stats(30))}


@router.post("/focus/analyse")
async def analyse_focus():
    try:
        run = await asyncio.to_thread(patterns.analyse_history)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"run": run}


# ---------------------------------------------------------------- voice profile
@router.post("/voice/enroll")
async def voice_enroll(clips: list[UploadFile] = File(...)):
    data = [await c.read() for c in clips]
    try:
        return await asyncio.to_thread(speaker.enroll, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, f"Voice match is unavailable: {e}")


class Enabled(BaseModel):
    enabled: bool


@router.post("/voice/enabled")
def voice_enabled(body: Enabled):
    repo.set_voice_enabled(body.enabled)
    return {"ok": True}


@router.delete("/voice")
def voice_delete():
    repo.delete_voice_profile()
    return {"ok": True}


# ---------------------------------------------------------------- text to speech
class SpeakReq(BaseModel):
    text: str


@router.post("/tts")
async def tts(body: SpeakReq):
    text = " ".join(body.text.split())[:600]
    if not text:
        raise HTTPException(400, "Nothing to say.")
    try:
        data, mime, model = await asyncio.to_thread(cached_speech, text)
    except Exception as e:
        raise HTTPException(502, f"AI voice unavailable ({e}). The browser voice will be used.")
    return Response(data, media_type=mime, headers={"X-TTS-Model": model})
