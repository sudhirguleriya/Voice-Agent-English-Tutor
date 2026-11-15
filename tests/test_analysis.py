"""Analysis module: normalising the LLM's structured output, and the speech-to-text guards."""
from app.analysis import live
from app.analysis.schema import normalize_hints, normalize_rule, normalize_sentence_result
from app.audio.stt import ChainSTT, is_noise, is_transcription_model, looks_like_reply, wrong_script


# ---------------------------------------------------------------- rule names
def test_rule_drops_example_part_and_lowercases():
    assert normalize_rule("Third person -s (he go -> he goes)", "subject_verb_agreement") == "third person -s"


def test_vague_rule_gets_a_specific_fallback_name():
    assert normalize_rule("grammar", "tense", "I go", "I went") == "tense: i go -> i went"
    assert normalize_rule("", "filler", "um", "") == "filler word um"


# ---------------------------------------------------------------- sentence result
RAW = {
    "is_directed_speech": True,
    "corrected": "Yesterday I went to the market.",
    "mistakes": [
        {"original": "go", "corrected": "went", "category": "tense", "rule": "Past simple (go -> went)",
         "explanation": "Finished action in the past."},
        {"original": "go", "corrected": "went", "category": "tense", "rule": "past simple", "explanation": "dup"},
        {"original": "market", "corrected": "market", "category": "article", "rule": "x", "explanation": "no-op"},
        {"original": "  the  ", "corrected": "", "category": "made_up_category", "rule": "", "explanation": ""},
    ],
    "suggestions": {"better_phrasing": "Yesterday I went to the market.",
                    "vocabulary": [{"word": "good", "better": ["great", "", "pleasant", "lovely", "fine"]},
                                   {"word": "", "better": ["x"]}],
                    "next_sentences": ["I bought some vegetables.", "", "It was busy.", "Extra one."]},
}


def test_normalize_sentence_result():
    r = normalize_sentence_result(RAW)
    # duplicate (same original + rule) and no-op corrections are dropped; unknown category -> "other"
    assert [(m["original"], m["rule"]) for m in r["mistakes"]] == [("go", "past simple"), ("the", "other: the -> ")]
    assert r["mistakes"][1]["category"] == "other"
    assert r["suggestions"]["vocabulary"] == [{"word": "good", "better": ["great", "pleasant", "lovely"]}]
    assert r["suggestions"]["next_sentences"] == ["I bought some vegetables.", "It was busy."]


def test_phrase_level_duplicates_are_dropped():
    raw = {"mistakes": [
        {"original": "go", "corrected": "went", "category": "tense", "rule": "past simple", "explanation": ""},
        {"original": "He go to office yesterday", "corrected": "He went to the office yesterday",
         "category": "tense", "rule": "past simple", "explanation": ""},
        {"original": "to office", "corrected": "to the office", "category": "article", "rule": "the", "explanation": ""},
        {"original": "two car", "corrected": "two cars", "category": "plural", "rule": "plural", "explanation": ""},
    ]}
    assert [m["original"] for m in normalize_sentence_result(raw)["mistakes"]] == ["go", "to office", "two car"]


def test_normalize_hints():
    h = normalize_hints({"fixes": [{"wrong": "we go", "right": "we went"}, {"wrong": "a", "right": "A"}],
                         "continuations": ["One.", " ", "Two.", "Three.", "Four."]})
    assert h == {"fixes": [{"wrong": "we go", "right": "we went"}], "continuations": ["One.", "Two.", "Three."]}


def test_analyse_sentence_uses_known_rules_and_normalises(monkeypatch):
    seen = {}

    def fake_json_call(**kw):
        seen.update(kw)
        return RAW

    monkeypatch.setattr(live, "json_call", fake_json_call)
    r = live.analyse_sentence("Yesterday I go to market", ["Me: hello"], ["past simple"], ["Articles"])
    assert "past simple" in seen["system"] and "Articles" in seen["system"]
    assert "Me: hello" in seen["user"] and seen["schema"]["required"]
    assert r["mistakes"][0]["rule"] == "past simple"


def test_hints_skip_short_partials(monkeypatch):
    monkeypatch.setattr(live, "json_call", lambda **kw: (_ for _ in ()).throw(AssertionError("no call")))
    assert live.hints("hello", []) == {"fixes": [], "continuations": []}


# ---------------------------------------------------------------- speech-to-text guards
def test_reply_detector():
    assert looks_like_reply("Understood. Please provide the speech you would like transcribed.")
    assert looks_like_reply("I'm sorry, but I currently don't have an audio clip to transcribe.")
    assert not looks_like_reply("Save it as artifact so that I can follow it later.")
    assert not looks_like_reply("Yesterday I go to market and buy some vegetable.")


def test_wrong_script_and_noise():
    assert wrong_script("कृपया इसे हिंदी में अनुवाद करें", "English")
    assert not wrong_script("Please translate this into Hindi", "English")
    assert is_noise("Thank you.") and is_noise("") and is_noise("hello")
    assert not is_noise("I went home")
    assert is_transcription_model("openai/gpt-4o-mini-transcribe") and not is_transcription_model("openai/gpt-audio-mini")


class FakeSTT:
    streaming = False

    def __init__(self, name, outputs):
        self.name, self.outputs, self.calls = name, list(outputs), 0

    def transcribe(self, wav):
        self.calls += 1
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_chain_retries_primary_after_glitch_then_uses_it():
    primary = FakeSTT("primary", ["कृपया इसे हिंदी में अनुवाद करें", "Please translate this."])
    chain = ChainSTT([primary, FakeSTT("backup", ["never"])], "English")
    assert chain.transcribe(b"") == "Please translate this." and chain.last_used == "primary"


def test_chain_goes_to_fallback_on_provider_error():
    backup = FakeSTT("backup", ["he go home"])
    chain = ChainSTT([FakeSTT("primary", [RuntimeError("model unavailable")]), backup], "English")
    assert chain.transcribe(b"") == "he go home" and chain.last_used == "backup"
