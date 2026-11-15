"""End to end over the WebSocket with every AI call faked: live session, practice session, summary, REST."""
import io
import struct
import wave

from fastapi.testclient import TestClient

from app.analysis import live as live_mod
from app.api import routes, ws as ws_mod
from app.audio import stt as stt_mod
from app.main import app
from app.patterns import engine
from app.summary import summarize


def wav(seconds=1.0, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    return buf.getvalue()


class ScriptedSTT:
    name, streaming = "fake", False

    def __init__(self, texts):
        self.texts = list(texts)

    def transcribe(self, data):
        return self.texts.pop(0)


def fake_analysis(text, context, known_rules, focus):
    mistakes = [{"original": "go", "corrected": "went", "category": "tense", "rule": "past simple",
                 "explanation": "Finished action."}] if " go " in f" {text} " else []
    return {"is_directed_speech": True, "corrected": text.replace(" go ", " went "), "mistakes": mistakes,
            "suggestions": {"better_phrasing": "", "vocabulary": [], "next_sentences": ["Then I went home."]}}


def setup(monkeypatch, texts):
    stt_mod.set_stt(ScriptedSTT(texts))
    monkeypatch.setattr(ws_mod, "analyse_sentence", fake_analysis)
    monkeypatch.setattr(routes, "analyse_sentence", fake_analysis)
    monkeypatch.setattr(ws_mod.speaker, "classify", lambda w: ("me", None))
    monkeypatch.setattr(live_mod, "json_call", lambda **kw: {"fixes": [{"wrong": "i go", "right": "i went"}],
                                                             "continuations": ["I went home."]})
    monkeypatch.setattr(summarize, "json_call", lambda **kw: {
        "title": " Market trip. ", "corrected_paragraph": "Yesterday I went to the market. Thank you.",
        "key_points": ["Past simple: went"]})
    monkeypatch.setattr(engine, "maybe_run", lambda: None)


def collect(ws, until):
    msgs = []
    while True:
        m = ws.receive_json()
        msgs.append(m)
        if m["type"] == until:
            return msgs


def test_live_session_end_to_end(monkeypatch):
    setup(monkeypatch, ["Yesterday I go to the market", "Thank you.", "It was very busy"])
    c = TestClient(app)
    with c.websocket_connect("/ws/session") as ws:
        ws.send_json({"type": "start", "mode": "live"})
        sid = ws.receive_json()["session_id"]
        for _ in range(3):
            ws.send_json({"type": "segment", "duration_ms": 1500})
            ws.send_bytes(wav())
        ws.send_json({"type": "partial", "text": "and then i go"})
        ws.send_json({"type": "stop"})
        msgs = collect(ws, "summary")
    kinds = [m["type"] for m in msgs]
    utts = [m["utterance"]["text"] for m in msgs if m["type"] == "utterance"]
    assert utts == ["Yesterday I go to the market", "It was very busy"]      # "Thank you." filtered as noise
    assert "skipped" in kinds and "analysis" in kinds
    analysis = next(m for m in msgs if m["type"] == "analysis" and m["mistakes"])
    assert analysis["mistakes"][0]["corrected"] == "went" and analysis["mistakes"][0]["times_seen"] == 1
    summary = msgs[-1]["summary"]
    assert summary["id"] == sid and summary["corrected_paragraph"].startswith("Yesterday I went")
    assert summary["title"] == "Market trip" and summary["most_common"] == "tense"
    assert summary["transcript"] == "Yesterday I go to the market It was very busy"
    assert len(summary["all_mistakes"]) == 1

    # REST: list, detail, "not me" removes the line's mistakes, "it was me" analyses it again
    assert c.get("/api/sessions").json()[0]["mistakes"] == 1
    detail = c.get(f"/api/sessions/{sid}").json()
    uid = detail["utterances"][0]["id"]
    assert c.patch(f"/api/utterances/{uid}", json={"speaker": "other"}).json()["mistakes"] == []
    assert c.patch(f"/api/utterances/{uid}", json={"speaker": "me"}).json()["mistakes"][0]["rule"] == "past simple"


def test_practice_session_agent_replies(monkeypatch):
    setup(monkeypatch, ["I go to Paris last year"])
    replies = []

    def fake_reply(topic, steering, lines):
        replies.append((topic, [l["text"] for l in lines]))
        return {"text": f"Reply {len(replies)}", "audio": b"RIFF....", "mime": "audio/wav", "model": "fake"}

    monkeypatch.setattr(ws_mod.agent, "reply", fake_reply)
    c = TestClient(app)
    with c.websocket_connect("/ws/session") as ws:
        ws.send_json({"type": "start", "mode": "practice", "topic": "Travel"})
        first = collect(ws, "agent")                      # the partner opens the conversation
        assert first[-1]["utterance"]["speaker"] == "agent" and first[-1]["audio"]
        ws.send_json({"type": "segment", "duration_ms": 1500})
        ws.send_bytes(wav())
        second = collect(ws, "agent")
        ws.send_json({"type": "stop"})
        collect(ws, "summary")
    assert replies[0] == ("Travel", [])
    assert replies[1] == ("Travel", ["Reply 1", "I go to Paris last year"])
    assert second[-1]["utterance"]["text"] == "Reply 2"


def test_health_and_focus_endpoints(monkeypatch):
    c = TestClient(app)
    h = c.get("/api/health").json()
    assert h["ok"] and h["models"]["speech_to_text"][0].endswith("gpt-4o-mini-transcribe")
    f = c.get("/api/focus").json()
    assert f["run"] is None and f["total_mistakes"] == 0
    assert f["totals"] == {"mistakes": 0, "sessions": 0, "minutes": 0, "categories": []}
    assert c.post("/api/focus/analyse").status_code == 400   # not enough history yet
