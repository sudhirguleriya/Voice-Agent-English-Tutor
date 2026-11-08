"""Exercises the full server pipeline with OpenAI mocked out (no key or network needed)."""
import io, os, struct, sys, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["OPENAI_API_KEY"] = "test"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from app import main

SAYINGS = {
  "2026-11-07": [("He go to office yesterday and he don't like it", ["third person -s (he go -> he goes)", "past tense for finished time"]),
                 ("I am very very happy, basically it was a good day", ["needless repetition"])],
  "2026-11-08": [("She go to market, um, like every day", ["third person -s (he go -> he goes)"]),
                 ("My brother go to school by walk", ["third person -s (he go -> he goes)", "by walk -> on foot"])],
}
queue = []

class FakeCoach:
    def transcribe(self, wav): return queue[0][0]
    def analyze(self, text, known, context=None):
        _, pats = queue.pop(0)
        return {"is_speech": True, "corrected": text + " (fixed)", "better_version": "Better: " + text, "score": 70,
                "tip": "Keep going", "pronunciation_suspects": [], "next_suggestions": ["Then I went home.", " "],
                "mistakes": [{"category": "grammar", "original": "go", "correction": "goes", "explanation": "x", "pattern": p} for p in pats],
                "vocabulary_upgrades": [{"word": "good", "alternatives": ["productive", "rewarding"], "example": "It was a productive day"}]}
    def pronunciation(self, wav, text): return [{"word": "vegetable", "heard_as": "veg-e-table", "tip": "VEJ-tuh-bul"}]
    tts_model, tts_voice, tts_fallback, tts_fallback_voice = "fake-tts", "alloy", "", ""
    spoken = []
    def speak(self, text):
        self.spoken.append(text)
        return b"RIFF" + b"\x00" * 600, "audio/wav", "fake-tts"
    def live_suggest(self, partial, context): return {"fixes": [{"wrong": "i go", "right": "i went"}], "continuations": [partial + " the shop."]}
    def polish_conversation(self, sentences): return {"polished": " ".join(sentences), "key_changes": ["he go -> he goes"]}
    def daily_plan(self, r): return {"headline": "Nice progress", "progress": "Fewer mistakes", "focus": ["he goes"], "drills": ["He goes to work."], "pronounce": ["vegetable"], "new_words": ["rewarding"]}

main._coach = FakeCoach()
c = TestClient(main.app)

def wav(sec=1.0, rate=16000):
    n = int(sec * rate); data = b"\x00\x00" * n
    return b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate*2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data

for day, items in SAYINGS.items():
    for text, pats in items:
        queue.append((text, pats))
        r = c.post("/api/analyze", files={"audio": ("s.wav", wav(), "audio/wav")}, data={"day": day})
        assert r.status_code == 200, r.text
        j = r.json()
print("last response times_seen:", [m["times_seen"] for m in j["mistakes"]])
assert j["mistakes"][0]["times_seen"] == 3

# hallucination filter
queue.append(("Thank you.", []))
assert c.post("/api/analyze", files={"audio": ("s.wav", wav(), "audio/wav")}, data={"day": "2026-11-08"}).json()["skipped"]
queue.clear()

rep = c.get("/api/report?day=2026-11-08").json()
print("totals:", rep["totals"])
print("still_making:", [p["pattern"] for p in rep["patterns"]["still_making"]])
print("fixed:", [p["pattern"] for p in rep["patterns"]["fixed_since_yesterday"]])
print("new:", [p["pattern"] for p in rep["patterns"]["new_today"]])
print("repeated today:", [(p["pattern"], p["today"]) for p in rep["patterns"]["repeated_today"]])
print("fillers:", rep["fillers"])
print("pron:", rep["pronunciation"])
assert [p["pattern"] for p in rep["patterns"]["still_making"]] == ["third person -s"]
assert {"um", "like"} <= {f["word"] for f in rep["fillers"]}
assert c.post("/api/report/plan?day=2026-11-08").json()["headline"] == "Nice progress"
assert c.get("/api/report?day=2026-11-08").json()["plan"]["drills"]
assert len(c.get("/api/day/2026-11-08").json()) == 2
assert rep["focus"] and rep["focus"][0]["pattern"] == "third person -s", rep["focus"]
assert {p["status"] for p in rep["patterns"]["today_all"]} >= {"still", "new"}

# ---- practice: spaced repetition deck from the user's own mistakes
pr = c.get("/api/practice?day=2026-11-08").json()
cards = {k["pattern"]: k for k in pr["cards"]}
print("practice cards:", list(cards), pr["stats"])
assert "third person -s" in cards and cards["third person -s"]["box"] == 0
card = cards["third person -s"]

def attempt(said, grade=1, day="2026-11-08"):
    queue.append((said, []))
    r = c.post("/api/attempt", files={"audio": ("a.wav", wav(), "audio/wav")},
               data={"target": card["target"], "must": card["correction"], "pattern": card["pattern"],
                     "grade": str(grade), "day": day}).json()
    queue.clear()
    return r

bad = attempt("um something totally different")
assert not bad["passed"] and bad["schedule"]["box"] == 1, bad
good = attempt(card["target"] + " " + card["correction"])  # fake target lacks the correction word
print("attempt:", good["similarity"], good["schedule"])
assert good["passed"] and good["schedule"]["box"] == 2 and good["schedule"]["due"] == "2026-11-11", good
assert "third person -s" not in {k["pattern"] for k in c.get("/api/practice?day=2026-11-08").json()["cards"]}
assert "third person -s" in {k["pattern"] for k in c.get("/api/practice?day=2026-11-11").json()["cards"]}

# ---- duration -> words per minute
queue.append(("I really enjoy cooking dinner for my family", []))
c.post("/api/analyze", files={"audio": ("s.wav", wav(), "audio/wav")}, data={"day": "2026-11-09", "duration_ms": "3000"})
assert c.get("/api/report?day=2026-11-09").json()["totals"]["today"]["wpm"] == 160

# ---- conversations: grouped, full text stored, suggestions stored, polish stored
convs = c.get("/api/conversations?day=2026-11-08").json()
assert len(convs) == 1 and len(convs[0]["sentences"]) == 2, convs
s0 = convs[0]["sentences"][0]
assert s0["corrected"].endswith("(fixed)") and s0["better"].startswith("Better:")
assert c.get("/api/day/2026-11-08").json()[0]["next_suggestions"] == ["Then I went home."]
pol = c.post(f"/api/conversation/{convs[0]['id']}/polish").json()
assert c.get("/api/conversations?day=2026-11-08").json()[0]["polished"] == pol
queue.append(("Now I am starting a new talk", []))
j = c.post("/api/analyze", files={"audio": ("s.wav", wav(), "audio/wav")},
           data={"day": "2026-11-08", "conversation_id": str(convs[0]["id"]), "new_conversation": "1"}).json()
assert j["conversation_id"] != convs[0]["id"] and len(c.get("/api/conversations?day=2026-11-08").json()) == 2
queue.append(("And I keep talking in it", []))
j2 = c.post("/api/analyze", files={"audio": ("s.wav", wav(), "audio/wav")},
            data={"day": "2026-11-08", "conversation_id": str(j["conversation_id"])}).json()
assert j2["conversation_id"] == j["conversation_id"]

# ---- live suggestions while speaking
lv = c.post("/api/live", json={"text": "yesterday i go to", "context": ["hello"]}).json()
assert lv["fixes"][0]["right"] == "i went" and lv["continuations"], lv
assert c.post("/api/live", json={"text": "hi"}).json() == {"fixes": [], "continuations": []}

# ---- AI voice: generated once, then served from the cache
import shutil
main.TTS_CACHE = main.ROOT / "data" / "_test_tts_cache"
shutil.rmtree(main.TTS_CACHE, ignore_errors=True)
r1 = c.post("/api/tts", json={"text": "He goes to work."})
r2 = c.post("/api/tts", json={"text": "He goes to work."})
assert r1.status_code == 200 and r1.headers["x-tts-model"] == "fake-tts" and r2.headers["x-tts-model"] == "cache"
assert main._coach.spoken == ["He goes to work."]
shutil.rmtree(main.TTS_CACHE, ignore_errors=True)

# ---- speech-to-text falls back to the second model when the first fails
from app.coach import Coach, is_transcription_model, is_chat_audio_model
assert is_transcription_model("openai/gpt-4o-mini-transcribe") and not is_transcription_model("openai/gpt-audio-mini")
assert is_chat_audio_model("openai/gpt-audio-mini") and not is_chat_audio_model("hexgrad/kokoro-82m")
co = Coach.__new__(Coach)
co.transcribe_model, co.transcribe_fallback = "primary", "backup"
def fake_with(model, wav):
    if model == "primary": raise RuntimeError("model not available")
    return "he go home"
co._transcribe_with = fake_with
assert co.transcribe(b"x") == "he go home" and co.last_stt_model == "backup"

# ---- provider errors come back as readable JSON
import openai, types
class Expired(FakeCoach):
    def daily_plan(self, r):
        resp = types.SimpleNamespace(status_code=401, request=None, headers={})
        raise openai.AuthenticationError("API key expired", response=resp, body=None)
main._coach = Expired()
r = c.post("/api/report/plan?day=2026-11-08")
assert r.status_code == 502 and "API key" in r.json()["detail"], r.text
print("ALL OK  db:", os.environ["DB_PATH"])
