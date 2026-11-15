"""Progress numbers: mistakes per 100 words, fillers per 100 words, speaking speed, with a daily trend."""


def per100(n: int, words: int) -> float | None:
    return round(n * 100 / words, 1) if words else None


def day_metrics(d: dict) -> dict:
    return {
        "day": d["day"], "words": d["words"], "sentences": d["sentences"],
        "mistakes_per_100": per100(d["mistakes"] - d["fillers"], d["words"]),   # fillers are tracked separately
        "fillers_per_100": per100(d["fillers"], d["words"]),
        "wpm": round(d["timed_words"] * 60000 / d["speech_ms"]) if d["speech_ms"] else None,
    }


def progress(daily: list[dict]) -> dict:
    """`daily` comes from repo.daily_stats(). Compares the latest day with the average of the days before it."""
    trend = [day_metrics(d) for d in daily]
    if not trend:
        return {"today": None, "before": None, "trend": []}
    latest, earlier = trend[-1], trend[:-1]

    def avg(key):
        vals = [t[key] for t in earlier if t[key] is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    before = {k: avg(k) for k in ("mistakes_per_100", "fillers_per_100", "wpm")} if earlier else None
    return {"today": latest, "before": before, "trend": trend}
