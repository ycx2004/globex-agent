"""对话后 Skill 建议服务：信号+防抖触发、优先修补已有 Skill、安全扫描，买家确认后才写入个人 Skill。"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

from app.domain.learning_redaction import redact
from app.domain.skill_suggestion import fingerprint, safety_issues, similarity, worth_auto_suggest

logger = logging.getLogger(__name__)

DAILY_MODEL_CALLS = 6          # 每位买家每天最多调用提炼模型的次数（自动+手动合计）
DISMISS_COOLDOWN = 30 * 86400  # 被忽略的同名建议 30 天内不再提出
DUPLICATE_THRESHOLD = 0.6      # 新建草稿与已有 Skill 正文过于相似时改为不提
MAX_MESSAGES, MAX_CHARS = 12, 600
TRIGGERS = ("idle", "manual")
IDLE_SECONDS = 180       # 本轮完成后静默多久才由服务端整理
IDLE_LOOKBACK = 1800     # 只看最近半小时完成的轮次，避免上线或重启时补跑历史会话
SCAN_INTERVAL = 30


class SuggestionRejected(ValueError):
    """请求本身不合法（会话不存在、运行中等），与“没有可提炼内容”区分。"""


class SkillSuggestionService:
    def __init__(self, store, skill_store, journal_path: Path, suggester_factory, *, clock=time.time):
        self.store, self.skill_store = store, skill_store
        self.journal_path, self.suggester_factory, self.clock = Path(journal_path), suggester_factory, clock
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------- 读取会话（只读，不改日志） ----------
    def _session(self, buyer_id: str, session_id: str) -> dict:
        db = sqlite3.connect(f"file:{self.journal_path}?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT messages_json,last_run_id FROM agui_sessions WHERE session_id=? AND buyer_id=?",
                             (session_id, buyer_id)).fetchone()
            if row is None:
                raise SuggestionRejected("会话不存在或不属于当前买家")
            status = db.execute("SELECT status FROM agui_runs WHERE run_id=?", (row[1],)).fetchone()
            tools = db.execute("SELECT count(*) FROM agui_events e JOIN agui_runs r USING(run_id) "
                               "WHERE r.session_id=? AND json_extract(e.event_json,'$.type')='TOOL_CALL_START'",
                               (session_id,)).fetchone()[0]
        finally:
            db.close()
        return {"messages": json.loads(row[0]), "run_id": row[1], "run_status": status[0] if status else None,
                "tool_calls": tools}

    @staticmethod
    def _snapshot(messages: list[dict]) -> list[dict]:
        """只取最近若干条文字消息并脱敏；不带商品卡片、订单与工具原始结果。"""
        out = []
        for message in messages[-MAX_MESSAGES:]:
            if message.get("role") not in ("user", "assistant") or not isinstance(message.get("content"), str):
                continue
            text, _ = redact(message["content"][:MAX_CHARS])
            out.append({"role": message["role"], "content": text})
        return out

    def idle_sessions(self) -> list[tuple[str, str]]:
        """最后一轮已完成满静默时长、且之后没有新轮次的会话；已检查过的轮次跳过。"""
        now = self.clock()
        db = sqlite3.connect(f"file:{self.journal_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = db.execute("SELECT s.buyer_id,s.session_id,s.last_run_id FROM agui_sessions s "
                              "JOIN agui_runs r ON r.run_id=s.last_run_id "
                              "WHERE r.status='completed' AND r.updated_at BETWEEN ? AND ?",
                              (now - IDLE_LOOKBACK, now - IDLE_SECONDS)).fetchall()
        finally:
            db.close()
        return [(buyer, session) for buyer, session, run in rows
                if not self.store.attempted(buyer, session, run, "idle")]

    # ---------- 分析 ----------
    async def analyze(self, buyer_id: str, session_id: str, trigger: str) -> dict:
        if trigger not in TRIGGERS:
            raise SuggestionRejected("未知触发方式")
        lock = self._locks.setdefault(buyer_id, asyncio.Lock())
        if lock.locked():
            return {"status": "busy", "suggestion": None}
        async with lock:
            return await self._analyze(buyer_id, session_id, trigger)

    async def _analyze(self, buyer_id, session_id, trigger) -> dict:
        session = await asyncio.to_thread(self._session, buyer_id, session_id)
        if session["run_status"] not in ("completed",):
            raise SuggestionRejected("本轮尚未完成，完成后再整理")
        run_id = session["run_id"]
        existing = await asyncio.to_thread(self.store.pending, buyer_id, session_id)
        if existing and existing[0]["run_id"] == run_id:
            return {"status": "pending", "suggestion": existing[0]}
        if await asyncio.to_thread(self.store.attempted, buyer_id, session_id, run_id, trigger):
            return {"status": "already_checked", "suggestion": None}

        user_texts = [m["content"] for m in session["messages"] if m.get("role") == "user" and isinstance(m.get("content"), str)]
        signals = worth_auto_suggest(user_texts, session["tool_calls"]) if trigger == "idle" else ["manual"]
        if not signals or not user_texts:
            await asyncio.to_thread(self.store.record_attempt, buyer_id, session_id, run_id, trigger, "no_signal")
            return {"status": "no_signal", "suggestion": None}
        if await asyncio.to_thread(self.store.model_calls_since, buyer_id, self.clock() - 86400) >= DAILY_MODEL_CALLS:
            return {"status": "quota", "suggestion": None}

        skills = await asyncio.to_thread(self.skill_store.list, buyer_id, body=True)
        catalog = [{"id": s["id"], "title": s["title"], "description": s["description"], "body": s["body"][:1200]} for s in skills[:20]]
        try:
            proposal, _usage = await self.suggester_factory().propose(self._snapshot(session["messages"]), catalog)
        except Exception as error:  # noqa: BLE001 - 提炼失败只记录，不影响对话
            logger.warning("Skill 建议提炼失败: %s", type(error).__name__)
            await asyncio.to_thread(self.store.record_attempt, buyer_id, session_id, run_id, trigger, "error")
            return {"status": "error", "suggestion": None}

        outcome, suggestion = self._accept_proposal(proposal, skills)
        if suggestion is not None and await asyncio.to_thread(
                self.store.dismissed_recently, buyer_id, suggestion["fingerprint"], self.clock() - DISMISS_COOLDOWN):
            outcome, suggestion = "dismissed_before", None
        await asyncio.to_thread(self.store.record_attempt, buyer_id, session_id, run_id, trigger, outcome)
        if suggestion is None:
            return {"status": outcome, "suggestion": None}
        saved = await asyncio.to_thread(self.store.add, buyer_id, session_id=session_id, run_id=run_id, trigger=trigger,
                                        signals=signals, reason=proposal.reason, **suggestion)
        return {"status": "suggested", "suggestion": saved}

    @staticmethod
    def _accept_proposal(proposal, skills) -> tuple[str, dict | None]:
        if proposal.decision == "none" or proposal.kind != "procedure":
            return "declined", None
        title, description, body = (proposal.title or "").strip(), (proposal.description or "").strip(), (proposal.body or "").strip()
        if not title or not description or len(body) < 20:
            return "invalid", None
        if safety_issues(title, description, body):
            return "unsafe", None
        target = None
        if proposal.decision == "update":
            target = next((s for s in skills if s["id"] == proposal.target_skill_id), None)
            if target is None:
                return "invalid", None
            if similarity(target["body"], body) > 0.97:
                return "duplicate", None
        elif any(similarity(s["body"], body) >= DUPLICATE_THRESHOLD for s in skills):
            return "duplicate", None
        return "suggested", {"decision": proposal.decision, "target_skill_id": target["id"] if target else None,
                             "target_version": target["version"] if target else None, "title": title,
                             "description": description, "body": body, "fingerprint": fingerprint(title)}

    # ---------- 买家处理 ----------
    def list_pending(self, buyer_id, session_id=None) -> list[dict]:
        return self.store.pending(buyer_id, session_id)

    def accept(self, buyer_id, suggestion_id, *, title, description, body) -> dict:
        suggestion = self.store.get(buyer_id, suggestion_id)
        if suggestion["status"] != "pending":
            raise LookupError("建议已处理或已过期")
        issues = safety_issues(title, description, body)
        if issues:
            raise ValueError("内容未通过安全检查：" + "、".join(issues))
        skill = self.skill_store.save(buyer_id, title, description, body,
                                      skill_id=suggestion["target_skill_id"], expected_version=suggestion["target_version"])
        resolved = self.store.resolve(buyer_id, suggestion_id, "accepted", saved_skill_id=skill["id"])
        return {"suggestion": resolved, "skill": skill}

    def dismiss(self, buyer_id, suggestion_id) -> dict:
        return {"suggestion": self.store.resolve(buyer_id, suggestion_id, "dismissed")}


async def run_idle_scan(get_service, *, interval: float = SCAN_INTERVAL, sleep=asyncio.sleep):
    """服务端空闲整理：页面关闭或切到后台也照常进行，结果留在待确认列表，由前端读取。"""
    while True:
        await sleep(interval)
        try:
            service = get_service()
            for buyer_id, session_id in await asyncio.to_thread(service.idle_sessions):
                try:
                    await service.analyze(buyer_id, session_id, "idle")
                except SuggestionRejected:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单次扫描失败不能停掉之后的扫描
            logger.exception("空闲 Skill 整理扫描失败")
