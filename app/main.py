"""Voice Coach server: receives speech clips from the always-on browser mic and coaches the speaker."""
import asyncio
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from .coach import Coach  # noqa: E402
from .db import DB, normalize_pattern  # noqa: E402
from .report import build_report  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice-coach")

db = DB(str(ROOT / os.getenv("DB_PATH", "data/coach.db")))
_coach = None

# Phrases speech-to-text models commonly invent from silence/noise.
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "bye", "you",
                  "subtitles by the amara.org community", "please subscribe"}

app = FastAPI(title="Voice Coach")


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
async def analyze(audio: UploadFile = File(...), day: str = Form(None)):
    day = _valid_day(day)
    wav = await audio.read()
    if len(wav) < 8000:  # < ~0.25 s at 16 kHz
        return {"skipped": "too short"}
    c = coach()

    try:
        text = await asyncio.to_thread(c.transcribe, wav)
    except Exception as e:
        log.exception("transcription failed")
        raise HTTPException(502, f"Transcription failed: {e}")

    norm = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if not norm or norm in HALLUCINATIONS or len(norm.split()) < 2:
        return {"skipped": "no speech", "text": text}

    # grammar analysis and audio pronunciation check run in parallel
    known = db.known_patterns()
    analysis_t = asyncio.to_thread(c.analyze, text, known)
    pron_t = asyncio.to_thread(c.pronunciation, wav, text)
    analysis, pron = await asyncio.gather(analysis_t, pron_t, return_exceptions=True)
    if isinstance(analysis, Exception):
        log.error("analysis failed: %s", analysis)
        raise HTTPException(502, f"Analysis failed: {analysis}")
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

    uid = db.save_analysis(ts=datetime.now().isoformat(timespec="seconds"), day=day, text=text,
                           analysis=analysis, pronunciation=pronunciation)

    # tell the user which mistakes are repeats
    repeat_counts = {r["pattern"]: r["n"] for r in db.known_patterns(limit=500)}
    for m in analysis["mistakes"]:
        m["times_seen"] = repeat_counts.get(normalize_pattern(m["pattern"]), 1)

    return {"id": uid, "day": day, "text": text, **analysis, "pronunciation": pronunciation}


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
