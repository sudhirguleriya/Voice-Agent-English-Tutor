"""WebSocket: one connection per listening session.

Client -> server
    {"type": "start", "mode": "live"|"practice", "topic": "..."}       first message
    {"type": "segment", "duration_ms": 1830}  + next frame: WAV bytes   one sentence, cut at a pause
    {"type": "partial", "text": "yesterday me and my"}                   browser's instant recognition (debounced)
    {"type": "stop"}

Server -> client
    {"type": "session", "session_id", "mode", "topic"}
    {"type": "utterance", "utterance": {...}}        a transcript line (speaker: me | other | agent)
    {"type": "analysis", "utterance_id", "corrected", "mistakes": [...], "suggestions": {...}}
    {"type": "hints", "partial", "fixes": [...], "continuations": [...]}
    {"type": "agent", "utterance": {...}, "audio": base64 | null, "mime"}
    {"type": "status", "state": "transcribing" | "agent_thinking" | "summarising" | "idle"}
    {"type": "skipped", "reason"}                    noise / too short / not speech
    {"type": "summary", "summary": {...}}
    {"type": "error", "message"}                     an AI step failed; the session keeps going

Segments are processed one at a time in order. Analysis of a sentence runs in the background so the next
sentence is transcribed without waiting. AI calls are blocking SDK calls, so they run in worker threads.
"""
import asyncio
import base64
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..analysis.live import analyse_sentence, hints
from ..audio import speaker
from ..audio.stt import get_stt, is_noise
from ..llm import friendly
from ..patterns import engine as patterns
from ..practice_agent import agent
from ..storage import repo
from ..summary.summarize import build_summary

log = logging.getLogger("ws")
router = APIRouter()
MIN_SEGMENT_BYTES = 8000  # < 0.25 s of 16 kHz audio


class LiveSession:
    def __init__(self, ws: WebSocket, session_id: int, mode: str, topic: str | None):
        self.ws, self.session_id, self.mode, self.topic = ws, session_id, mode, topic
        self.segments: asyncio.Queue = asyncio.Queue()
        self.analysis_tasks: set[asyncio.Task] = set()
        self.hint_task: asyncio.Task | None = None
        self.worker_task: asyncio.Task = asyncio.create_task(self.worker())
        self.closed = False

    async def send(self, msg: dict):
        if not self.closed:
            try:
                await self.ws.send_text(json.dumps(msg))
            except Exception:
                self.closed = True

    async def error(self, what: str, e: Exception):
        log.warning("%s failed: %s", what, e)
        await self.send({"type": "error", "message": f"{what}: {friendly(e)}"})

    # ------------------------------------------------------------ one sentence
    async def worker(self):
        while True:
            item = await self.segments.get()
            if item is None:
                return
            wav, duration = item
            try:
                await self.handle_segment(wav, duration)
            except Exception as e:  # never let one bad segment end the session
                await self.error("Processing a sentence", e)

    async def handle_segment(self, wav: bytes, duration_ms: int):
        await self.send({"type": "status", "state": "transcribing"})
        stt = get_stt()
        # transcription and voice match run in parallel
        text_t = asyncio.to_thread(stt.transcribe, wav)
        who_t = asyncio.to_thread(speaker.classify, wav)
        try:
            text, (who, score) = await asyncio.gather(text_t, who_t)
        except Exception as e:
            await self.error("Speech to text", e)
            return
        if is_noise(text):
            await self.send({"type": "skipped", "reason": "no clear speech", "text": text})
            await self.send({"type": "status", "state": "idle"})
            return
        context = repo.recent_texts(self.session_id)
        utt = repo.add_utterance(self.session_id, who, text, duration_ms, score)
        await self.send({"type": "utterance", "utterance": utt})
        if who != "me":
            await self.send({"type": "status", "state": "idle"})
            return  # someone else in the room: shown, not graded
        task = asyncio.create_task(self.analyse(utt, context))
        self.analysis_tasks.add(task)
        task.add_done_callback(self.analysis_tasks.discard)
        # practice mode: the agent answers once the learner has finished (no more queued sentences)
        if self.mode == "practice" and self.segments.empty():
            await self.agent_turn()
        else:
            await self.send({"type": "status", "state": "idle"})

    async def analyse(self, utt: dict, context: list[str]):
        try:
            result = await asyncio.to_thread(analyse_sentence, utt["text"], context, repo.known_rules(),
                                             patterns.focus_titles())
        except Exception as e:
            await self.error("Checking your sentence", e)
            return
        if not result["is_directed_speech"]:
            result["mistakes"] = []
        saved = repo.save_analysis(utt["id"], result["corrected"] or utt["text"], result["mistakes"],
                                   result["suggestions"])
        seen = repo.rule_counts([m["rule"] for m in saved["mistakes"]])
        for m in saved["mistakes"]:
            m["times_seen"] = seen.get(m["rule"], 1)
        await self.send({"type": "analysis", "utterance_id": utt["id"], "corrected": saved["corrected"],
                         "mistakes": saved["mistakes"], "suggestions": saved["suggestions"]})

    # ------------------------------------------------------------ practice agent
    async def agent_turn(self):
        await self.send({"type": "status", "state": "agent_thinking"})
        lines = (repo.get_session(self.session_id) or {}).get("utterances", [])
        try:
            r = await asyncio.to_thread(agent.reply, self.topic or "free conversation",
                                        patterns.agent_steering(), lines)
        except Exception as e:
            await self.error("The practice partner", e)
            await self.send({"type": "status", "state": "idle"})
            return
        utt = repo.add_utterance(self.session_id, "agent", r["text"])
        await self.send({"type": "agent", "utterance": utt, "mime": r["mime"],
                         "audio": base64.b64encode(r["audio"]).decode() if r["audio"] else None})
        await self.send({"type": "status", "state": "idle"})

    # ------------------------------------------------------------ hints while speaking
    async def on_partial(self, text: str):
        if self.hint_task and not self.hint_task.done():
            self.hint_task.cancel()  # only the latest partial matters
        self.hint_task = asyncio.create_task(self._hints(text))

    async def _hints(self, text: str):
        try:
            h = await asyncio.to_thread(hints, text, repo.recent_texts(self.session_id, 3))
            await self.send({"type": "hints", "partial": text, **h})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.info("hints failed: %s", e)  # optional feature: stay quiet

    # ------------------------------------------------------------ stop
    async def finish(self, send_summary: bool):
        await self.segments.put(None)
        if self.worker_task:
            await asyncio.gather(self.worker_task, return_exceptions=True)  # finish queued sentences first
        if self.hint_task and not self.hint_task.done():
            self.hint_task.cancel()
        if self.analysis_tasks:
            await asyncio.gather(*self.analysis_tasks, return_exceptions=True)
        repo.end_session(self.session_id)
        if send_summary:
            await self.send({"type": "status", "state": "summarising"})
            try:
                summary = await asyncio.to_thread(build_summary, self.session_id)
                await self.send({"type": "summary", "summary": summary})
            except Exception as e:
                await self.error("Writing the summary", e)
        asyncio.get_running_loop().run_in_executor(None, patterns.maybe_run)  # background, never blocks


@router.websocket("/ws/session")
async def session_socket(ws: WebSocket):
    await ws.accept()
    sess: LiveSession | None = None
    pending_segment: dict | None = None
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:  # audio frame belonging to the last "segment" header
                if sess and pending_segment is not None and len(msg["bytes"]) >= MIN_SEGMENT_BYTES:
                    await sess.segments.put((msg["bytes"], int(pending_segment.get("duration_ms") or 0)))
                pending_segment = None
                continue
            data = json.loads(msg.get("text") or "{}")
            kind = data.get("type")
            if kind == "start" and not sess:
                mode = "practice" if data.get("mode") == "practice" else "live"
                topic = (data.get("topic") or "").strip()[:200] or None
                sess = LiveSession(ws, repo.create_session(mode, topic), mode, topic)
                await sess.send({"type": "session", "session_id": sess.session_id, "mode": mode, "topic": topic})
                if mode == "practice":
                    await sess.agent_turn()  # the partner opens the conversation
            elif kind == "segment" and sess:
                pending_segment = data
            elif kind == "partial" and sess and data.get("text"):
                await sess.on_partial(str(data["text"])[:400])
            elif kind == "stop" and sess:
                await sess.finish(send_summary=True)
                await sess.send({"type": "status", "state": "idle"})
                sess = None
                break
    except WebSocketDisconnect:
        pass
    finally:
        if sess:  # tab closed without Stop: keep what was said, summary is built when the session is opened
            sess.closed = True
            await sess.finish(send_summary=False)
