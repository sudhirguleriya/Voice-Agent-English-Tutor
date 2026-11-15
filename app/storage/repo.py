"""Data access used by the rest of the app. Returns plain dicts (JSON-ready), never ORM objects."""
from collections import Counter
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import delete, func, select

from .db import db
from .models import (CATEGORIES, CoachSession, Drill, FocusArea, Mistake, PatternRun, Utterance,
                     VoiceProfile)


# ---------------------------------------------------------------- serialisers
def mistake_dict(m: Mistake) -> dict:
    return {"id": m.id, "utterance_id": m.utterance_id, "original": m.original, "corrected": m.corrected,
            "category": m.category, "rule": m.rule, "explanation": m.explanation}


def utterance_dict(u: Utterance) -> dict:
    return {"id": u.id, "speaker": u.speaker, "text": u.text, "corrected": u.corrected,
            "suggestions": u.suggestions, "analysed": u.analysed, "duration_ms": u.duration_ms,
            "word_count": u.word_count, "speaker_score": u.speaker_score,
            "created_at": u.created_at.isoformat(timespec="seconds") if u.created_at else None,
            "mistakes": [mistake_dict(m) for m in u.mistakes]}


def session_dict(s: CoachSession, full: bool = False) -> dict:
    mine = [u for u in s.utterances if u.speaker == "me"]
    cats = Counter(m.category for u in mine for m in u.mistakes)
    last = max((u.created_at for u in s.utterances if u.created_at), default=None)
    end = s.ended_at or last
    out = {"id": s.id, "mode": s.mode, "topic": s.topic, "title": s.title,
           "started_at": s.started_at.isoformat(timespec="seconds") if s.started_at else None,
           "ended_at": s.ended_at.isoformat(timespec="seconds") if s.ended_at else None,
           "duration_s": int((end - s.started_at).total_seconds()) if end and s.started_at else 0,
           "sentences": len(mine), "words": sum(u.word_count for u in mine),
           "mistakes": sum(len(u.mistakes) for u in mine),
           "most_common": cats.most_common(1)[0][0] if cats else None,
           "corrected_paragraph": s.corrected_paragraph, "key_points": s.key_points or []}
    if full:
        out["utterances"] = [utterance_dict(u) for u in s.utterances]
    return out


# ---------------------------------------------------------------- sessions
def create_session(mode: str, topic: str | None = None) -> int:
    with db() as s:
        row = CoachSession(mode=mode, topic=topic)
        s.add(row)
        s.flush()
        return row.id


def end_session(session_id: int):
    with db() as s:
        row = s.get(CoachSession, session_id)
        if row and not row.ended_at:
            row.ended_at = datetime.now()


def get_session(session_id: int, full: bool = True) -> dict | None:
    with db() as s:
        row = s.get(CoachSession, session_id)
        return session_dict(row, full) if row else None


def list_sessions(limit: int = 50) -> list[dict]:
    with db() as s:
        rows = s.scalars(select(CoachSession).order_by(CoachSession.id.desc()).limit(limit)).all()
        return [session_dict(r) for r in rows if r.utterances]


def save_summary(session_id: int, corrected_paragraph: str, key_points: list[str], title: str | None = None):
    with db() as s:
        row = s.get(CoachSession, session_id)
        row.corrected_paragraph, row.key_points = corrected_paragraph, key_points
        if title:
            row.title = title[:120]


def delete_session(session_id: int):
    with db() as s:
        s.execute(delete(CoachSession).where(CoachSession.id == session_id))


# ---------------------------------------------------------------- utterances + mistakes
def add_utterance(session_id: int, speaker: str, text: str, duration_ms: int | None = None,
                  speaker_score: float | None = None) -> dict:
    with db() as s:
        u = Utterance(session_id=session_id, speaker=speaker, text=text, duration_ms=duration_ms,
                      word_count=len(text.split()), speaker_score=speaker_score)
        s.add(u)
        s.flush()
        s.refresh(u)
        return utterance_dict(u)


def save_analysis(utterance_id: int, corrected: str, mistakes: list[dict], suggestions: dict) -> dict:
    with db() as s:
        u = s.get(Utterance, utterance_id)
        u.corrected, u.suggestions, u.analysed = corrected, suggestions, True
        u.mistakes = [Mistake(session_id=u.session_id, original=m["original"], corrected=m["corrected"],
                              category=m["category"] if m["category"] in CATEGORIES else "other",
                              rule=m["rule"], explanation=m.get("explanation", "")) for m in mistakes]
        s.flush()
        return utterance_dict(u)


def set_speaker(utterance_id: int, speaker: str) -> dict | None:
    """User correction: "this was not me" / "this was me". Mistakes of a non-me line are removed."""
    with db() as s:
        u = s.get(Utterance, utterance_id)
        if not u:
            return None
        u.speaker = speaker
        if speaker != "me":
            u.mistakes = []
            u.analysed = False
        s.flush()
        return utterance_dict(u)


def recent_texts(session_id: int, limit: int = 4) -> list[str]:
    """The last few lines of the conversation, for context."""
    with db() as s:
        rows = s.scalars(select(Utterance).where(Utterance.session_id == session_id)
                         .order_by(Utterance.id.desc()).limit(limit)).all()
        return [f"{'Me' if u.speaker == 'me' else 'Agent' if u.speaker == 'agent' else 'Other'}: {u.text}"
                for u in reversed(rows)]


def known_rules(limit: int = 40) -> list[str]:
    """Most frequent rule names, so the analyser reuses the same name for the same mistake."""
    with db() as s:
        rows = s.execute(select(Mistake.rule, func.count().label("n")).group_by(Mistake.rule)
                         .order_by(func.count().desc()).limit(limit)).all()
        return [r.rule for r in rows]


def rule_counts(rules: list[str]) -> dict[str, int]:
    if not rules:
        return {}
    with db() as s:
        rows = s.execute(select(Mistake.rule, func.count()).where(Mistake.rule.in_(rules))
                         .group_by(Mistake.rule)).all()
        return {r: n for r, n in rows}


def all_mistakes(after_id: int = 0, limit: int = 2000) -> list[dict]:
    """Mistake history for the pattern engine, newest last, with the day it happened."""
    with db() as s:
        rows = s.execute(select(Mistake, Utterance.text).join(Utterance)
                         .where(Mistake.id > after_id).order_by(Mistake.id).limit(limit)).all()
        return [{**mistake_dict(m), "sentence": text, "session_id": m.session_id,
                 "day": m.created_at.date().isoformat()} for m, text in rows]


def mistake_count() -> tuple[int, int]:
    """(number of mistakes, highest mistake id)."""
    with db() as s:
        n, mx = s.execute(select(func.count(Mistake.id), func.max(Mistake.id))).one()
        return n or 0, mx or 0


# ---------------------------------------------------------------- focus areas
def save_pattern_run(areas: list[dict], overview: str, analysed: int, last_mistake_id: int) -> dict:
    with db() as s:
        run = PatternRun(mistakes_analysed=analysed, last_mistake_id=last_mistake_id, overview=overview)
        for i, a in enumerate(areas, 1):
            fa = FocusArea(rank=i, title=a["title"], category=a["category"], description=a["description"],
                           occurrences=a.get("occurrences", 0), sessions=a.get("sessions", 0),
                           examples=a.get("examples", []),
                           agent_hint=a.get("agent_hint", ""))
            fa.drills = [Drill(prompt=d["prompt"], answer=d["answer"]) for d in a.get("drills", [])]
            run.focus_areas.append(fa)
        s.add(run)
        s.flush()
        return _run_dict(run)


def latest_pattern_run() -> dict | None:
    with db() as s:
        run = s.scalars(select(PatternRun).order_by(PatternRun.id.desc()).limit(1)).first()
        return _run_dict(run) if run else None


def _run_dict(run: PatternRun) -> dict:
    return {"id": run.id, "created_at": run.created_at.isoformat(timespec="seconds") if run.created_at else None,
            "mistakes_analysed": run.mistakes_analysed, "last_mistake_id": run.last_mistake_id,
            "overview": run.overview,
            "focus_areas": [{"id": f.id, "rank": f.rank, "title": f.title, "category": f.category,
                             "description": f.description, "occurrences": f.occurrences,
                             "sessions": f.sessions or 0,
                             "examples": f.examples or [], "agent_hint": f.agent_hint,
                             "drills": [{"id": d.id, "prompt": d.prompt, "answer": d.answer} for d in f.drills]}
                            for f in run.focus_areas]}


def focus_totals() -> dict:
    """Headline numbers for the Focus page: mistakes, sessions with speech, minutes spoken, mistakes by type."""
    with db() as s:
        mistakes = s.scalar(select(func.count(Mistake.id))) or 0
        sessions = s.scalar(select(func.count(func.distinct(Utterance.session_id))).where(Utterance.speaker == "me")) or 0
        ms = s.scalar(select(func.sum(Utterance.duration_ms)).where(Utterance.speaker == "me")) or 0
        cats = s.execute(select(Mistake.category, func.count()).group_by(Mistake.category)
                         .order_by(func.count().desc())).all()
    minutes = ms / 60000
    return {"mistakes": mistakes, "sessions": sessions, "minutes": round(minutes, 1) if minutes < 10 else round(minutes),
            "categories": [{"category": c, "n": n} for c, n in cats]}


# ---------------------------------------------------------------- progress numbers
def daily_stats(days: int = 14) -> list[dict]:
    """Per day: words, sentences, mistakes, fillers, speaking time (only the user's own lines)."""
    since = datetime.now() - timedelta(days=days)
    with db() as s:
        utts = s.execute(select(Utterance.id, Utterance.created_at, Utterance.word_count, Utterance.duration_ms)
                         .where(Utterance.speaker == "me", Utterance.created_at >= since)).all()
        mis = s.execute(select(Mistake.created_at, Mistake.category).join(Utterance)
                        .where(Utterance.speaker == "me", Mistake.created_at >= since)).all()
    by: dict[str, dict] = {}
    for _, ts, wc, ms in utts:
        d = by.setdefault(ts.date().isoformat(), {"words": 0, "sentences": 0, "mistakes": 0, "fillers": 0,
                                                  "timed_words": 0, "speech_ms": 0})
        d["words"] += wc or 0
        d["sentences"] += 1
        if ms and ms > 500:
            d["timed_words"] += wc or 0
            d["speech_ms"] += ms
    for ts, cat in mis:
        d = by.get(ts.date().isoformat())
        if d:
            d["mistakes"] += 1
            d["fillers"] += cat == "filler"
    return [{"day": k, **v} for k, v in sorted(by.items())]


# ---------------------------------------------------------------- voice profile
def get_voice_profile() -> dict | None:
    with db() as s:
        p = s.scalars(select(VoiceProfile).order_by(VoiceProfile.id.desc()).limit(1)).first()
        if not p:
            return None
        return {"embedding": np.frombuffer(p.embedding, dtype=np.float32), "threshold": p.threshold,
                "enabled": p.enabled, "created_at": p.created_at.isoformat(timespec="seconds")}


def save_voice_profile(embedding: np.ndarray, threshold: float):
    with db() as s:
        s.execute(delete(VoiceProfile))
        s.add(VoiceProfile(embedding=embedding.astype(np.float32).tobytes(), threshold=threshold))


def set_voice_enabled(enabled: bool):
    with db() as s:
        p = s.scalars(select(VoiceProfile).limit(1)).first()
        if p:
            p.enabled = enabled


def delete_voice_profile():
    with db() as s:
        s.execute(delete(VoiceProfile))
