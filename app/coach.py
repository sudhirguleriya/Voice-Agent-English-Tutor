"""OpenAI calls: verbatim transcription, language coaching, pronunciation check."""
import base64
import io
import json
import logging
import os
import re
import wave

from openai import OpenAI

log = logging.getLogger("coach")

CATEGORIES = [
    "grammar", "tense", "article", "preposition", "subject_verb_agreement",
    "word_choice", "vocabulary", "sentence_structure", "filler", "repetition",
    "pronunciation",
]

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_speech", "corrected", "better_version", "mistakes",
                 "vocabulary_upgrades", "pronunciation_suspects", "score", "tip", "next_suggestions"],
    "properties": {
        "is_speech": {"type": "boolean",
                      "description": "false if the transcript is noise, a TV/other person, or meaningless"},
        "corrected": {"type": "string",
                      "description": "Minimal edit: the same sentence with only the errors fixed"},
        "better_version": {"type": "string",
                           "description": "How a fluent native speaker would naturally say the same thing"},
        "mistakes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["category", "original", "correction", "explanation", "pattern"],
            "properties": {
                "category": {"type": "string", "enum": CATEGORIES},
                "original": {"type": "string"},
                "correction": {"type": "string"},
                "explanation": {"type": "string", "description": "One short sentence, simple words"},
                "pattern": {"type": "string",
                            "description": "Short reusable rule name, e.g. 'third person -s (he go -> he goes)'"},
            }}},
        "vocabulary_upgrades": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["word", "alternatives", "example"],
            "properties": {
                "word": {"type": "string", "description": "Plain/overused word or phrase the user said"},
                "alternatives": {"type": "array", "items": {"type": "string"}},
                "example": {"type": "string", "description": "The user's sentence rewritten with an alternative"},
            }}},
        "pronunciation_suspects": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["word", "heard_as", "tip"],
            "properties": {
                "word": {"type": "string", "description": "The word the user most likely meant"},
                "heard_as": {"type": "string", "description": "What the transcriber heard instead"},
                "tip": {"type": "string"},
            }}},
        "score": {"type": "integer", "description": "0-100 overall quality of this utterance"},
        "tip": {"type": "string", "description": "One encouraging, actionable coaching tip"},
        "next_suggestions": {"type": "array", "items": {"type": "string"},
                             "description": "2-3 sentences the user could say NEXT to continue the same topic"},
    },
}

LIVE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["fixes", "continuations"],
    "properties": {
        "fixes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["wrong", "right"],
            "properties": {"wrong": {"type": "string"}, "right": {"type": "string"}}}},
        "continuations": {"type": "array", "items": {"type": "string"}},
    },
}

POLISH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["polished", "key_changes"],
    "properties": {
        "polished": {"type": "string", "description": "The whole conversation retold as natural, fluent speech"},
        "key_changes": {"type": "array", "items": {"type": "string"},
                        "description": "3-5 most useful improvements, each 'you said X -> say Y (why)'"},
    },
}


class Coach:
    def __init__(self):
        or_key = os.getenv("OPENROUTER_API_KEY")
        self.openrouter = bool(or_key) and not os.getenv("OPENAI_API_KEY")
        if self.openrouter:
            self.client = OpenAI(api_key=or_key, base_url=os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"))
            d_coach, d_pron, d_live = "openai/gpt-5-mini", "google/gemini-2.5-flash", "google/gemini-2.5-flash-lite"
            # one model listens and speaks; each job has its own fallback
            d_tr, d_tr_fb = "openai/gpt-audio-mini", "openai/gpt-4o-mini-transcribe"
            d_tts, d_tts_voice, d_tts_fb, d_tts_fb_voice = "openai/gpt-audio-mini", "alloy", "fish-audio/s2-pro", ""
        else:
            self.client = OpenAI()  # reads OPENAI_API_KEY (and OPENAI_BASE_URL if set)
            d_coach, d_pron, d_live = "gpt-5-mini", "gpt-4o-audio-preview", "gpt-4.1-nano"
            d_tr, d_tr_fb = "gpt-audio-mini", "gpt-4o-mini-transcribe"
            d_tts, d_tts_voice, d_tts_fb, d_tts_fb_voice = "gpt-audio-mini", "alloy", "gpt-4o-mini-tts", "alloy"
        env = lambda k, d: (d if os.getenv(k) is None else os.getenv(k)).strip()  # set to empty = turned off
        self.live_model = env("LIVE_MODEL", d_live)  # fast model for suggestions while speaking
        self.transcribe_model = os.getenv("TRANSCRIBE_MODEL") or d_tr
        self.transcribe_fallback = env("TRANSCRIBE_FALLBACK", d_tr_fb)
        self.tts_model, self.tts_voice = env("TTS_MODEL", d_tts), env("TTS_VOICE", d_tts_voice)
        self.tts_fallback, self.tts_fallback_voice = env("TTS_FALLBACK", d_tts_fb), env("TTS_FALLBACK_VOICE", d_tts_fb_voice)
        self.coach_model = os.getenv("COACH_MODEL") or d_coach
        self.pron_model = env("PRONUNCIATION_MODEL", d_pron)
        self.language = os.getenv("TARGET_LANGUAGE", "English")
        # no retries on the audio path: if a model fails we move straight to its fallback
        self.fast = self.client.with_options(max_retries=0, timeout=25)
        self.last_stt_model = None

    # ---------- 1. speech to text (primary, then fallback) ----------
    def transcribe(self, wav: bytes) -> str:
        err = None
        for model in (self.transcribe_model, self.transcribe_fallback):
            if not model:
                continue
            try:
                text = self._transcribe_with(model, wav)
                self.last_stt_model = model
                return text
            except Exception as e:  # unavailable, timeout, provider error -> try the next model
                log.warning("speech-to-text with %s failed: %s", model, e)
                err = e
        raise err or RuntimeError("No speech-to-text model configured")

    def _transcribe_with(self, model: str, wav: bytes) -> str:
        instruction = (f"Transcribe this {self.language} speech verbatim. Keep grammatical mistakes, wrong words, "
                       "filler words (um, uh, like, you know), false starts and repetitions exactly as spoken. "
                       "Do NOT correct anything. Output only the transcript text. If there is no speech, output nothing.")
        if not is_transcription_model(model):  # audio-input chat model (gpt-audio-mini, gemini...)
            r = self.fast.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": instruction},
                    {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}},
                ]}],
            )
            return (r.choices[0].message.content or "").strip().strip('"')
        f = io.BytesIO(wav)
        f.name = "speech.wav"
        r = self.fast.audio.transcriptions.create(
            model=model, file=f, language=_lang_code(self.language),
            prompt=("Transcribe exactly what is said, verbatim. Keep grammatical mistakes, wrong words, "
                    "filler words (um, uh, like, you know), false starts and repetitions. Do NOT correct anything."),
        )
        return (r.text or "").strip()

    # ---------- text to speech (primary, then fallback; the browser voice is the last resort) ----------
    def speak(self, text: str) -> tuple[bytes, str, str]:
        """Returns (audio bytes, mime type, model used)."""
        errors = []
        for model, voice in ((self.tts_model, self.tts_voice), (self.tts_fallback, self.tts_fallback_voice)):
            if not model:
                continue
            try:
                if is_chat_audio_model(model):
                    try:
                        return self._speak_chat(model, voice, text, style=0), "audio/wav", model
                    except ValueError as e:  # it answered instead of reading: one retry with another prompt
                        log.info("retrying %s with a second prompt style: %s", model, e)
                        return self._speak_chat(model, voice, text, style=1), "audio/wav", model
                data = self._speak_endpoint(model, voice, text)
                if len(data) < 500:
                    raise ValueError("empty audio")
                return data, "audio/mpeg", model
            except Exception as e:
                log.warning("text-to-speech with %s failed: %s", model, e)
                errors.append(f"{model}: {e}")
        raise RuntimeError("; ".join(errors) or "No text-to-speech model configured")

    def _speak_endpoint(self, model: str, voice: str, text: str) -> bytes:
        """Dedicated TTS model via /audio/speech. Sent directly because some models (Fish Audio) work best
        with no voice at all, while the SDK always requires one."""
        import urllib.error
        import urllib.request
        body = {"model": model, "input": text, "response_format": "mp3"}
        if voice:
            body["voice"] = voice
        req = urllib.request.Request(
            str(self.client.base_url).rstrip("/") + "/audio/speech", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.client.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{e.code} {e.read().decode(errors='replace')[:200]}") from None

    def _speak_chat(self, model: str, voice: str, text: str, style: int = 0) -> bytes:
        """Audio-output chat model (gpt-audio-mini). It is a chat model, so for requests like "tell me a joke"
        it sometimes ANSWERS instead of reading. We check its transcript and reject any clip that isn't the text."""
        if style == 0:
            messages = [{"role": "system", "content": (
                "You are a text-to-speech voice. You never converse. You only read the script you are given "
                "aloud, exactly, word for word, in a clear, natural, friendly voice, then stop.")},
                {"role": "user", "content": f'Script:\n"""\n{text}\n"""'}]
        else:
            messages = [{"role": "user", "content": f'Say exactly this and nothing else, then stop: "{text}"'}]
        # NB: no temperature=0 here; audio-output models can loop endlessly at very low temperature
        stream = self.fast.chat.completions.create(
            model=model, modalities=["text", "audio"], audio={"voice": voice or "alloy", "format": "pcm16"},
            stream=True, messages=messages,
        )
        # safety stop: a sentence never needs more than ~0.7 s per word (+3 s); 24 kHz * 2 bytes = 48000 B/s
        max_bytes = int((3 + 0.7 * len(text.split())) * 48000)
        pcm, said = bytearray(), []
        for ch in stream:
            if len(pcm) > max_bytes:
                stream.close()
                raise ValueError("audio ran far longer than the sentence")
            a = getattr(ch.choices[0].delta, "audio", None) if ch.choices else None
            if not a:
                continue
            get = (lambda k: a.get(k)) if isinstance(a, dict) else (lambda k: getattr(a, k, None))
            if get("data"):
                pcm += base64.b64decode(get("data"))
            if get("transcript"):
                said.append(get("transcript"))
        if len(pcm) < 4800:
            raise ValueError("no audio returned")
        if said and _similar(" ".join(said), text) < 0.85:
            raise ValueError(f"model did not read the text verbatim (said: {' '.join(said)[:80]!r})")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(bytes(pcm))
        return buf.getvalue()

    # ---------- 2. grammar / phrasing / vocabulary ----------
    def analyze(self, text: str, known_patterns: list[dict], context: list[str] | None = None) -> dict:
        patterns = "\n".join(f"- {p['pattern']}" for p in known_patterns) or "(none yet)"
        # stored keys are rule names only; the "(wrong -> right)" part the model adds is dropped on save
        system = f"""You are a friendly but precise {self.language} speaking coach. The user is speaking out loud;
you receive a verbatim speech transcript (no punctuation errors should be counted — it is speech).

Find every real mistake: grammar, tenses, articles, prepositions, subject-verb agreement, wrong word choice,
awkward sentence structure, filler words (um, like, basically, you know...) and needless repetition.
Do not flag acceptable informal spoken style. If the sentence is already correct, return no mistakes.

- corrected: minimal fix of the user's sentence.
- better_version: a more natural, fluent way to say it (better vocabulary, idiomatic).
- vocabulary_upgrades: plain or overused words (good, nice, very big, thing, do...) with stronger alternatives.
- pronunciation_suspects: speech recognition sometimes writes a wrong word when the speaker mispronounced the
  intended one (e.g. "I will sheet here" -> sit; "I live in the village" fine). Only list cases where a word
  clearly does not fit the context and sounds like a likely intended word.
- pattern: names the grammar RULE so the SAME kind of mistake always gets the SAME name. Format:
  "<rule in a few words> (<typical wrong> -> <right>)", lowercase. Two format examples (do not use these names
  unless the mistake really is that rule): "third person -s (he go -> he goes; he don't -> he doesn't)",
  "filler word um". Never use vague names such as "verb choice", "word choice", "grammar", "article", "tense",
  "preposition": name the actual rule (e.g. "agree is a verb, not an adjective (i am agree -> i agree)",
  "stative verbs not in -ing (he is liking -> he likes)"). Not tied to this one sentence.
  EXISTING rule names from the user's history are listed below. If the mistake breaks one of these exact rules,
  use that name exactly (this is how repeated mistakes are tracked). Otherwise make a new name.
  Never reuse a name for a different rule:
{patterns}
- next_suggestions: 2-3 short sentences (under 15 words each) the user could naturally say NEXT, continuing
  the same topic and the conversation so far, in first person, correct and fluent, everyday words slightly
  above the user's level. Where natural, make them use the CORRECT form of the rules above that the user
  often breaks (e.g. past tense, articles), so they get practice saying them right. No questions to the coach.
"""
        ctx = "\n".join(f"- {c}" for c in (context or [])[-4:])
        user = (f"Conversation so far (earlier sentences, for context only):\n{ctx}\n\n" if ctx else "") + \
            f"Transcript to analyse: \"{text}\""
        r = self.client.chat.completions.create(
            model=self.coach_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "speech_feedback", "strict": True, "schema": ANALYSIS_SCHEMA}},
        )
        return json.loads(r.choices[0].message.content)

    # ---------- 3. pronunciation from the actual audio ----------
    def pronunciation(self, wav: bytes, text: str) -> list[dict]:
        if not self.pron_model:
            return []
        prompt = (
            f"You are a {self.language} pronunciation coach. Listen to the recording. The transcript is: \"{text}\".\n"
            "List only words that were clearly mispronounced (wrong vowel/consonant, wrong stress, dropped sounds). "
            "Ignore accent differences that do not hurt understanding. Do NOT list a word that was said correctly, "
            "and do NOT list grammar mistakes (wrong word or wrong verb form) — only sound problems. Most sentences "
            "have no pronunciation problems; when unsure, leave it out. Reply with ONLY JSON: "
            '{"issues":[{"word":"...","heard_as":"how it sounded, e.g. veg-E-table",'
            '"tip":"how to say it correctly, with stress in CAPS","confidence":"high|medium|low"}]} '
            '— or {"issues":[]} if pronunciation was fine.'
        )
        r = self.client.chat.completions.create(
            model=self.pron_model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}},
            ]}],
        )
        return _parse_issues(r.choices[0].message.content or "")

    # ---------- live help while the user is still speaking ----------
    def live_suggest(self, partial: str, context: list[str]) -> dict:
        if not self.live_model:
            return {"fixes": [], "continuations": [], "disabled": True}
        ctx = " ".join(context[-3:])
        r = self.client.chat.completions.create(
            model=self.live_model,
            temperature=0.4,
            max_tokens=220,
            messages=[
                {"role": "system", "content": (
                    f"You help a {self.language} learner WHILE they are speaking. You get the words of the sentence "
                    "they are saying right now (live speech recognition: unfinished, no punctuation, may have small "
                    "recognition errors). Reply fast and short.\n"
                    "- fixes: at most 2 real grammar or word mistakes in the words so far, as the short wrong "
                    "phrase and its correct form. Check especially: tense ('yesterday we go' -> 'we went'), "
                    "present perfect with since/for ('i am working here since' -> 'i have been working here "
                    "since'), word order and pronouns ('me and my wife we' -> 'my wife and i'), articles, "
                    "prepositions, third person -s. Ignore missing punctuation, capital letters, the unfinished "
                    "ending and fillers. Empty list if it is correct so far.\n"
                    "- continuations: 3 different natural ways to FINISH the sentence they are in the middle of. "
                    "Each is a complete sentence under 20 words that begins with THEIR words so far but with "
                    "EVERY mistake fixed (apply all fixes; never repeat a mistake), properly capitalised. "
                    "Simple everyday words. If what they said is already a complete sentence, give 3 short "
                    "natural NEXT sentences instead.")},
                {"role": "user", "content": (f"Earlier: {ctx}\n" if ctx else "") + f"Saying now: {partial}"},
            ],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "live_help", "strict": True, "schema": LIVE_SCHEMA}},
        )
        content = r.choices[0].message.content or "{}"
        m = re.search(r"\{.*\}", content, re.S)
        try:
            d = json.loads(m.group(0) if m else content)
        except json.JSONDecodeError:
            d = {}
        fixes = [f for f in d.get("fixes") or [] if isinstance(f, dict) and f.get("wrong") and f.get("right")
                 and f["wrong"].strip().lower() != f["right"].strip().lower()][:2]
        conts = [str(s).strip() for s in d.get("continuations") or [] if str(s).strip()][:3]
        return {"fixes": fixes, "continuations": conts}

    # ---------- whole-conversation rewrite ----------
    def polish_conversation(self, sentences: list[str]) -> dict:
        r = self.client.chat.completions.create(
            model=self.coach_model,
            messages=[
                {"role": "system", "content": (
                    f"You are a {self.language} speaking coach. The user spoke the sentences below out loud, in "
                    "order (verbatim speech transcript, may contain mistakes, fillers and repeats). Retell the "
                    "WHOLE thing as one natural, fluent spoken version a native speaker would say: same meaning, "
                    "same order, first person, keep their ideas and facts, remove fillers and repetition, fix "
                    "grammar, use natural linking words (then, after that, so, but). Keep it about the same length. "
                    "Use paragraphs if the topic changes. Then list the 3-5 most useful improvements.")},
                {"role": "user", "content": "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sentences))},
            ],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "polish", "strict": True, "schema": POLISH_SCHEMA}},
        )
        return json.loads(r.choices[0].message.content)

    # ---------- daily coaching plan ----------
    def daily_plan(self, report: dict) -> dict:
        compact = {k: report[k] for k in ("day", "totals", "categories", "focus", "fillers",
                                          "pronunciation", "vocabulary")}
        compact["patterns"] = {k: v for k, v in report["patterns"].items() if k != "today_all"}
        r = self.client.chat.completions.create(
            model=self.coach_model,
            messages=[
                {"role": "system", "content": (
                    f"You are a {self.language} speaking coach writing a short daily review. Compare today with "
                    "yesterday using the data (mistakes per 100 words, fillers per 100 words, speaking speed in "
                    "words per minute - natural conversation is about 120-160), praise real improvement, and name "
                    "the top 3 recurring mistakes ('focus' lists them; explain each rule in one simple line). "
                    "Give concrete practice: 5 short drill sentences (under 14 words, everyday topics) the user "
                    "should say out loud that use the CORRECT form of their repeated mistakes, plus 3 words to "
                    "practise pronouncing (word - stress in CAPS) and 3 vocabulary words to start using "
                    "(word - short example). Be warm, specific and brief.")},
                {"role": "user", "content": json.dumps(compact)},
            ],
            response_format={"type": "json_schema", "json_schema": {"name": "daily_plan", "strict": True, "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["headline", "progress", "focus", "drills", "pronounce", "new_words"],
                "properties": {
                    "headline": {"type": "string"},
                    "progress": {"type": "string"},
                    "focus": {"type": "array", "items": {"type": "string"}},
                    "drills": {"type": "array", "items": {"type": "string"}},
                    "pronounce": {"type": "array", "items": {"type": "string"}},
                    "new_words": {"type": "array", "items": {"type": "string"}},
                }}}},
        )
        return json.loads(r.choices[0].message.content)


def is_transcription_model(model: str) -> bool:
    """Dedicated speech-to-text models use /audio/transcriptions; everything else gets audio in a chat request."""
    return bool(re.search(r"transcribe|whisper|scribe|asr|nova-|parakeet|chirp|stt", model.lower()))


def is_chat_audio_model(model: str) -> bool:
    """Chat models that can answer with audio (as opposed to dedicated /audio/speech TTS models)."""
    return bool(re.search(r"gpt-audio|audio-preview", model.lower()))


def _similar(a: str, b: str) -> float:
    """Letter-level match, so 'veget able' still equals 'vegetable'."""
    from difflib import SequenceMatcher
    letters = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    return SequenceMatcher(None, letters(a), letters(b), autojunk=False).ratio()


def _parse_issues(content: str) -> list[dict]:
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return []
    try:
        issues = json.loads(m.group(0)).get("issues", [])
    except json.JSONDecodeError:
        return []
    keep = [i for i in issues if isinstance(i, dict) and i.get("word")
            and str(i.get("confidence", "high")).lower() == "high"]
    return [{"word": str(i.get("word", "")).strip(), "heard_as": str(i.get("heard_as", "")),
             "tip": str(i.get("tip", ""))} for i in keep[:3]]


_LANG = {"english": "en", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
         "portuguese": "pt", "hindi": "hi", "japanese": "ja", "chinese": "zh", "korean": "ko"}


def _lang_code(lang: str):
    return _LANG.get(lang.strip().lower())
