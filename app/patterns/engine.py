"""Pattern engine: find the learner's recurring mistakes and turn them into Focus Areas with drills.

Two stages:
1. `aggregate` (pure Python, unit-tested): group the mistake history by rule, count occurrences, distinct days
   and sessions, weight recent ones higher, keep a few real examples.
2. `analyse_history` (LLM): merges related groups into 3-5 Focus Areas, explains each rule simply, picks the
   learner's own examples, writes drills and a hint telling the practice agent how to steer the conversation.

Runs on demand ("Analyse my history") and automatically after a session when `should_run` says enough new
material has arrived.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from ..config import settings
from ..llm import json_call
from ..storage import repo
from ..storage.models import CATEGORIES

log = logging.getLogger("patterns")

MIN_NEW_MISTAKES = 15         # automatic run after this many new mistakes...
MAX_AGE = timedelta(hours=24)  # ...or once a day if there is anything new at all

FOCUS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["overview", "focus_areas"],
    "properties": {
        "overview": {"type": "string"},
        "focus_areas": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "category", "description", "rules", "drills", "agent_hint"],
            "properties": {
                "title": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORIES},
                "description": {"type": "string"},
                "rules": {"type": "array", "items": {"type": "string"},
                          "description": "the exact rule names from the data that this focus area covers"},
                "drills": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["prompt", "answer"],
                    "properties": {"prompt": {"type": "string"}, "answer": {"type": "string"}}}},
                "agent_hint": {"type": "string"},
            }}},
    },
}


def aggregate(mistakes: list[dict], today: date | None = None, max_groups: int = 25) -> list[dict]:
    """Group mistakes by rule. Score = count weighted by recency (last 7 days x2) + spread across days."""
    today = today or date.today()
    groups: dict[str, dict] = defaultdict(lambda: {"count": 0, "days": set(), "sessions": set(),
                                                   "examples": [], "recent": 0, "categories": defaultdict(int)})
    for m in mistakes:
        g = groups[m["rule"]]
        g["count"] += 1
        g["days"].add(m["day"])
        g["sessions"].add(m["session_id"])
        g["categories"][m["category"]] += 1
        if (today - date.fromisoformat(m["day"])).days <= 7:
            g["recent"] += 1
        g["examples"].append({"original": m["original"], "corrected": m["corrected"], "sentence": m.get("sentence", "")})
    out = []
    for rule, g in groups.items():
        out.append({
            "rule": rule,
            "category": max(g["categories"], key=g["categories"].get),
            "count": g["count"], "days": len(g["days"]), "sessions": len(g["sessions"]),
            "session_ids": sorted(g["sessions"]),
            "score": g["count"] + g["recent"] + 2 * (len(g["days"]) - 1),
            "examples": g["examples"][-4:],  # most recent last
        })
    out.sort(key=lambda x: (-x["score"], -x["count"], x["rule"]))
    return out[:max_groups]


def should_run(last_run: dict | None, total_mistakes: int, max_mistake_id: int, now: datetime | None = None) -> bool:
    """Automatic trigger after a session."""
    if total_mistakes < 5:
        return False  # not enough history to find patterns
    if not last_run:
        return True
    new = max_mistake_id - (last_run.get("last_mistake_id") or 0)
    if new <= 0:
        return False
    age = (now or datetime.now()) - datetime.fromisoformat(last_run["created_at"])
    return new >= MIN_NEW_MISTAKES or age >= MAX_AGE


SYSTEM = """You are an expert {language} speaking coach analysing a learner's mistake history (from their own
speech, grouped by rule, with counts, number of days seen and recent examples).
Find their 3-5 most important RECURRING patterns. Prefer mistakes that repeat across many days and recent
sessions over one-offs. Merge groups that are really the same rule. Fillers count only if frequent.
For each focus area:
- title: short and clear ("Past tense for finished actions").
- description: the rule in 1-2 simple sentences.
- rules: the exact rule names (copied from the data) of every group this focus area covers.
- drills: 4 short practice items. "prompt" is a fill-in or transform task about everyday life
  ("Yesterday I ___ (go) to the market."), "answer" is the full correct sentence to say out loud.
- agent_hint: one instruction for an AI conversation partner on how to make the learner USE this rule naturally
  (e.g. "Ask about what they did last weekend so they must use past simple").
overview: 1-2 encouraging sentences about overall progress and the main priority."""


def analyse_history() -> dict:
    """Run the full pattern analysis and store the resulting Focus Areas."""
    history = repo.all_mistakes()
    if len(history) < 3:
        raise ValueError("Not enough history yet. Talk for a few minutes first.")
    groups = aggregate(history)
    lines = [f"- rule: {g['rule']} | category: {g['category']} | count: {g['count']} | days: {g['days']} | "
             f"sessions: {g['sessions']} | examples: " +
             "; ".join(f"{e['original']} -> {e['corrected']}" for e in g["examples"]) for g in groups]
    out = json_call(model=settings.pattern_model, system=SYSTEM.format(language=settings.language),
                    user=f"Total mistakes: {len(history)}\nGroups (most important first):\n" + "\n".join(lines),
                    schema=FOCUS_SCHEMA, name="focus_areas", reasoning="low")
    areas = [attach_evidence(a, groups) for a in out.get("focus_areas", []) if a.get("title")][:5]
    _, max_id = repo.mistake_count()
    return repo.save_pattern_run(areas, out.get("overview", ""), len(history), max_id)


def attach_evidence(area: dict, groups: list[dict]) -> dict:
    """Examples and counts come from the learner's real data (never written by the model): the groups whose
    rule names the model listed, falling back to groups of the same category."""
    by_rule = {g["rule"]: g for g in groups}
    matched = [by_rule[r] for r in area.get("rules") or [] if r in by_rule]
    category = area.get("category") if area.get("category") in CATEGORIES else "other"
    if not matched:
        matched = [g for g in groups if g["category"] == category][:2]
    examples, seen = [], set()
    for g in matched:
        for e in reversed(g["examples"]):  # most recent first
            key = (e["original"].lower(), e["corrected"].lower())
            if key not in seen:
                seen.add(key)
                examples.append({"original": e["original"], "corrected": e["corrected"], "sentence": e["sentence"]})
    return {"title": area["title"], "category": category, "description": area.get("description", ""),
            "occurrences": sum(g["count"] for g in matched),
            "sessions": len({sid for g in matched for sid in g.get("session_ids", [])}),
            "examples": examples[:3],
            "drills": [d for d in area.get("drills", []) if d.get("prompt") and d.get("answer")][:5],
            "agent_hint": area.get("agent_hint", "")}


def maybe_run() -> dict | None:
    """Called in the background after each session."""
    total, max_id = repo.mistake_count()
    if not should_run(repo.latest_pattern_run(), total, max_id):
        return None
    try:
        return analyse_history()
    except Exception as e:  # background job: log, never crash the app
        log.warning("automatic pattern analysis failed: %s", e)
        return None


def agent_steering() -> list[str]:
    """Focus areas as instructions for the practice agent (empty until the first analysis)."""
    run = repo.latest_pattern_run()
    if not run:
        return []
    return [f"{fa['title']}: {fa['agent_hint']}" for fa in run["focus_areas"] if fa.get("agent_hint")]


def focus_titles() -> list[str]:
    run = repo.latest_pattern_run()
    return [f"{fa['title']} ({fa['description']})" for fa in run["focus_areas"]] if run else []
