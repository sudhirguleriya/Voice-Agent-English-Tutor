"""Practice helpers: compare a spoken attempt with a target sentence, and spaced-repetition scheduling."""
import re
from datetime import date, timedelta
from difflib import SequenceMatcher

# Leitner boxes: after a correct answer the card moves up a box and comes back after this many days.
INTERVALS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
MASTERED_BOX = 4
SPOKEN_FILLERS = {"um", "uh", "erm", "er", "hmm", "mm", "ah"}


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower().replace("’", "'"))


def compare(target: str, said: str, must: str = "") -> dict:
    """Word-level match between what the user should say and what they said."""
    tw = words(target)
    sw = [w for w in words(said) if w not in SPOKEN_FILLERS]
    ratio = SequenceMatcher(None, tw, sw, autojunk=False).ratio() if tw else 0.0
    need = [w for w in words(must) if w not in SPOKEN_FILLERS]
    must_ok = all(w in sw for w in need)
    diff = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, tw, sw, autojunk=False).get_opcodes():
        if op == "equal":
            diff += [{"op": "eq", "w": w} for w in tw[i1:i2]]
        else:
            diff += [{"op": "miss", "w": w} for w in tw[i1:i2]]   # should have said
            diff += [{"op": "extra", "w": w} for w in sw[j1:j2]]  # said instead
    return {"similarity": round(ratio, 2), "must_ok": must_ok,
            "passed": ratio >= 0.75 and must_ok, "diff": diff}


def schedule(box: int, passed: bool, day: str) -> tuple[int, str]:
    box = min(5, box + 1) if passed else 1
    due = date.fromisoformat(day) + timedelta(days=INTERVALS[box] if passed else 0)
    return box, due.isoformat()


def pick_sentence(text: str, fragment: str, max_words: int = 28) -> str:
    """The sentence of `text` that contains `fragment`, trimmed to a readable length."""
    text = (text or "").strip()
    frag = (fragment or "").strip().lower()
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()] or [text]
    hit = next((p for p in parts if frag and frag in p.lower()), None)
    if hit is None:
        hit = text if len(parts) == 1 else max(parts, key=lambda p: _overlap(p, frag))
    toks = hit.split()
    if len(toks) <= max_words:
        return hit
    fw = frag.split()
    idx = next((i for i in range(len(toks)) if fw and toks[i].lower().strip(",.!?") == fw[0]), 0)
    start = max(0, idx - max_words // 2)
    return ("… " if start else "") + " ".join(toks[start:start + max_words]) + " …"


def _overlap(sentence: str, frag: str) -> int:
    s = set(words(sentence))
    return sum(w in s for w in words(frag))
