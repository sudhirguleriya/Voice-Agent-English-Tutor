"""End-of-session summary: full transcript, a corrected clean paragraph, and every mistake."""
from ..config import settings
from ..llm import json_call
from ..storage import repo

SUMMARY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "corrected_paragraph", "key_points"],
    "properties": {
        "title": {"type": "string", "description": "2-5 word name of what the session was about"},
        "corrected_paragraph": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """You are a {language} speaking coach. Below is everything the learner said in one session, in order
(verbatim speech: mistakes, fillers, repeats). Write what they SHOULD have said: one clean, natural paragraph
(or a few if the topic changes) with the same meaning, the same order and the same facts. Keep exactly who did
what: never change "he", "she", "my brother" or "they" into "I" (only sentences about the speaker use "I").
Fix grammar, remove fillers and repetition, use natural linking words. Keep it about the same length and use
simple, natural words, not fancier ones.
title: a 2-5 word name for what they talked about ("Client meeting recap", "Weekend in Goa").
Then give 2-4 key_points: the most useful lessons from this session, each one short line
("Use past simple for finished actions: I went, not I go")."""


def transcript_lines(session: dict) -> list[str]:
    """The user's own lines only (other people and the agent are not graded)."""
    return [u["text"] for u in session.get("utterances", []) if u["speaker"] == "me"]


def build_summary(session_id: int) -> dict:
    session = repo.get_session(session_id)
    if not session:
        raise ValueError("Session not found")
    lines = transcript_lines(session)
    mistakes = [m for u in session["utterances"] if u["speaker"] == "me" for m in u["mistakes"]]
    if lines:
        out = json_call(model=settings.summary_model, system=SYSTEM.format(language=settings.language),
                        user="\n".join(f"{i}. {t}" for i, t in enumerate(lines, 1)),
                        schema=SUMMARY_SCHEMA, name="session_summary", reasoning="low")
        paragraph = (out.get("corrected_paragraph") or "").strip()
        points = [p.strip() for p in out.get("key_points") or [] if p.strip()][:4]
        title = " ".join((out.get("title") or "").split()).strip(" .\"'")
    else:
        paragraph, points, title = "", [], None
    repo.save_summary(session_id, paragraph, points, title or session.get("topic"))
    return {**repo.get_session(session_id), "transcript": " ".join(lines), "all_mistakes": mistakes}
