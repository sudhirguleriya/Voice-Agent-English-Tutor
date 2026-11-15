"""Database tables (SQLAlchemy 2.0 ORM).

    CoachSession ─┬─< Utterance ──< Mistake
                  └── summary (corrected paragraph, stored on the session)
    PatternRun ──< FocusArea ──< Drill
    VoiceProfile (the user's enrolled voice, for "is this me speaking?")

All times are local time (naive), set by Python, so durations and "today" are consistent.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Mistake categories used everywhere (LLM schema, filters, charts)
CATEGORIES = [
    "tense", "article", "preposition", "word_order", "subject_verb_agreement", "word_choice",
    "verb_form", "plural", "pronoun", "sentence_structure", "filler", "repetition", "other",
]


class CoachSession(Base):
    """One listening session: live practice, or a conversation with the AI agent."""
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))                 # "live" | "practice"
    topic: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(120))         # short name written with the summary
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    corrected_paragraph: Mapped[str | None] = mapped_column(Text)  # end-of-session summary
    key_points: Mapped[list | None] = mapped_column(JSON)          # 2-4 short takeaways
    utterances: Mapped[list["Utterance"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Utterance.id")


class Utterance(Base):
    """One sentence/segment, cut at a pause."""
    __tablename__ = "utterances"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    speaker: Mapped[str] = mapped_column(String(8), default="me")  # "me" | "other" | "agent"
    text: Mapped[str] = mapped_column(Text)
    corrected: Mapped[str | None] = mapped_column(Text)
    suggestions: Mapped[dict | None] = mapped_column(JSON)         # better phrasing, vocabulary, next sentences
    analysed: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    speaker_score: Mapped[float | None] = mapped_column(Float)     # similarity to the enrolled voice
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    session: Mapped[CoachSession] = relationship(back_populates="utterances")
    mistakes: Mapped[list["Mistake"]] = relationship(
        back_populates="utterance", cascade="all, delete-orphan", order_by="Mistake.id")


class Mistake(Base):
    __tablename__ = "mistakes"
    id: Mapped[int] = mapped_column(primary_key=True)
    utterance_id: Mapped[int] = mapped_column(ForeignKey("utterances.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    original: Mapped[str] = mapped_column(Text)
    corrected: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), index=True)
    rule: Mapped[str] = mapped_column(String(160), index=True)     # stable rule name, so repeats can be counted
    explanation: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    utterance: Mapped[Utterance] = relationship(back_populates="mistakes")


class PatternRun(Base):
    """One run of the pattern engine over the mistake history."""
    __tablename__ = "pattern_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    mistakes_analysed: Mapped[int] = mapped_column(Integer, default=0)
    last_mistake_id: Mapped[int] = mapped_column(Integer, default=0)
    overview: Mapped[str] = mapped_column(Text, default="")
    focus_areas: Mapped[list["FocusArea"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="FocusArea.rank")


class FocusArea(Base):
    __tablename__ = "focus_areas"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pattern_runs.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)                 # the rule, in plain words
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    sessions: Mapped[int] = mapped_column(Integer, default=0)      # in how many sessions it happened
    examples: Mapped[list] = mapped_column(JSON, default=list)     # [{original, corrected}] from the user's speech
    agent_hint: Mapped[str] = mapped_column(Text, default="")      # how the practice agent should steer
    run: Mapped[PatternRun] = relationship(back_populates="focus_areas")
    drills: Mapped[list["Drill"]] = relationship(
        back_populates="focus_area", cascade="all, delete-orphan", order_by="Drill.id")


class Drill(Base):
    __tablename__ = "drills"
    id: Mapped[int] = mapped_column(primary_key=True)
    focus_area_id: Mapped[int] = mapped_column(ForeignKey("focus_areas.id", ondelete="CASCADE"), index=True)
    prompt: Mapped[str] = mapped_column(Text)                      # e.g. "Yesterday I ___ (go) to the market."
    answer: Mapped[str] = mapped_column(Text)                      # "Yesterday I went to the market."
    focus_area: Mapped[FocusArea] = relationship(back_populates="drills")


class VoiceProfile(Base):
    """The user's enrolled voice (speaker embedding) and the calibrated match threshold."""
    __tablename__ = "voice_profile"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)          # float32 vector, L2-normalised
    threshold: Mapped[float] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
