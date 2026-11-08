"""OpenAI calls: verbatim transcription, language coaching, pronunciation check."""
import base64
import io
import json
import logging
import os
import re

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
                 "vocabulary_upgrades", "pronunciation_suspects", "score", "tip"],
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
    },
}


class Coach:
    def __init__(self):
        or_key = os.getenv("OPENROUTER_API_KEY")
        self.openrouter = bool(or_key) and not os.getenv("OPENAI_API_KEY")
        if self.openrouter:
            self.client = OpenAI(api_key=or_key, base_url=os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"))
            d_tr, d_coach, d_pron = "google/gemini-2.5-flash", "openai/gpt-5-mini", "google/gemini-2.5-flash"
        else:
            self.client = OpenAI()  # reads OPENAI_API_KEY (and OPENAI_BASE_URL if set)
            d_tr, d_coach, d_pron = "gpt-4o-transcribe", "gpt-5-mini", "gpt-4o-audio-preview"
        self.transcribe_model = os.getenv("TRANSCRIBE_MODEL") or d_tr
        self.coach_model = os.getenv("COACH_MODEL") or d_coach
        pron = os.getenv("PRONUNCIATION_MODEL")
        self.pron_model = (d_pron if pron is None else pron).strip()
        self.language = os.getenv("TARGET_LANGUAGE", "English")
        # OpenRouter has no /audio/transcriptions endpoint: transcribe with an audio-input chat model instead.
        mode = os.getenv("TRANSCRIBE_VIA_CHAT", "auto").lower()
        self.transcribe_via_chat = self.openrouter if mode == "auto" else mode in ("1", "true", "yes")

    # ---------- 1. speech to text ----------
    def transcribe(self, wav: bytes) -> str:
        if self.transcribe_via_chat:
            r = self.client.chat.completions.create(
                model=self.transcribe_model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": (
                        f"Transcribe this {self.language} speech verbatim. Keep grammatical mistakes, wrong words, "
                        "filler words (um, uh, like, you know), false starts and repetitions exactly as spoken. "
                        "Do NOT correct anything. Output only the transcript text. If there is no speech, output nothing.")},
                    {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}},
                ]}],
            )
            return (r.choices[0].message.content or "").strip().strip('"')
        f = io.BytesIO(wav)
        f.name = "speech.wav"
        r = self.client.audio.transcriptions.create(
            model=self.transcribe_model,
            file=f,
            language=_lang_code(self.language),
            prompt=("Transcribe exactly what is said, verbatim. Keep grammatical mistakes, wrong words, "
                    "filler words (um, uh, like, you know), false starts and repetitions. Do NOT correct anything."),
        )
        return (r.text or "").strip()

    # ---------- 2. grammar / phrasing / vocabulary ----------
    def analyze(self, text: str, known_patterns: list[dict]) -> dict:
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
"""
        r = self.client.chat.completions.create(
            model=self.coach_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": f"Transcript: \"{text}\""}],
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

    # ---------- daily coaching plan ----------
    def daily_plan(self, report: dict) -> dict:
        compact = {k: report[k] for k in ("day", "totals", "categories", "patterns", "fillers",
                                          "pronunciation", "vocabulary")}
        r = self.client.chat.completions.create(
            model=self.coach_model,
            messages=[
                {"role": "system", "content": (
                    f"You are a {self.language} speaking coach writing a short daily review. Compare today with "
                    "yesterday using the data, praise real improvement, name the top 3 recurring mistakes, and "
                    "give concrete practice: 5 drill sentences the user should say out loud that use the correct "
                    "form of their repeated mistakes, plus 3 words to practise pronouncing and 3 vocabulary words "
                    "to start using. Be warm, specific and brief.")},
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
