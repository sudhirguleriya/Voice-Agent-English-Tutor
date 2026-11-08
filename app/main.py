"""Voice Coach server: receives speech clips from the always-on browser mic and coaches the speaker."""
import asyncio
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
import openai
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from .coach import Coach  # noqa: E402
from .db import DB, normalize_pattern  # noqa: E402
from .practice import INTERVALS, compare  # noqa: E402
from .report import build_report  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice-coach")

db = DB(str(ROOT / os.getenv("DB_PATH", "data/coach.db")))
_coach = None

# Phrases speech-to-text models commonly invent from silence/noise.
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "bye", "you",
                  "subtitles by the amara.org community", "please subscribe"}

app = FastAPI(title="Voice Coach")


def friendly(e: Exception) -> str:
    """Turn provider errors into a message the user can act on."""
    status = getattr(e, "status_code", None)
    if status == 401 or isinstance(e, openai.AuthenticationError):
        return ("The AI provider rejected your API key (expired, revoked or wrong). "
                "Put a valid key in .env and restart the server.")
    if status == 402:
        return "Your AI provider account is out of credit, or the key reached its spending limit."
    if status == 403:
        return "The API key is not allowed to use this model. Check the model names in .env."
    if status == 429:
        return "Rate limited by the AI provider. Wait a few seconds and try again."
    if isinstance(e, openai.APIConnectionError):
        return "Could not reach the AI provider. Check your internet connection."
    return f"AI request failed: {e}"


@app.exception_handler(openai.OpenAIError)
async def _openai_error(request: Request, e: openai.OpenAIError):
    log.error("provider error on %s: %s", request.url.path, e)
    return JSONResponse({"detail": friendly(e)}, status_code=502)


@app.exception_handler(Exception)
async def _any_error(request: Request, e: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": f"Server error: {e}"}, status_code=500)


def coach() -> Coach:
    global _coach
    if _coach is None:
        if not (os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY")):
            raise HTTPException(500, "No API key set. Copy .env.example to .env and add OPENAI_API_KEY or OPENROUTER_API_KEY.")
        _coach = Coach()
    return _coach


def _valid_day(day: str | None) -> str:
    if day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return day
    return date.today().isoformat()


@app.get("/api/health")
def health():
    return {"ok": True, "key_configured": bool(os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY"))}


@app.post("/api/analyze")
async def analyze(audio: UploadFile = File(...), day: str = Form(None), duration_ms: int = Form(None),
                  conversation_id: int = Form(None), new_conversation: int = Form(0)):
    day = _valid_day(day)
    wav = await audio.read()
    if len(wav) < 8000:  # < ~0.25 s at 16 kHz
        return {"skipped": "too short"}
    c = coach()

    try:
        text = await asyncio.to_thread(c.transcribe, wav)
    except Exception as e:
        log.exception("transcription failed")
        raise HTTPException(502, "Transcription failed. " + friendly(e))

    norm = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if not norm or norm in HALLUCINATIONS or len(norm.split()) < 2:
        return {"skipped": "no speech", "text": text}

    # grammar analysis and audio pronunciation check run in parallel
    ts = datetime.now().isoformat(timespec="seconds")
    conv = db.resolve_conversation(day=day, ts=ts, conversation_id=conversation_id, new=bool(new_conversation))
    known = db.known_patterns()
    analysis_t = asyncio.to_thread(c.analyze, text, known, db.conversation_texts(conv, limit=4))
    pron_t = asyncio.to_thread(c.pronunciation, wav, text)
    analysis, pron = await asyncio.gather(analysis_t, pron_t, return_exceptions=True)
    if isinstance(analysis, Exception):
        log.error("analysis failed: %s", analysis)
        raise HTTPException(502, "Analysis failed. " + friendly(analysis))
    if isinstance(pron, Exception):
        log.warning("pronunciation pass failed (continuing without it): %s", pron)
        pron = []
    if not analysis.get("is_speech", True):
        return {"skipped": "not directed speech", "text": text}

    # merge transcript-inferred and audio-detected pronunciation issues (dedupe by word)
    for m in analysis.get("mistakes", []):
        if not normalize_pattern(m.get("pattern") or ""):
            m["pattern"] = _fallback_pattern(m)

    pronunciation = _merge_pronunciation(list(pron), analysis.get("pronunciation_suspects", []))

    dur = duration_ms if duration_ms and 300 < duration_ms < 60000 else None
    analysis["next_suggestions"] = [s.strip() for s in (analysis.get("next_suggestions") or []) if s.strip()][:3]
    uid = db.save_analysis(ts=ts, day=day, text=text, analysis=analysis, pronunciation=pronunciation,
                           duration_ms=dur, conversation_id=conv)

    # tell the user which mistakes are repeats
    repeat_counts = {r["pattern"]: r["n"] for r in db.known_patterns(limit=500)}
    for m in analysis["mistakes"]:
        m["times_seen"] = repeat_counts.get(normalize_pattern(m["pattern"]), 1)

    return {"id": uid, "day": day, "ts": ts, "text": text, "word_count": len(text.split()), "duration_ms": dur,
            "conversation_id": conv, **analysis, "pronunciation": pronunciation}


class LiveReq(BaseModel):
    text: str
    context: list[str] = []


@app.post("/api/live")
async def live(req: LiveReq):
    """Suggestions while the user is still speaking: fixes so far + ways to finish the sentence."""
    text = " ".join(req.text.split())[:400]
    if len(text.split()) < 2:
        return {"fixes": [], "continuations": []}
    return await asyncio.to_thread(coach().live_suggest, text, [c[:300] for c in req.context[-3:]])


@app.get("/api/conversations")
def conversations(day: str | None = None):
    return db.conversations_for_day(_valid_day(day))


@app.post("/api/conversation/{cid}/polish")
async def polish(cid: int):
    texts = db.conversation_texts(cid)
    if not texts:
        raise HTTPException(404, "That conversation has no sentences.")
    out = await asyncio.to_thread(coach().polish_conversation, texts)
    db.set_polished(cid, datetime.now().isoformat(timespec="seconds"), out)
    return out


@app.post("/api/attempt")
async def attempt(audio: UploadFile = File(...), target: str = Form(...), must: str = Form(""),
                  pattern: str = Form(""), grade: int = Form(0), day: str = Form(None)):
    """User repeats a target sentence (shadowing / practice card). Transcribe and compare; no AI analysis."""
    day = _valid_day(day)
    wav = await audio.read()
    if len(wav) < 8000:
        return {"skipped": "too short"}
    try:
        said = await asyncio.to_thread(coach().transcribe, wav)
    except Exception as e:
        log.exception("attempt transcription failed")
        raise HTTPException(502, "Transcription failed. " + friendly(e))
    norm = re.sub(r"[^a-z ]", "", said.lower()).strip()
    if not norm or norm in HALLUCINATIONS:
        return {"skipped": "no speech", "said": said}
    res = compare(target, said, must)
    sched = db.record_attempt(ts=datetime.now().isoformat(timespec="seconds"), day=day, pattern=pattern,
                              target=target, said=said, similarity=res["similarity"], passed=res["passed"],
                              grade=bool(grade))
    if sched:
        sched["interval_days"] = INTERVALS[sched["box"]] if res["passed"] else 0
    return {"said": said, "target": target, **res, "schedule": sched}


@app.get("/api/practice")
def practice(day: str | None = None):
    day = _valid_day(day)
    return {"day": day, "cards": db.practice_cards(day), "stats": db.mastery_stats()}


def _fallback_pattern(m: dict) -> str:
    """Used when the model returns an empty rule name, so unrelated mistakes never merge under ''."""
    orig = " ".join(re.findall(r"[a-z']+", (m.get("original") or "").lower())[:4])
    if m.get("category") == "filler":
        return f"filler word {orig}".strip()
    corr = " ".join(re.findall(r"[a-z']+", (m.get("correction") or "").lower())[:4])
    return f"{m.get('category', 'mistake').replace('_', ' ')}: {orig} -> {corr}"


def _ipa(s: str) -> str:
    return re.sub(r"[^a-zɐ-ʯəɛɪʊʌɔæθðŋʃʒ]", "", (s or "").lower().replace("ː", ""))


def _merge_pronunciation(audio_issues, suspects):
    """Dedupe by word, and drop 'issues' where what was heard is the same as the correct form."""
    out, seen = [], set()
    for p in audio_issues + suspects:
        w = p["word"].strip().lower()
        heard = _ipa(p.get("heard_as"))
        if not w or w in seen:
            continue
        heard_plain = re.sub(r"[^a-z'-]", "", (p.get("heard_as") or "").lower())
        if heard_plain == w or (heard and "/" in (p.get("heard_as") or "") and heard in _ipa(p.get("tip"))):
            continue  # "heard" matches the correct pronunciation -> not a real problem
        seen.add(w)
        seen.add(re.sub(r"[^a-z]", "", (p.get("heard_as") or "").lower()))  # e.g. "bided" vs "bought"
        out.append(p)
    return out


@app.get("/api/day/{day}")
def get_day(day: str):
    return db.utterances_for_day(_valid_day(day))


@app.delete("/api/utterance/{uid}")
def delete_utterance(uid: int):
    db.delete_utterance(uid)
    return {"ok": True}


@app.get("/api/report")
def report(day: str | None = None):
    day = _valid_day(day)
    r = build_report(db, day)
    r["plan"] = db.get_summary(day)
    return r


@app.post("/api/report/plan")
async def make_plan(day: str | None = None):
    day = _valid_day(day)
    r = build_report(db, day)
    if not r["totals"]["today"]["utterances"]:
        raise HTTPException(400, "No speech recorded for this day yet.")
    plan = await asyncio.to_thread(coach().daily_plan, r)
    db.put_summary(day, datetime.now().isoformat(timespec="seconds"), plan)
    return plan


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")
