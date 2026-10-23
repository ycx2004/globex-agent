"""Skill 建议库：待确认草稿、处理结果、丢弃指纹与每日调用配额；独立文件，不进入个人 Skill 库。"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_suggestions (
 id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
 trigger TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', decision TEXT NOT NULL,
 target_skill_id TEXT, target_version TEXT, title TEXT NOT NULL, description TEXT NOT NULL,
 body TEXT NOT NULL, reason TEXT NOT NULL, signals TEXT NOT NULL, fingerprint TEXT NOT NULL,
 saved_skill_id TEXT, created_at REAL NOT NULL, resolved_at REAL);
CREATE INDEX IF NOT EXISTS skill_suggestions_buyer ON skill_suggestions(buyer_id,status,created_at);
CREATE TABLE IF NOT EXISTS skill_suggestion_attempts (
 buyer_id TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL, trigger TEXT NOT NULL,
 outcome TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(buyer_id,session_id,run_id,trigger));
"""
PENDING_TTL = 14 * 86400
FIELDS = ("id", "session_id", "run_id", "trigger", "status", "decision", "target_skill_id", "target_version",
          "title", "description", "body", "reason", "signals", "saved_skill_id", "created_at", "resolved_at")


class SuggestionStore:
    def __init__(self, path: str | Path, *, clock=time.time):
        self.path, self.clock = Path(path), clock

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            db.executescript("SAVEPOINT s;" + SCHEMA + "RELEASE s;")
            # 过期草稿只改状态不删除，保留处理历史。
            db.execute("UPDATE skill_suggestions SET status='expired',resolved_at=? WHERE status='pending' AND created_at<?",
                       (self.clock(), self.clock() - PENDING_TTL))
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        item = {key: row[key] for key in FIELDS}
        item["signals"] = json.loads(item["signals"])
        return item

    def attempted(self, buyer_id, session_id, run_id, trigger) -> bool:
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM skill_suggestion_attempts WHERE buyer_id=? AND session_id=? AND run_id=? AND trigger=?",
                              (buyer_id, session_id, run_id, trigger)).fetchone() is not None

    def record_attempt(self, buyer_id, session_id, run_id, trigger, outcome) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO skill_suggestion_attempts VALUES(?,?,?,?,?,?)",
                       (buyer_id, session_id, run_id, trigger, outcome, self.clock()))

    def model_calls_since(self, buyer_id, since: float) -> int:
        """只统计真正调用了模型的尝试；信号不足或命中缓存不计入配额。"""
        with self.transaction() as db:
            return db.execute("SELECT count(*) FROM skill_suggestion_attempts WHERE buyer_id=? AND created_at>=? "
                              "AND outcome NOT IN ('no_signal','skipped')", (buyer_id, since)).fetchone()[0]

    def dismissed_recently(self, buyer_id, fingerprint, since: float) -> bool:
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM skill_suggestions WHERE buyer_id=? AND fingerprint=? AND status='dismissed' AND resolved_at>=?",
                              (buyer_id, fingerprint, since)).fetchone() is not None

    def add(self, buyer_id, *, session_id, run_id, trigger, decision, target_skill_id, target_version,
            title, description, body, reason, signals, fingerprint) -> dict:
        with self.transaction() as db:
            # 同一会话只保留最新一条待确认草稿，旧的标记为 superseded。
            db.execute("UPDATE skill_suggestions SET status='superseded',resolved_at=? WHERE buyer_id=? AND session_id=? AND status='pending'",
                       (self.clock(), buyer_id, session_id))
            identity = "sugg-" + uuid.uuid4().hex
            db.execute("INSERT INTO skill_suggestions(id,buyer_id,session_id,run_id,trigger,decision,target_skill_id,target_version,"
                       "title,description,body,reason,signals,fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (identity, buyer_id, session_id, run_id, trigger, decision, target_skill_id, target_version,
                        title, description, body, reason, json.dumps(signals, ensure_ascii=False), fingerprint, self.clock()))
            return self._decode(db.execute("SELECT * FROM skill_suggestions WHERE id=?", (identity,)).fetchone())

    def pending(self, buyer_id, session_id=None) -> list[dict]:
        with self.transaction() as db:
            sql, args = "SELECT * FROM skill_suggestions WHERE buyer_id=? AND status='pending'", [buyer_id]
            if session_id:
                sql, args = sql + " AND session_id=?", args + [session_id]
            return [self._decode(row) for row in db.execute(sql + " ORDER BY created_at DESC LIMIT 20", args)]

    def get(self, buyer_id, identity) -> dict:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM skill_suggestions WHERE id=? AND buyer_id=?", (identity, buyer_id)).fetchone()
        if row is None:
            raise LookupError("建议不存在或不属于当前买家")
        return self._decode(row)

    def resolve(self, buyer_id, identity, status, *, saved_skill_id=None) -> dict:
        with self.transaction() as db:
            changed = db.execute("UPDATE skill_suggestions SET status=?,saved_skill_id=?,resolved_at=? "
                                 "WHERE id=? AND buyer_id=? AND status='pending'",
                                 (status, saved_skill_id, self.clock(), identity, buyer_id)).rowcount
            if changed != 1:
                raise LookupError("建议已处理、过期或不属于当前买家")
            return self._decode(db.execute("SELECT * FROM skill_suggestions WHERE id=?", (identity,)).fetchone())
