"""Live analysis.

* `analyse_sentence`: runs once per finished sentence (cut at a pause), so it stays fast and cheap.
  One structured call returns both panels: Mistakes and Suggestions.
* `hints`: while the user is still mid-sentence (browser partial transcript, debounced on the client),
  a small fast model offers quick fixes and ways to finish the sentence.
"""
from ..config import settings
from ..llm import json_call
from .schema import HINT_SCHEMA, SENTENCE_SCHEMA, normalize_hints, normalize_sentence_result


def _sentence_prompt(language: str, known_rules: list[str], focus: list[str]) -> str:
    rules = "\n".join(f"- {r}" for r in known_rules) or "(none yet)"
    focus_txt = "\n".join(f"- {f}" for f in focus) or "(none yet)"
    return f"""You are a friendly, precise {language} speaking coach. You receive a verbatim transcript of one
sentence the learner just SAID out loud (speech: ignore punctuation and capitalisation).

Find every real mistake: tense, articles, prepositions, word order, subject-verb agreement, word choice, verb form,
plurals, pronouns, sentence structure, filler words (um, uh, like, basically, you know) and needless repetition.
Do not flag acceptable informal spoken English. A correct sentence has no mistakes.

- corrected: the same sentence with only the errors fixed (minimal edit).
- List each error ONCE, at the smallest span ("go" -> "went"), never again inside a longer phrase.
- mistakes: each with the wrong words as said ("original"), the fix ("corrected"), a category, a one-line
  explanation in simple words, and "rule": a short reusable name of the RULE, lower case, so the same kind of
  mistake always gets the same name (e.g. "past simple for finished actions", "third person -s",
  "no article before uncountable nouns"). Never vague names like "grammar" or "word choice".
  If the mistake breaks one of the learner's EXISTING rules below, reuse that exact name:
{rules}
- suggestions.better_phrasing: how a fluent speaker would naturally say the same thing ('' if already natural).
- suggestions.vocabulary: up to 3 plain or overused words they used (good, nice, thing, very big...) with 2-3
  stronger alternatives each. Empty if none.
- suggestions.next_sentences: 1-2 short, correct sentences (under 15 words) they could naturally say NEXT to
  continue the topic. Where natural, use the correct form of their focus areas:
{focus_txt}
- is_directed_speech: false only for noise, a TV, or meaningless fragments.
"""


def analyse_sentence(text: str, context: list[str], known_rules: list[str], focus: list[str]) -> dict:
    ctx = "\n".join(context[-4:])
    user = (f"Conversation so far (context only, do not grade):\n{ctx}\n\n" if ctx else "") + \
        f'Sentence to analyse: "{text}"'
    raw = json_call(model=settings.analysis_model, system=_sentence_prompt(settings.language, known_rules, focus),
                    user=user, schema=SENTENCE_SCHEMA, name="sentence_feedback",
                    reasoning=settings.analysis_reasoning)
    return normalize_sentence_result(raw)


HINT_PROMPT = """You help a {language} learner WHILE they are speaking. You get the words of the sentence they are
saying right now (live recognition: unfinished, no punctuation). Reply fast and short.
- fixes: at most 2 real grammar or word mistakes in the words so far (short wrong phrase -> correct form). Check
  tense ("yesterday we go" -> "we went"), present perfect with since/for, word order and pronouns
  ("me and my wife we" -> "my wife and I"), articles, prepositions, third person -s. Ignore punctuation, the
  unfinished ending and fillers. Empty if correct so far.
- continuations: 3 natural ways to FINISH the sentence. Each starts with their words so far with EVERY mistake
  fixed, is under 20 words, simple everyday English. If the sentence is already complete, give 3 natural NEXT
  sentences instead."""


def hints(partial: str, context: list[str]) -> dict:
    if not settings.hint_model or len(partial.split()) < 2:
        return {"fixes": [], "continuations": []}
    ctx = " ".join(context[-3:])
    raw = json_call(model=settings.hint_model, system=HINT_PROMPT.format(language=settings.language),
                    user=(f"Earlier: {ctx}\n" if ctx else "") + f"Saying now: {partial[:400]}",
                    schema=HINT_SCHEMA, name="live_hints", max_tokens=220, fast=True)
    return normalize_hints(raw)
