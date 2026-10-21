# -*- coding: utf-8 -*-
"""精排 A/B 对比：同一查询、同一向量候选集，比较 向量排序 vs 向量+qwen3-rerank 的 Top-8 差异。"""
from __future__ import annotations

import asyncio

from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.rerank.http_reranker import HttpReranker
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex

QUERIES = [
    "轻便双肩包 通勤 预算300元",
    "不含塑料的保温杯 户外",
    "适合长途飞行的旅行收纳套装",
    "送朋友的桌面香薰 小众设计",
]
TOP_N = 20
SHOW = 8


async def main() -> None:
    settings = load_settings()
    repo = InMemoryProductRepository()
    embedder = OpenAIEmbeddingClient(settings)
    index = QdrantProductIndex(settings)
    reranker = HttpReranker(settings)
    products = {p.product_id: p for p in await repo.list_all()}
    try:
        for query in QUERIES:
            embedding = await embedder.embed(query)
            hits = await index.search(embedding, TOP_N)
            base = [(h.score, products[h.product_id]) for h in hits if h.product_id in products]
            docs = [p.searchable_text() for _, p in base]
            rr = await reranker.rerank(query, docs)
            reranked = sorted(zip(rr, [p for _, p in base]), key=lambda x: x[0], reverse=True)

            base_top = [p.product_id for _, p in base[:SHOW]]
            rr_top = [p.product_id for _, p in reranked[:SHOW]]
            print(f"\n{'='*78}\n查询：{query}")
            print(f"{'名次':<4}{'仅向量排序':<44}{'向量+精排':<40}")
            for i in range(SHOW):
                b = base[i] if i < len(base) else None
                r = reranked[i] if i < len(reranked) else None
                btxt = f"{b[1].product_id} {b[1].title[:18]}({b[0]:.3f})" if b else "-"
                rtxt = f"{r[1].product_id} {r[1].title[:18]}({r[0]:.3f})" if r else "-"
                mark = "" if (b and r and b[1].product_id == r[1].product_id) else " ←变动"
                print(f"#{i+1:<3}{btxt:<44}{rtxt:<40}{mark}")
            entered = [pid for pid in rr_top if pid not in base_top]
            dropped = [pid for pid in base_top if pid not in rr_top]
            print(f"新进 Top{SHOW}: {entered or '无'} | 被挤出: {dropped or '无'}")
    finally:
        await index.close()


asyncio.run(main())
