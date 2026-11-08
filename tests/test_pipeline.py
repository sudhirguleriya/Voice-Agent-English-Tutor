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
    def analyze(self, text, known):
        _, pats = queue.pop(0)
        return {"is_speech": True, "corrected": text + " (fixed)", "better_version": "Better: " + text, "score": 70,
                "tip": "Keep going", "pronunciation_suspects": [],
                "mistakes": [{"category": "grammar", "original": "go", "correction": "goes", "explanation": "x", "pattern": p} for p in pats],
                "vocabulary_upgrades": [{"word": "good", "alternatives": ["productive", "rewarding"], "example": "It was a productive day"}]}
    def pronunciation(self, wav, text): return [{"word": "vegetable", "heard_as": "veg-e-table", "tip": "VEJ-tuh-bul"}]
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
print("ALL OK  db:", os.environ["DB_PATH"])
