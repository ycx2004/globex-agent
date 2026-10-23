"""学习候选的管理员审核服务：提炼与自动校验可定时执行；批准、发布、撤回只由管理员手动触发。"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.learning import digest, validate_strategy

logger = logging.getLogger(__name__)

VALIDATOR_VERSION = "auto-structural-v1"
# 不适用场景探针：与选购策略无关的常见请求。策略关键词命中任一探针说明触发范围过宽。
NEGATIVE_PROBES = ("你好", "查看我的订单状态", "取消刚才的订单", "把这件商品加入收藏", "今天天气怎么样", "修改收货地址")
MAX_JOBS_PER_RUN = 200
# 运行历史只保留最近若干轮报告；累计数字另从任务表实时统计，不依赖历史文件。
HISTORY_LIMIT = 30
SAFE_JOB_FIELDS = ("id", "source_kind", "status", "attempts", "last_error", "created_at", "updated_at")


def _keyword_hits(keywords, text: str) -> list[str]:
    # 与 CapabilityRegistry.lookup_strategies 相同的子串匹配，保证校验的就是线上触发行为。
    folded = text.casefold()
    return [word for word in keywords if word.strip().casefold() in folded]


class LearningReviewService:
    def __init__(self, store, registry_factory, extractor_factory=None, *, report_path: Path | None = None, clock=time.time):
        self.store, self.registry_factory, self.extractor_factory = store, registry_factory, extractor_factory
        self.report_path, self.clock = report_path, clock
        self._lock = asyncio.Lock()
        self.running_since: float | None = None

    # ---------- 自动校验 ----------
    def _job(self, job_id):
        with self.store.transaction() as db:
            return self.store._decode(db.execute("SELECT * FROM learning_jobs WHERE id=?", (job_id,)).fetchone())

    def check_candidate(self, candidate) -> dict:
        """确定性检查：正例=来源证据可复现且会触发；反例=无关请求不触发。不调用模型，不判断收益。"""
        document = candidate["document"]
        keywords = document.get("keywords", [])
        positive = {"case_id": "source-evidence", "kind": "positive"}
        try:
            evidence = self.store.evidence(self._job(candidate["job_id"]))
            validate_strategy(document, evidence)
            source = evidence.get("input") if isinstance(evidence.get("input"), str) else ""
            hits = _keyword_hits(keywords, source)
            positive.update(passed=bool(hits), reason="来源请求命中关键词" if hits else "来源请求未命中任何关键词，线上不会触发",
                            evidence_hash=digest({"input": digest(source), "facts": digest(evidence.get("facts", [])), "candidate": candidate["content_hash"], "hits": hits}))
        except Exception as error:  # noqa: BLE001 - 证据缺失或篡改都记为未通过
            positive.update(passed=False, reason="来源证据复核失败：" + type(error).__name__, evidence_hash=digest({"candidate": candidate["content_hash"], "error": type(error).__name__}))
        probe_hits = {probe: _keyword_hits(keywords, probe) for probe in NEGATIVE_PROBES}
        probe_hits = {probe: hits for probe, hits in probe_hits.items() if hits}
        negative = {"case_id": "unrelated-probes", "kind": "negative", "passed": not probe_hits,
                    "reason": "无关请求均不触发" if not probe_hits else "关键词过宽：" + "、".join(sorted({w for hits in probe_hits.values() for w in hits})),
                    "evidence_hash": digest({"probes": NEGATIVE_PROBES, "keywords": keywords, "candidate": candidate["content_hash"]})}
        return {"candidate_id": candidate["id"], "passed": positive["passed"] and negative["passed"], "cases": [positive, negative]}

    def validate(self, candidate_id) -> dict:
        candidate = self.store.get("strategy", candidate_id)
        if candidate["status"] != "captured":
            raise ValueError("只有待校验的候选可以执行校验")
        result = self.check_candidate(candidate)
        if result["passed"]:
            checks = [{key: case[key] for key in ("case_id", "kind", "passed", "evidence_hash")} for case in result["cases"]]
            self.store.validate_candidate(candidate_id, expected_hash=candidate["content_hash"], checks=checks, validator_version=VALIDATOR_VERSION)
        return result

    # ---------- 提炼 + 校验一轮 ----------
    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run_cycle(self, *, trigger: str, max_jobs: int = MAX_JOBS_PER_RUN) -> dict:
        if self._lock.locked():
            raise RuntimeError("已有提炼任务在执行")
        async with self._lock:
            self.running_since = self.clock()
            try:
                return await self._run_cycle(trigger, max_jobs)
            finally:
                self.running_since = None

    async def _run_cycle(self, trigger, max_jobs):
        from app.application.learning import LearningWorker
        started = self.clock()
        extractor = self.extractor_factory() if self.extractor_factory else None
        worker = LearningWorker(self.store, extractor)
        processed: dict[str, int] = {}
        for _ in range(max_jobs):
            result = await worker.run_once()
            if result is None:
                break
            processed[result.get("status", "unknown")] = processed.get(result.get("status", "unknown"), 0) + 1
        validated, failed = 0, 0
        for candidate in await asyncio.to_thread(self.store.list_records, "strategy", 200):
            if candidate["status"] != "captured":
                continue
            outcome = await asyncio.to_thread(self.validate, candidate["id"])
            validated, failed = validated + outcome["passed"], failed + (not outcome["passed"])
        report = {"trigger": trigger, "started_at": started, "finished_at": self.clock(), "jobs": processed,
                  "validated": validated, "validation_failed": failed, "auto_approved": 0, "auto_published": 0}
        if self.report_path is not None:
            await asyncio.to_thread(self._write_report, report)
        logger.info("学习提炼完成 trigger=%s jobs=%s validated=%s failed=%s", trigger, processed, validated, failed)
        return report

    @property
    def history_path(self) -> Path | None:
        return self.report_path.with_name(self.report_path.stem + "_history.jsonl") if self.report_path else None

    def _write_report(self, report):
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        # 首次写历史时并入旧版只保存最后一轮的报告，避免丢掉上线前那一轮。
        history = self.history() + [report]
        temp = self.history_path.with_suffix(".tmp")
        temp.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in history[-HISTORY_LIMIT:]), encoding="utf-8")
        temp.replace(self.history_path)
        temp = self.report_path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.report_path)

    def history(self, limit=HISTORY_LIMIT) -> list[dict]:
        """最近若干轮运行报告，新的在后；没有历史文件时退回最后一轮报告。"""
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines() if self.history_path else []
            return [json.loads(line) for line in lines if line.strip()][-limit:]
        except FileNotFoundError:
            last = self.last_report()
            return [last] if last else []
        except (OSError, ValueError):
            return []

    def job_totals(self) -> dict:
        """全部历史任务的去向（按任务结果状态统计），以及最近一次处理时间。"""
        with self.store.transaction() as db:
            rows = db.execute("SELECT status,result_json,updated_at FROM learning_jobs").fetchall()
        outcomes: dict[str, int] = {}
        pending, last_processed = 0, None
        for status, result_json, updated_at in rows:
            if status in ("pending", "running"):
                pending += 1
                continue
            try:
                outcome = (json.loads(result_json) or {}).get("status") if result_json else None
            except ValueError:
                outcome = None
            key = outcome or status
            outcomes[key] = outcomes.get(key, 0) + 1
            last_processed = max(last_processed or 0, updated_at or 0) or None
        return {"total": len(rows), "pending": pending, "outcomes": outcomes, "last_processed_at": last_processed}

    def last_report(self):
        try:
            return json.loads(self.report_path.read_text(encoding="utf-8")) if self.report_path else None
        except (OSError, ValueError):
            return None

    # ---------- 管理员操作 ----------
    def overview(self, limit=100) -> dict:
        candidates = []
        for row in self.store.list_records("strategy", limit):
            item = {key: row.get(key) for key in ("id", "job_id", "content_hash", "revision", "status", "reviewer", "review_note", "created_at", "updated_at")}
            item["document"], item["validation"] = row["document"], row.get("validation")
            if row["status"] == "captured":
                # 未通过的自动校验不落库，展示时现算，管理员可据此驳回。
                item["pending_check"] = self.check_candidate(row)
            candidates.append(item)
        jobs = [{key: job.get(key) for key in SAFE_JOB_FIELDS} | {"result_status": (job.get("result") or {}).get("status")}
                for job in self.store.list_records("job", 50)]
        return {"summary": self.store.summary(), "candidates": candidates, "jobs": jobs}

    def review(self, candidate_id, *, accepted, expected_hash, expected_revision, reviewer, note):
        self.store.review("strategy", candidate_id, expected_revision=expected_revision, expected_hash=expected_hash,
                          accepted=accepted, reviewer=reviewer, note=note)
        return self.store.get("strategy", candidate_id)

    def publish(self, candidate_id, *, expected_hash, actor, expires_days):
        from app.infrastructure.learning_publication import publish_candidate
        if type(expires_days) is not int or not 1 <= expires_days <= 365:
            raise ValueError("有效期须为 1 至 365 天")
        expires = dt.datetime.fromtimestamp(self.clock() + expires_days * 86400, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return publish_candidate(self.store, self.registry_factory(), candidate_id, expected_hash=expected_hash, actor=actor, expires_at=expires)

    def withdraw(self, candidate_id, *, actor, reason):
        from app.infrastructure.learning_publication import withdraw_candidate
        withdraw_candidate(self.store, self.registry_factory(), candidate_id, actor=actor, reason=reason)
        return self.store.get("strategy", candidate_id)


def next_midnight(now: float, timezone: str) -> float:
    zone = ZoneInfo(timezone)
    local = dt.datetime.fromtimestamp(now, zone)
    tomorrow = (local + dt.timedelta(days=1)).date()
    return dt.datetime.combine(tomorrow, dt.time(0, 0), zone).timestamp()


async def run_daily(service: LearningReviewService, timezone: str, *, clock=time.time, sleep=asyncio.sleep):
    """每天当地 0 点提炼并自动校验；只产生待审核候选，从不批准或发布。"""
    while True:
        await sleep(max(1.0, next_midnight(clock(), timezone) - clock()))
        try:
            await service.run_cycle(trigger="schedule")
        except RuntimeError:
            logger.info("定时提炼跳过：已有手动任务在执行")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单轮失败不能停掉之后的定时任务
            logger.exception("定时提炼失败")
