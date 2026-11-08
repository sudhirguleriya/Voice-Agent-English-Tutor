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

    def save_analysis(self, *, ts, day, text, analysis, pronunciation):
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO utterances (ts, day, text, corrected, better, score, word_count, tip)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ts, day, text, analysis.get("corrected"), analysis.get("better_version"),
                 analysis.get("score"), len(text.split()), analysis.get("tip")),
            )
            uid = cur.lastrowid
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
        by = {r["id"]: {**r, "mistakes": [], "vocabulary_upgrades": [], "pronunciation": []} for r in rows}
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
