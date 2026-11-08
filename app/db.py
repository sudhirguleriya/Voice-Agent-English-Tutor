"""SQLite storage for utterances and the mistakes found in them."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS utterances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    corrected TEXT,
    better TEXT,
    score INTEGER,
    word_count INTEGER,
    tip TEXT
);
CREATE TABLE IF NOT EXISTS mistakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    utterance_id INTEGER NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    category TEXT NOT NULL,
    original TEXT,
    correction TEXT,
    explanation TEXT,
    pattern TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vocab (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    utterance_id INTEGER NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    word TEXT NOT NULL,
    alternatives TEXT,
    example TEXT
);
CREATE TABLE IF NOT EXISTS pronunciation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    utterance_id INTEGER NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    word TEXT NOT NULL,
    heard_as TEXT,
    tip TEXT
);
CREATE TABLE IF NOT EXISTS daily_summary (
    day TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    started TEXT NOT NULL,
    ended TEXT NOT NULL,
    polished TEXT,
    polished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_day ON conversations(day);
CREATE TABLE IF NOT EXISTS reviews (
    pattern TEXT PRIMARY KEY,
    box INTEGER NOT NULL DEFAULT 1,
    due TEXT NOT NULL,
    last TEXT,
    passes INTEGER NOT NULL DEFAULT 0,
    fails INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    pattern TEXT,
    target TEXT NOT NULL,
    said TEXT,
    similarity REAL,
    passed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_utt_day ON utterances(day);
CREATE INDEX IF NOT EXISTS idx_mis_day ON mistakes(day);
CREATE INDEX IF NOT EXISTS idx_mis_pattern ON mistakes(pattern);
CREATE INDEX IF NOT EXISTS idx_voc_day ON vocab(day);
CREATE INDEX IF NOT EXISTS idx_pron_day ON pronunciation(day);
"""


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(utterances)")}
        for col, typ in (("duration_ms", "INTEGER"),       # speaking time, for words per minute
                         ("conversation_id", "INTEGER"),   # which conversation the sentence belongs to
                         ("suggestions", "TEXT")):         # JSON list: what the user could say next
            if col not in cols:
                self.conn.execute(f"ALTER TABLE utterances ADD COLUMN {col} {typ}")
        self.conn.commit()
        self._backfill_conversations()

    # ---------- conversations ----------
    GAP_S = 600  # a pause longer than 10 minutes starts a new conversation

    @staticmethod
    def _secs(a, b):
        from datetime import datetime
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()

    def _backfill_conversations(self):
        """Group sentences saved before conversations existed."""
        with _lock:
            rows = self.conn.execute(
                "SELECT id, day, ts FROM utterances WHERE conversation_id IS NULL ORDER BY day, ts, id").fetchall()
            conv = last_day = last_ts = None
            for r in rows:
                if conv is None or r["day"] != last_day or self._secs(last_ts, r["ts"]) > self.GAP_S:
                    conv = self.conn.execute("INSERT INTO conversations (day, started, ended) VALUES (?,?,?)",
                                             (r["day"], r["ts"], r["ts"])).lastrowid
                self.conn.execute("UPDATE utterances SET conversation_id=? WHERE id=?", (conv, r["id"]))
                self.conn.execute("UPDATE conversations SET ended=? WHERE id=?", (r["ts"], conv))
                last_day, last_ts = r["day"], r["ts"]
            self.conn.commit()

    def resolve_conversation(self, *, day, ts, conversation_id=None, new=False):
        """Keep adding to the client's conversation unless it asks for a new one or the user took a long break."""
        if conversation_id and not new:
            row = self.query("SELECT id, day, ended FROM conversations WHERE id=?", (conversation_id,))
            if row and row[0]["day"] == day and self._secs(row[0]["ended"], ts) <= self.GAP_S:
                return row[0]["id"]
        if not new and not conversation_id:
            row = self.query("SELECT id, ended FROM conversations WHERE day=? ORDER BY ended DESC LIMIT 1", (day,))
            if row and self._secs(row[0]["ended"], ts) <= self.GAP_S:
                return row[0]["id"]
        with self.tx() as c:
            return c.execute("INSERT INTO conversations (day, started, ended) VALUES (?,?,?)", (day, ts, ts)).lastrowid

    def conversation_texts(self, conversation_id, limit=None):
        rows = self.query("SELECT text FROM utterances WHERE conversation_id=? ORDER BY id", (conversation_id,))
        texts = [r["text"] for r in rows]
        return texts[-limit:] if limit else texts

    def conversations_for_day(self, day):
        convs = self.query("SELECT * FROM conversations WHERE day=? ORDER BY started", (day,))
        utts = self.query(
            "SELECT u.id, u.conversation_id, u.ts, u.text, u.corrected, u.better, u.score, u.word_count,"
            " (SELECT COUNT(*) FROM mistakes m WHERE m.utterance_id=u.id AND m.category!='filler') mistakes,"
            " (SELECT COUNT(*) FROM mistakes m WHERE m.utterance_id=u.id AND m.category='filler') fillers"
            " FROM utterances u WHERE u.day=? ORDER BY u.id", (day,))
        by = {c["id"]: {**c, "polished": json.loads(c["polished"]) if c["polished"] else None, "sentences": []}
              for c in convs}
        for u in utts:
            if u["conversation_id"] in by:
                by[u["conversation_id"]]["sentences"].append(u)
        return [c for c in by.values() if c["sentences"]]

    def set_polished(self, conversation_id, created, content):
        with self.tx() as c:
            c.execute("UPDATE conversations SET polished=?, polished_at=? WHERE id=?",
                      (json.dumps(content), created, conversation_id))

    @contextmanager
    def tx(self):
        with _lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def query(self, sql, params=()):
        with _lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def save_analysis(self, *, ts, day, text, analysis, pronunciation, duration_ms=None, conversation_id=None):
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO utterances (ts, day, text, corrected, better, score, word_count, tip, duration_ms,"
                " conversation_id, suggestions) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, day, text, analysis.get("corrected"), analysis.get("better_version"),
                 analysis.get("score"), len(text.split()), analysis.get("tip"), duration_ms,
                 conversation_id, json.dumps(analysis.get("next_suggestions") or [])),
            )
            uid = cur.lastrowid
            if conversation_id:
                c.execute("UPDATE conversations SET ended=? WHERE id=? AND ended<?", (ts, conversation_id, ts))
            for m in analysis.get("mistakes", []):
                c.execute(
                    "INSERT INTO mistakes (utterance_id, day, category, original, correction, explanation, pattern)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (uid, day, m["category"], m.get("original"), m.get("correction"),
                     m.get("explanation"), normalize_pattern(m.get("pattern") or m.get("correction") or "")),
                )
            for v in analysis.get("vocabulary_upgrades", []):
                c.execute(
                    "INSERT INTO vocab (utterance_id, day, word, alternatives, example) VALUES (?,?,?,?,?)",
                    (uid, day, v["word"].strip().lower(), json.dumps(v.get("alternatives", [])), v.get("example")),
                )
            for p in pronunciation:
                c.execute(
                    "INSERT INTO pronunciation (utterance_id, day, word, heard_as, tip) VALUES (?,?,?,?,?)",
                    (uid, day, p["word"].strip().lower(), p.get("heard_as"), p.get("tip")),
                )
            return uid

    def known_patterns(self, limit=40):
        return self.query(
            "SELECT pattern, category, COUNT(*) n FROM mistakes GROUP BY pattern"
            " ORDER BY n DESC, MAX(id) DESC LIMIT ?", (limit,))

    def utterances_for_day(self, day):
        rows = self.query("SELECT * FROM utterances WHERE day=? ORDER BY id", (day,))
        if not rows:
            return rows
        ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(ids))
        mis = self.query(f"SELECT * FROM mistakes WHERE utterance_id IN ({ph})", ids)
        voc = self.query(f"SELECT * FROM vocab WHERE utterance_id IN ({ph})", ids)
        pro = self.query(f"SELECT * FROM pronunciation WHERE utterance_id IN ({ph})", ids)
        by = {r["id"]: {**r, "next_suggestions": json.loads(r.get("suggestions") or "[]"),
                        "mistakes": [], "vocabulary_upgrades": [], "pronunciation": []} for r in rows}
        for m in mis:
            by[m["utterance_id"]]["mistakes"].append(m)
        for v in voc:
            v["alternatives"] = json.loads(v["alternatives"] or "[]")
            by[v["utterance_id"]]["vocabulary_upgrades"].append(v)
        for p in pro:
            by[p["utterance_id"]]["pronunciation"].append(p)
        return list(by.values())

    def delete_utterance(self, uid):
        with self.tx() as c:
            c.execute("DELETE FROM utterances WHERE id=?", (uid,))

    # ---------- spaced-repetition practice ----------
    NOT_DRILLABLE = ("filler", "repetition", "pronunciation")

    def review_map(self):
        return {r["pattern"]: r for r in self.query("SELECT * FROM reviews")}

    def practice_cards(self, day, limit=12):
        """Cards due today: reviewed patterns whose due date has come, then never-practised ones (most frequent first)."""
        from .practice import pick_sentence
        ph = ",".join("?" * len(self.NOT_DRILLABLE))
        rows = self.query(
            "SELECT pattern, MAX(category) category, COUNT(*) total, COUNT(DISTINCT day) days, MAX(id) last_id"
            f" FROM mistakes WHERE category NOT IN ({ph}) GROUP BY pattern", self.NOT_DRILLABLE)
        rev = self.review_map()
        ranked = []
        for r in rows:
            v = rev.get(r["pattern"])
            if v and v["due"] > day:
                continue
            ranked.append(((0 if v else 1, v["due"] if v else "", -r["days"], -r["total"]), r, v))
        ranked.sort(key=lambda x: x[0])
        cards = []
        for _, r, v in ranked[:limit]:
            m = self.query(
                "SELECT m.original, m.correction, m.explanation, u.text, u.corrected FROM mistakes m"
                " JOIN utterances u ON u.id = m.utterance_id WHERE m.id=?", (r["last_id"],))[0]
            target = pick_sentence(m["corrected"] or m["correction"] or "", m["correction"])
            cards.append({
                "pattern": r["pattern"], "category": r["category"], "total": r["total"], "days": r["days"],
                "box": v["box"] if v else 0,
                "original": m["original"], "correction": m["correction"], "explanation": m["explanation"],
                "wrong": pick_sentence(m["text"], m["original"]), "target": target,
            })
        return cards

    def mastery_stats(self):
        rows = self.query(
            "SELECT COUNT(DISTINCT pattern) n FROM mistakes WHERE category NOT IN (%s)"
            % ",".join("?" * len(self.NOT_DRILLABLE)), self.NOT_DRILLABLE)
        total = rows[0]["n"]
        from .practice import MASTERED_BOX
        rev = self.query("SELECT box FROM reviews")
        mastered = sum(1 for r in rev if r["box"] >= MASTERED_BOX)
        return {"patterns": total, "new": max(0, total - len(rev)),
                "learning": len(rev) - mastered, "mastered": mastered}

    def record_attempt(self, *, ts, day, pattern, target, said, similarity, passed, grade):
        from .practice import schedule
        with self.tx() as c:
            c.execute("INSERT INTO attempts (ts, day, pattern, target, said, similarity, passed) VALUES (?,?,?,?,?,?,?)",
                      (ts, day, pattern or None, target, said, similarity, int(passed)))
            if not (pattern and grade):
                return None
            row = c.execute("SELECT box, passes, fails FROM reviews WHERE pattern=?", (pattern,)).fetchone()
            box, due = schedule(row["box"] if row else 0, passed, day)
            c.execute(
                "INSERT INTO reviews (pattern, box, due, last, passes, fails) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(pattern) DO UPDATE SET box=excluded.box, due=excluded.due, last=excluded.last,"
                " passes=reviews.passes+excluded.passes, fails=reviews.fails+excluded.fails",
                (pattern, box, due, day, int(passed), int(not passed)))
            return {"box": box, "due": due}

    def get_summary(self, day):
        rows = self.query("SELECT content FROM daily_summary WHERE day=?", (day,))
        return json.loads(rows[0]["content"]) if rows else None

    def put_summary(self, day, created, content):
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO daily_summary (day, created, content) VALUES (?,?,?)",
                      (day, created, json.dumps(content)))


def normalize_pattern(p: str) -> str:
    import re
    p = re.sub(r"\(seen \d+x\)", "", p.lower())      # bookkeeping the model may echo back
    p = re.sub(r"^\s*\[[a-z_ ]+\]\s*", "", p)        # leading "[category]"
    p = p.replace("\u2192", "->").replace("_", " ")
    p = re.sub(r"\s*\(.*$", "", p) or p                # keep the rule name, drop the "(wrong -> right)" example
    return " ".join(p.strip().rstrip(".").split())[:120]
