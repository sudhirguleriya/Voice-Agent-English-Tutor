"""Practice mode: a one-to-one spoken conversation with an AI partner.

Each turn: the learner's speech is transcribed (same STT as live mode, and graded the same way), then the agent
replies WITH ITS OWN VOICE in one call (gpt-audio-mini is built for spoken conversation). Its instructions include
the learner's Focus Areas, so it steers the talk towards their weak spots. If the voice model fails, a fast text
model writes the reply and the TTS chain (or finally the browser) speaks it.
"""
import logging

from ..config import settings
from ..llm import client
from .tts import stream_chat_audio, synthesize

log = logging.getLogger("agent")

TOPICS = [
    "Small talk: my day so far", "My job and what I do at work", "A trip I enjoyed", "Plans for the weekend",
    "Food and cooking", "A film or series I watched", "My home town", "A job interview (you interview me)",
    "Ordering at a restaurant", "Talking about my family", "Technology I use every day", "A problem I solved recently",
]

MAX_REPLY_SECONDS = 25


def system_prompt(topic: str, steering: list[str], language: str = "") -> str:
    language = language or settings.language
    steer = "\n".join(f"- {s}" for s in steering)
    return f"""You are a warm, patient {language} conversation partner for a learner who is practising speaking.
Topic: {topic}.

How to talk:
- Speak naturally, like a friendly native speaker. Keep each reply SHORT: 1-3 sentences, then ask ONE open
  question so the learner talks more than you do.
- Use clear, everyday words at a moderate pace.
- Do not lecture about grammar. If the learner makes a mistake, naturally repeat the idea with the correct form in
  your reply (for example they say "I go there yesterday", you say "Oh, you went there yesterday? ...").
- Stay on the topic, but follow the learner if they change it.
""" + (f"""
The learner is working on these weak spots. Steer the conversation so they naturally have to use them,
one at a time, without mentioning that you are doing it:
{steer}
""" if steer else "")


def history_messages(lines: list[dict]) -> list[dict]:
    """Session lines -> chat history. Other people in the room are left out."""
    out = []
    for u in lines:
        if u["speaker"] == "me":
            out.append({"role": "user", "content": u["text"]})
        elif u["speaker"] == "agent":
            out.append({"role": "assistant", "content": u["text"]})
    return out[-20:]


def reply(topic: str, steering: list[str], lines: list[dict]) -> dict:
    """Next agent turn. Returns {"text", "audio" (bytes or None), "mime", "model"}.
    With no history yet, the agent opens the conversation."""
    messages = [{"role": "system", "content": system_prompt(topic, steering)}] + history_messages(lines)
    if len(messages) == 1:
        messages.append({"role": "user", "content": "(The learner has just joined. Greet them in one short "
                                                    "sentence and ask your first question about the topic.)"})
    if settings.agent_model:
        try:
            wav, said = stream_chat_audio(settings.agent_model, settings.agent_voice, messages, MAX_REPLY_SECONDS)
            if said.strip():
                return {"text": said, "audio": wav, "mime": "audio/wav", "model": settings.agent_model}
        except Exception as e:
            log.warning("voice agent %s failed, using text fallback: %s", settings.agent_model, e)
    # fallback: text reply, then the TTS chain (or the browser voice if that fails too)
    r = client().chat.completions.create(model=settings.agent_text_fallback, messages=messages, max_tokens=160)
    text = " ".join((r.choices[0].message.content or "").split())
    try:
        audio, mime, model = synthesize(text)
    except Exception as e:
        log.warning("agent TTS failed, browser will speak: %s", e)
        audio, mime, model = None, None, settings.agent_text_fallback
    return {"text": text, "audio": audio, "mime": mime, "model": model}
