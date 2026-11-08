"""Daily report: today vs yesterday, repeated mistakes, fillers, vocabulary, pronunciation."""
import re
from collections import Counter
from datetime import date, timedelta

FILLERS = ["um", "uh", "erm", "like", "basically", "actually", "literally", "you know",
           "i mean", "kind of", "sort of", "so yeah", "right"]
STOP = set("""a an the and or but if so to of in on at for with from by as is are was were be been am i you he she
it we they me him her us them my your his its our their this that these those do does did have has had not no
yes there here what which who when where why how then than just can could will would should may might must
shall let get got go going im dont its thats ive id youre theyre were""".split())


def _day_shift(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _words(text):
    return re.findall(r"[a-z']+", text.lower())


def _filler_counts(texts):
    c = Counter()
    for t in texts:
        low = " " + " ".join(_words(t)) + " "
        for f in FILLERS:
            n = low.count(f" {f} ")
            if n:
                c[f] += n
    return c


def _day_stats(db, day):
    utts = db.query("SELECT text, score, word_count FROM utterances WHERE day=?", (day,))
    mis = db.query("SELECT category, pattern, original, correction FROM mistakes WHERE day=?", (day,))
    words = sum(u["word_count"] or 0 for u in utts)
    scores = [u["score"] for u in utts if u["score"] is not None]
    texts = [u["text"] for u in utts]
    content = Counter(w for t in texts for w in _words(t) if w not in STOP and len(w) > 2)
    return {
        "utterances": len(utts),
        "words": words,
        "mistakes": len(mis),
        "per100": round(len(mis) * 100 / words, 1) if words else None,
        "avg_score": round(sum(scores) / len(scores)) if scores else None,
        "_cats": Counter(m["category"] for m in mis),
        "_patterns": Counter(m["pattern"] for m in mis),
        "_examples": {m["pattern"]: m for m in mis},
        "_fillers": _filler_counts(texts),
        "_overused": content,
    }


def build_report(db, day: str) -> dict:
    yday = _day_shift(day, -1)
    t, y = _day_stats(db, day), _day_stats(db, yday)

    cats = sorted(set(t["_cats"]) | set(y["_cats"]), key=lambda c: -(t["_cats"][c] + y["_cats"][c]))
    categories = [{"category": c, "today": t["_cats"][c], "yesterday": y["_cats"][c]} for c in cats]

    # history of each pattern: total count + number of distinct days seen
    hist = {r["pattern"]: r for r in db.query(
        "SELECT pattern, category, COUNT(*) total, COUNT(DISTINCT day) days, MIN(day) first_seen"
        " FROM mistakes WHERE day<=? GROUP BY pattern", (day,))}

    def pat(p, src):
        ex = src["_examples"].get(p) or {}
        h = hist.get(p, {})
        return {"pattern": p, "category": h.get("category") or ex.get("category"),
                "today": t["_patterns"][p], "yesterday": y["_patterns"][p],
                "total": h.get("total", 0), "days": h.get("days", 0), "first_seen": h.get("first_seen"),
                "example": {"original": ex.get("original"), "correction": ex.get("correction")}}

    tp, yp = set(t["_patterns"]), set(y["_patterns"])
    patterns = {
        "repeated_today": sorted([pat(p, t) for p in tp if t["_patterns"][p] >= 2], key=lambda x: -x["today"]),
        "still_making": sorted([pat(p, t) for p in tp & yp], key=lambda x: -(x["today"] + x["yesterday"])),
        "fixed_since_yesterday": sorted([pat(p, y) for p in yp - tp], key=lambda x: -x["yesterday"]),
        "new_today": sorted([pat(p, t) for p in tp - yp], key=lambda x: -x["today"]),
        "all_time_top": [],
    }
    top = db.query(
        "SELECT pattern FROM mistakes WHERE day<=? GROUP BY pattern HAVING COUNT(*)>=2"
        " ORDER BY COUNT(DISTINCT day) DESC, COUNT(*) DESC LIMIT 10", (day,))
    for r in top:
        p = r["pattern"]
        ex = db.query("SELECT original, correction, category FROM mistakes WHERE pattern=? ORDER BY id DESC LIMIT 1", (p,))[0]
        patterns["all_time_top"].append({**pat(p, t), "category": ex["category"],
                                         "example": {"original": ex["original"], "correction": ex["correction"]}})

    # Fillers come from the coach's analysis (it knows "I like tea" is not a filler); fall back to word counts.
    def coach_fillers(d):
        rows = db.query("SELECT original FROM mistakes WHERE day=? AND category='filler'", (d,))
        return Counter(" ".join(_words(r["original"] or "")) for r in rows if _words(r["original"] or ""))
    tf, yf = coach_fillers(day), coach_fillers(yday)
    if not tf and not yf:
        tf, yf = t["_fillers"], y["_fillers"]
    fillers = sorted(
        [{"word": f, "today": tf[f], "yesterday": yf[f]}
         for f in set(tf) | set(yf)], key=lambda x: -(x["today"] + x["yesterday"]))

    overused = [{"word": w, "count": n} for w, n in t["_overused"].most_common(8) if n >= 3]

    vocab = db.query(
        "SELECT word, COUNT(*) n, MAX(alternatives) alternatives FROM vocab WHERE day=? GROUP BY word"
        " ORDER BY n DESC LIMIT 12", (day,))
    import json as _j
    for v in vocab:
        v["alternatives"] = _j.loads(v["alternatives"] or "[]")
        v["yesterday"] = db.query("SELECT COUNT(*) n FROM vocab WHERE day=? AND word=?", (yday, v["word"]))[0]["n"]

    pron = db.query(
        "SELECT word, COUNT(*) n, MAX(tip) tip, MAX(heard_as) heard_as FROM pronunciation WHERE day=?"
        " GROUP BY word ORDER BY n DESC LIMIT 12", (day,))
    for p in pron:
        p["yesterday"] = db.query("SELECT COUNT(*) n FROM pronunciation WHERE day=? AND word=?", (yday, p["word"]))[0]["n"]
        p["all_days"] = db.query("SELECT COUNT(DISTINCT day) n FROM pronunciation WHERE word=? AND day<=?", (p["word"], day))[0]["n"]

    strip = lambda s: {k: v for k, v in s.items() if not k.startswith("_")}
    trend = db.query(
        "SELECT u.day, SUM(u.word_count) words, COUNT(*) utterances, ROUND(AVG(u.score)) avg_score,"
        " (SELECT COUNT(*) FROM mistakes m WHERE m.day=u.day) mistakes"
        " FROM utterances u WHERE u.day<=? GROUP BY u.day ORDER BY u.day DESC LIMIT 14", (day,))
    for r in trend:
        r["per100"] = round(r["mistakes"] * 100 / r["words"], 1) if r["words"] else None

    return {
        "day": day, "yesterday": yday,
        "totals": {"today": strip(t), "yesterday": strip(y)},
        "categories": categories,
        "patterns": patterns,
        "fillers": fillers,
        "overused_words": overused,
        "vocabulary": vocab,
        "pronunciation": pron,
        "trend": list(reversed(trend)),
    }
