"""Pattern engine: aggregation, the automatic trigger, storing Focus Areas, and agent steering."""
from datetime import date, datetime, timedelta

from app.patterns import engine
from app.patterns.progress import progress
from app.practice_agent.agent import history_messages, system_prompt
from app.storage import repo


def M(rule, day, session=1, category="tense", original="go", corrected="went"):
    return {"rule": rule, "day": day, "session_id": session, "category": category,
            "original": original, "corrected": corrected}


def test_aggregate_ranks_recurring_recent_mistakes_first():
    today = date(2026, 11, 14)
    hist = ([M("past simple", "2026-11-14", 3)] * 2 + [M("past simple", "2026-11-10", 2)] +
            [M("articles", "2026-10-01", 1, "article", "go to market", "go to the market")] * 4 +
            [M("one-off", "2026-11-14", 3, "word_choice")])
    g = engine.aggregate(hist, today)
    assert [x["rule"] for x in g] == ["past simple", "articles", "one-off"]
    top = g[0]
    assert (top["count"], top["days"], top["sessions"], top["category"]) == (3, 2, 2, "tense")
    assert len(g[1]["examples"]) == 4 and g[1]["examples"][0]["corrected"] == "go to the market"


def test_should_run():
    now = datetime(2026, 11, 14, 12, 0)
    assert not engine.should_run(None, 3, 3, now)                       # too little history
    assert engine.should_run(None, 10, 10, now)                         # first run
    run = {"last_mistake_id": 10, "created_at": (now - timedelta(hours=2)).isoformat()}
    assert not engine.should_run(run, 10, 10, now)                      # nothing new
    assert not engine.should_run(run, 14, 14, now)                      # a few new, run is recent
    assert engine.should_run(run, 25, 25, now)                          # many new mistakes
    old = {**run, "created_at": (now - timedelta(hours=30)).isoformat()}
    assert engine.should_run(old, 11, 11, now)                          # a day old, something new


def _seed_mistakes():
    sid = repo.create_session("live")
    for text, rule in [("Yesterday I go to market", "past simple"), ("He go to office", "third person -s"),
                       ("Last week I go home", "past simple"), ("I am go there", "verb form")]:
        u = repo.add_utterance(sid, "me", text, 2000)
        repo.save_analysis(u["id"], text, [{"original": "go", "corrected": "went", "category": "tense",
                                            "rule": rule, "explanation": "x"}], {})
    return sid


FAKE_FOCUS = {"overview": "Good progress. Main priority: past tense.", "focus_areas": [
    {"title": "Past tense for finished actions", "category": "tense", "description": "Use went, not go.",
     "rules": ["past simple", "made-up rule the model invented"],
     "drills": [{"prompt": "Yesterday I ___ (go) home.", "answer": "Yesterday I went home."},
                {"prompt": "", "answer": "dropped: empty prompt"}],
     "agent_hint": "Ask what they did last weekend."},
    {"title": "Odd", "category": "not-a-category", "description": "d", "rules": [],
     "drills": [], "agent_hint": ""}]}


def test_analyse_history_stores_focus_areas_and_steers_agent(monkeypatch):
    _seed_mistakes()
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return FAKE_FOCUS

    monkeypatch.setattr(engine, "json_call", fake)
    run = engine.analyse_history()
    assert "rule: past simple" in seen["user"] and "count: 2" in seen["user"]
    assert run["mistakes_analysed"] == 4 and run["last_mistake_id"] == 4
    fa = run["focus_areas"]
    assert fa[0]["title"] == "Past tense for finished actions" and len(fa[0]["drills"]) == 1
    # examples and counts come from the real data, not from the model
    assert fa[0]["occurrences"] == 2 and fa[0]["sessions"] == 1
    assert fa[0]["examples"][0] == {"original": "go", "corrected": "went", "sentence": "Last week I go home"}
    assert fa[1]["category"] == "other" and fa[1]["examples"] == []
    assert repo.latest_pattern_run()["id"] == run["id"]
    steer = engine.agent_steering()
    assert steer == ["Past tense for finished actions: Ask what they did last weekend."]
    assert "Ask what they did last weekend." in system_prompt("Travel", steer, "English")


def test_maybe_run_waits_for_enough_history(monkeypatch):
    monkeypatch.setattr(engine, "json_call", lambda **kw: FAKE_FOCUS)
    assert engine.maybe_run() is None           # no mistakes at all
    _seed_mistakes()
    _seed_mistakes()                            # 8 mistakes: enough for a first run
    assert engine.maybe_run() is not None
    assert engine.maybe_run() is None           # nothing new since


def test_agent_history_excludes_other_people():
    lines = [{"speaker": "agent", "text": "Hi!"}, {"speaker": "me", "text": "Hello"},
             {"speaker": "other", "text": "(TV)"}]
    assert history_messages(lines) == [{"role": "assistant", "content": "Hi!"}, {"role": "user", "content": "Hello"}]


def test_progress_numbers():
    daily = [{"day": "2026-11-13", "words": 200, "sentences": 20, "mistakes": 30, "fillers": 10,
              "timed_words": 200, "speech_ms": 100000},
             {"day": "2026-11-14", "words": 100, "sentences": 10, "mistakes": 8, "fillers": 2,
              "timed_words": 100, "speech_ms": 40000}]
    p = progress(daily)
    assert p["today"]["mistakes_per_100"] == 6.0 and p["today"]["fillers_per_100"] == 2.0 and p["today"]["wpm"] == 150
    assert p["before"]["mistakes_per_100"] == 10.0
