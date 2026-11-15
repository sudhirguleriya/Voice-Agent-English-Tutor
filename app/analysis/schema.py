"""JSON schemas for structured LLM output, plus validation/normalisation of what comes back.

The normalisers are pure functions (no network), so they are unit-tested directly.
"""
import re

from ..storage.models import CATEGORIES

SENTENCE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["is_directed_speech", "corrected", "mistakes", "suggestions"],
    "properties": {
        "is_directed_speech": {"type": "boolean",
                               "description": "false for noise, TV, or meaningless fragments"},
        "corrected": {"type": "string", "description": "The same sentence with only the errors fixed"},
        "mistakes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["original", "corrected", "category", "rule", "explanation"],
            "properties": {
                "original": {"type": "string", "description": "the wrong words, as said"},
                "corrected": {"type": "string", "description": "the fixed words"},
                "category": {"type": "string", "enum": CATEGORIES},
                "rule": {"type": "string", "description": "short reusable rule name"},
                "explanation": {"type": "string", "description": "one short sentence, simple words"},
            }}},
        "suggestions": {
            "type": "object", "additionalProperties": False,
            "required": ["better_phrasing", "vocabulary", "next_sentences"],
            "properties": {
                "better_phrasing": {"type": "string",
                                    "description": "how a fluent speaker would naturally say it; '' if already natural"},
                "vocabulary": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["word", "better"],
                    "properties": {"word": {"type": "string"},
                                   "better": {"type": "array", "items": {"type": "string"}}}}},
                "next_sentences": {"type": "array", "items": {"type": "string"},
                                   "description": "1-2 sentences the user could naturally say next"},
            }},
    },
}

HINT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["fixes", "continuations"],
    "properties": {
        "fixes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["wrong", "right"],
            "properties": {"wrong": {"type": "string"}, "right": {"type": "string"}}}},
        "continuations": {"type": "array", "items": {"type": "string"}},
    },
}


def clean(s) -> str:
    return " ".join(str(s or "").split())


def normalize_rule(rule: str, category: str, original: str = "", corrected: str = "") -> str:
    """Stable, lower-case rule name. Drops the "(wrong -> right)" example part so the same rule always matches.
    Falls back to "<category>: <original> -> <corrected>" when the model gives no usable name."""
    r = (rule or "").lower().replace("→", "->").replace("_", " ")
    r = re.sub(r"\(.*$", "", r)                  # "third person -s (he go -> he goes)" -> "third person -s"
    r = re.sub(r"^\s*\[[a-z ]+\]\s*", "", r)     # leading "[tense]"
    r = " ".join(r.strip(" .:-").split())[:120]
    vague = {"", "grammar", "tense", "article", "preposition", "word choice", "vocabulary", "other", "mistake"}
    if r in vague:
        words = lambda s: " ".join(re.findall(r"[a-z']+", (s or "").lower())[:4])
        if category == "filler":
            return f"filler word {words(original)}".strip()
        return f"{category.replace('_', ' ')}: {words(original)} -> {words(corrected)}"[:120]
    return r


def normalize_sentence_result(raw: dict, max_mistakes: int = 8) -> dict:
    """Validate the analyser's JSON: known categories, stable rule names, no empty or no-op corrections."""
    mistakes, seen = [], set()
    for m in raw.get("mistakes") or []:
        if not isinstance(m, dict):
            continue
        original, corrected = clean(m.get("original")), clean(m.get("corrected"))
        if not original or original.lower() == corrected.lower():
            continue  # a "mistake" whose correction is identical is noise
        category = m.get("category") if m.get("category") in CATEGORIES else "other"
        rule = normalize_rule(m.get("rule"), category, original, corrected)
        key = (original.lower(), rule)
        if key in seen:
            continue
        seen.add(key)
        mistakes.append({"original": original, "corrected": corrected, "category": category, "rule": rule,
                         "explanation": clean(m.get("explanation"))})
    mistakes = drop_overlapping(mistakes)
    sug = raw.get("suggestions") or {}
    vocab = [{"word": clean(v.get("word")), "better": [clean(b) for b in (v.get("better") or []) if clean(b)][:3]}
             for v in (sug.get("vocabulary") or []) if isinstance(v, dict) and clean(v.get("word"))]
    return {
        "is_directed_speech": bool(raw.get("is_directed_speech", True)),
        "corrected": clean(raw.get("corrected")),
        "mistakes": mistakes[:max_mistakes],
        "suggestions": {
            "better_phrasing": clean(sug.get("better_phrasing")),
            "vocabulary": [v for v in vocab if v["better"]][:3],
            "next_sentences": [clean(s) for s in (sug.get("next_sentences") or []) if clean(s)][:2],
        },
    }


def drop_overlapping(mistakes: list[dict]) -> list[dict]:
    """The model sometimes lists one error twice: as a word ("go -> went") and again inside a longer phrase
    ("He go to office -> He went to the office"). Keep the small, specific ones; drop a phrase-level mistake
    whose wrong words contain another listed mistake (it would double-count in the statistics)."""
    def words(s: str) -> str:  # " he go to office " (padded, so " go " never matches inside "going")
        return " " + " ".join(re.findall(r"[a-z0-9']+", s.lower())) + " "

    keep = []
    for m in mistakes:
        mine = words(m["original"])
        inner = [o for o in mistakes if o is not m and len(words(o["original"])) < len(mine)
                 and words(o["original"]) in mine]
        if not inner:
            keep.append(m)
    return keep


def normalize_hints(raw: dict) -> dict:
    fixes = [{"wrong": clean(f.get("wrong")), "right": clean(f.get("right"))}
             for f in (raw.get("fixes") or []) if isinstance(f, dict)]
    fixes = [f for f in fixes if f["wrong"] and f["right"] and f["wrong"].lower() != f["right"].lower()]
    return {"fixes": fixes[:2],
            "continuations": [clean(c) for c in (raw.get("continuations") or []) if clean(c)][:3]}
