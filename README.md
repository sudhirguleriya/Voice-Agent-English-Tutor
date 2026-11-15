# Voice Coach

An always-on English speaking coach that runs on your laptop. It listens while you practise alone, while you
talk with an AI voice partner, or while you talk to real people. It helps you in real time and over time.

- **Live**: one button (or <kbd>Space</kbd>) starts listening. You see a live transcript, a **Suggestions** panel
  (better phrasing, stronger words, what you could say next) and a **Mistakes** panel (each mistake with a short
  fix). While you are still mid-sentence, quick hints appear too.
- **Practice**: pick a topic and talk with an AI partner who speaks back. It keeps its replies short, naturally
  repeats your mistakes back in the correct form, and steers the talk towards your weak spots.
- **Summary** when you press Stop: your full transcript, a clean corrected paragraph ("what you should have
  said"), the key lessons, and every mistake (original, corrected, type, explanation).
- **My Focus Areas**: the pattern engine finds your recurring mistakes across all sessions, shows examples
  from your own speech and short drills, and tracks progress (mistakes per 100 words, fillers, speaking speed).
- **Only your voice is graded**: record a 20-second voice sample once (⚙ Settings). Other people in the room
  are shown greyed out and are not checked. This runs locally.

## Setup (Windows, macOS, Linux)

Needs Python 3.11+ and Chrome or Edge.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (PowerShell: .\.venv\Scripts\Activate.ps1)
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
```

Put your key in `.env` (`OPENROUTER_API_KEY=...` or `OPENAI_API_KEY=...`), then:

```bash
uvicorn app.main:app --port 8000
```

Open **http://localhost:8000**, press **Start listening**, and allow the microphone. Browsers only allow the mic
on `localhost` or HTTPS.

Tests (no key or network needed; every AI call is faked):

```bash
python -m pytest -q
```

## How it works

```
browser (frontend/)                                 server (app/)
  mic → voice-activity detection ── WAV per sentence ──►  /ws/session ─► audio/stt     (speech to text)
  browser recognition (instant partial words) ─ partial ─►               audio/speaker  (is it me?)
                                                                         analysis/live  (mistakes + suggestions)
  transcript · Suggestions · Mistakes  ◄────── JSON events ──────────    practice_agent (partner reply + voice)
                                                                         summary        (on Stop)
                                                                         patterns       (after sessions / on demand)
                                                                         storage        (SQLite via SQLAlchemy)
```

- **Live = one sentence at a time.** The browser cuts audio at each pause (adjusts to room noise) and sends
  that sentence over the WebSocket. The server transcribes it (about 1 s) and runs one structured-JSON analysis
  for both panels, so it stays fast and cheap. Silence is never sent.
- **Two transcripts.** The words you see while you talk come from the browser's own recognizer (instant, free).
  The text that is stored and graded comes from the speech-to-text model, because browser recognition quietly
  fixes grammar and would hide your mistakes.
- **Streaming STT.** The configured models are request/response per sentence. A true streaming provider can be
  added by implementing `STTProvider` in `app/audio/stt.py`; nothing else changes.
- **Safety nets.** A chat model used for speech to text sometimes answers you instead of transcribing; such
  replies are detected and rejected. A transcript in the wrong alphabet (a model that translated) is retried.
  Known "phantom" phrases from silence ("Thank you.") are dropped.

## Modules

| Module | Responsibility |
|---|---|
| `app/audio/stt.py` | swappable speech-to-text providers, fallback chain, guards |
| `app/audio/speaker.py` | local voice match (sherpa-onnx, WeSpeaker CAM++ model, about 28 MB, downloaded on first use) |
| `app/analysis/` | per-sentence mistakes + suggestions (strict JSON schema), live hints, output validation |
| `app/summary/` | end-of-session corrected paragraph and key points |
| `app/patterns/` | pattern engine (aggregate → LLM → Focus Areas + drills + agent hints), progress numbers |
| `app/practice_agent/` | AI conversation partner (speaks with its own voice), topics, text to speech + cache |
| `app/storage/` | SQLAlchemy models (sessions, utterances, mistakes, focus areas, drills, voice profile) |
| `app/api/` | WebSocket session protocol (`ws.py`, documented at the top) and REST routes |
| `frontend/` | plain HTML + ES-module JavaScript, no build step |

## Models (defaults, all configurable in `.env`)

| Job | Default (OpenRouter) | Why |
|---|---|---|
| Speech to text | `openai/gpt-4o-mini-transcribe` → `openai/gpt-audio-mini` | keeps your mistakes and fillers word for word, about 0.9 s |
| Mistakes + suggestions, summary, patterns | `openai/gpt-5-mini` (low reasoning effort) | reliable structured JSON |
| Hints while you speak | `google/gemini-2.5-flash-lite` | about 0.7 s |
| Practice partner | `openai/gpt-audio-mini` (voice "alloy") | one call returns the reply already spoken |
| Speaker buttons | `openai/gpt-audio-mini` → `fish-audio/s2-pro` → browser voice | clips are cached in `data/tts_cache` |

Data lives in `data/voice_coach.db` (SQLite). Nothing is uploaded except the audio and text sent to the AI
provider you configured.

## Cost

Each sentence costs one transcription and one analysis call; hints add a few tiny calls while you speak (turn
off with `HINT_MODEL=`). Practice adds one voice reply per turn. The pattern engine runs at most about once a
day automatically, or when you press **Analyse my history**.
