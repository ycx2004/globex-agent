# -*- coding: utf-8 -*-
"""线上装配下的 ESCI 检索评测：复用生产 Settings / Milvus / embedding / reranker，
直接调 CatalogSearchUseCase.execute，统计质量指标与分阶段延迟。

用法（在 app 容器内）：python bench_prod_retrieval.py --sample 300 --output <目录>
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复制进来的 scripts/eval/metrics.py

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.rerank.factory import create_reranker
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.product_index_factory import build_product_index
from scripts.eval.metrics import mrr, ndcg_at_k, precision_at_k, recall_at_k

CASES = Path("/tmp/bench/product_recall_esci.jsonl")
CATALOG = Path("/app/catalog/catalog-v3.jsonl")
K = 8


class Timed:
    """透明代理：记录被包装对象指定异步方法的耗时（毫秒），不改变行为。"""

    def __init__(self, inner, methods):
        self._inner, self._methods, self.log = inner, set(methods), []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in self._methods:
            return attr

        async def wrapped(*args, **kwargs):
            t = time.perf_counter()
            try:
                return await attr(*args, **kwargs)
            finally:
                self.log.append((name, (time.perf_counter() - t) * 1000))
        return wrapped


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))], 1)


def lat_summary(values):
    return {"n": len(values), "mean": round(statistics.mean(values), 1) if values else None,
            "p50": pct(values, 50), "p95": pct(values, 95), "p99": pct(values, 99)}


async def run_strategy(name, usecase, embedder, index, reranker, cases, out):
    rows = []
    for i, case in enumerate(cases, 1):
        for proxy in (embedder, index, reranker):
            if proxy is not None:
                proxy.log.clear()
        spec = ProductSearchSpec(normalized_query=case["query"], top_k=K, ship_to="CN", target_currency="CNY")
        t = time.perf_counter()
        error = None
        try:
            payload = await usecase.execute(spec)
        except Exception as err:  # noqa: BLE001 —— 失败样本保留，不静默跳过
            payload, error = {}, f"{type(err).__name__}: {err}"
        total = (time.perf_counter() - t) * 1000
        got = [h["product_id"] for h in payload.get("hits", [])]
        gold = case["relevant"]
        rows.append({
            "id": case["id"], "query": case["query"], "gold_count": len(gold), "retrieved": got,
            "recall_strategy": payload.get("recall_strategy"), "error": error,
            "rerank_status": (payload.get("retrieval_diagnostics") or {}).get("rerank_status"),
            "recall_full": recall_at_k(got, gold, K),
            "recall_capped": len(set(got[:K]) & set(gold)) / min(len(gold), K),
            "precision": precision_at_k(got, gold, K), "mrr": mrr(got, gold), "ndcg": ndcg_at_k(got, gold, K),
            "hit": bool(set(got[:K]) & set(gold)),
            "ms_total": round(total, 1),
            "ms_embed": round(sum(ms for _, ms in embedder.log), 1),
            "ms_vector": round(sum(ms for n, ms in index.log if n != "search_lexical"), 1),
            "vector_calls": sum(1 for n, _ in index.log if n != "search_lexical"),
            "ms_lexical": round(sum(ms for n, ms in index.log if n == "search_lexical"), 1),
            "fused_candidates": (payload.get("retrieval_diagnostics") or {}).get("fused_candidates"),
            "query_understanding": (payload.get("retrieval_diagnostics") or {}).get("query_understanding"),
            "ms_rerank": round(sum(ms for _, ms in reranker.log), 1) if reranker else 0.0,
        })
        if i % 50 == 0:
            print(f"  [{name}] {i}/{len(cases)}", flush=True)
    (out / f"per_query_{name}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    ok = [r for r in rows if not r["error"]]
    mean = lambda key: round(statistics.mean(r[key] for r in ok), 4) if ok else None
    return {
        "queries": len(rows), "errors": len(rows) - len(ok),
        "actual_recall_strategies": sorted({str(r["recall_strategy"]) for r in rows}),
        "rerank_status": sorted({str(r["rerank_status"]) for r in rows}),
        "k": K, f"recall@{K}_full": mean("recall_full"), f"recall@{K}_capped": mean("recall_capped"),
        f"precision@{K}": mean("precision"), "mrr": mean("mrr"), f"ndcg@{K}": mean("ndcg"),
        f"hit_rate@{K}": mean("hit"),
        "latency_ms": {stage: lat_summary([r[f"ms_{stage}"] for r in ok])
                       for stage in ("total", "embed", "vector", "lexical", "rerank")},
        "vector_calls_mean": mean("vector_calls"),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20261023)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--recall-candidates", type=int, default=None, help="覆盖 RECALL_CANDIDATES（送入精排的候选数）")
    ap.add_argument("--strategies", default="embedding_only,embedding_rerank")
    ap.add_argument("--lexical-weight", type=float, default=1.0)
    ap.add_argument("--vector-weight", type=float, default=2.0)
    ap.add_argument("--k", type=int, default=8, help="top_k 与指标截断位置")
    ap.add_argument("--query-understanding", action="store_true", help="按线上配置开启否定排除/BM25 扩展/非英语改写")
    args = ap.parse_args()
    global K
    K = args.k
    args.output.mkdir(parents=True, exist_ok=False)  # 每次新目录，不覆盖旧证据

    all_cases = [json.loads(l) for l in CASES.read_text(encoding="utf-8").splitlines() if l.strip()]
    cases = random.Random(args.seed).sample(all_cases, args.sample)
    settings = load_settings()
    t = time.perf_counter()
    repo = InMemoryProductRepository()
    load_s = time.perf_counter() - t
    embedder = Timed(OpenAIEmbeddingClient(settings), {"embed", "embed_batch"})
    index = Timed(build_product_index(settings), {"search", "search_filtered", "search_lexical"})
    reranker_raw = create_reranker(settings)
    reranker = Timed(reranker_raw, {"rerank"}) if reranker_raw else None

    recall_candidates = args.recall_candidates or settings.recall_candidates
    common = dict(recall_candidates=recall_candidates, hybrid_enabled=False)
    strategies = {
        "embedding_only": (CatalogSearchUseCase(repo, embedder=embedder, vector_index=index, **common), None),
        "embedding_rerank": (CatalogSearchUseCase(repo, embedder=embedder, vector_index=index,
                                                  reranker=reranker, **common), reranker),
    }
    hybrid = dict(recall_candidates=recall_candidates, hybrid_enabled=True,
                  hybrid_lexical_weight=args.lexical_weight, hybrid_vector_weight=args.vector_weight)
    if args.query_understanding:
        from app.infrastructure.query.llm_query_translator import LlmQueryTranslator
        hybrid.update(query_negation_enabled=settings.query_negation_enabled,
                      query_lexical_expansion_enabled=settings.query_lexical_expansion_enabled,
                      query_translator=LlmQueryTranslator(settings) if settings.query_rewrite_enabled else None)
    strategies["hybrid_only"] = (CatalogSearchUseCase(repo, embedder=embedder, vector_index=index, **hybrid), None)
    strategies["hybrid_rerank"] = (CatalogSearchUseCase(repo, embedder=embedder, vector_index=index,
                                                        reranker=reranker, **hybrid), reranker)
    strategies = {k: v for k, v in strategies.items() if k in args.strategies.split(",")}
    results = {}
    for name, (usecase, rr) in strategies.items():
        print(f"[{name}] 开始 {len(cases)} 条", flush=True)
        results[name] = await run_strategy(name, usecase, embedder, index, rr, cases, args.output)
        print(json.dumps(results[name], ensure_ascii=False), flush=True)

    fp = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    report = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "scope": "检索模块评测（直连 CatalogSearchUseCase，不经 Agent/LLM）；非 make eval-harness",
        "wiring": {"vector_backend": settings.vector_backend, "milvus_collection": settings.milvus_collection,
                   "milvus_search_ef": settings.milvus_search_ef, "embedding_model": settings.embedding_model,
                   "embedding_dim": settings.embedding_dim, "reranker_model": settings.reranker_model,
                   "recall_candidates": recall_candidates, "milvus_lexical_enabled": settings.milvus_lexical_enabled,
                   "hybrid_weights": {"lexical": args.lexical_weight, "vector": args.vector_weight},
                   "strategies": list(strategies),
                   "query_understanding": bool(args.query_understanding) and {
                       "negation": settings.query_negation_enabled, "lexical_expansion": settings.query_lexical_expansion_enabled,
                       "rewrite": settings.query_rewrite_enabled}},
        "spec": {"top_k": K, "ship_to": "CN", "target_currency": "CNY"},
        "dataset": {"cases_total": len(all_cases), "sample": args.sample, "seed": args.seed,
                    "gold": "ESCI esci_label == E", "cases_sha256": fp(CASES), "catalog_sha256": fp(CATALOG),
                    "catalog_count": len(await repo.list_all()), "catalog_load_s": round(load_s, 1)},
        "source_sha256": {p: fp(p) for p in ("/app/app/application/usecases/catalog_search.py",
                                             "/app/app/infrastructure/vector/milvus_product_index.py")},
        "results": results,
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{args.output / 'report.json'}")


if __name__ == "__main__":
    asyncio.run(main())
