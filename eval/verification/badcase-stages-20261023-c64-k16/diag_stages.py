# -*- coding: utf-8 -*-
"""Badcase 诊断：线上装配 hybrid+rerank，记录各阶段候选，定位金标丢在哪一步。只读，不改线上。"""
import asyncio, json, random, sys, time
from pathlib import Path
sys.path.insert(0, "/app")
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.rerank.factory import create_reranker
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.product_index_factory import build_product_index

W = Path("/tmp/diag"); K = 16
async def main():
    cases = [json.loads(l) for l in (W / "product_recall_esci.jsonl").read_text().splitlines() if l.strip()]
    cases = random.Random(20261023).sample(cases, 300)
    labels = json.loads((W / "labels.json").read_text())
    s = load_settings(); repo = InMemoryProductRepository()
    uc = CatalogSearchUseCase(repo, embedder=OpenAIEmbeddingClient(s), vector_index=build_product_index(s),
                              reranker=create_reranker(s), hybrid_enabled=True,
                              hybrid_lexical_weight=s.hybrid_lexical_weight, hybrid_vector_weight=s.hybrid_vector_weight,
                              recall_candidates=s.recall_candidates, capture_retrieval_stages=True)
    out = (W / "stages.jsonl").open("w")
    for i, c in enumerate(cases, 1):
        p = await uc.execute(ProductSearchSpec(normalized_query=c["query"], top_k=K, ship_to="CN", target_currency="CNY"))
        st = p.get("retrieval_stages") or {}
        lab = labels.get(c["id"], {})
        ids = set(st.get("lexical_candidates", [])) | set(st.get("vector_candidates", [])) | set(c["relevant"]) | set(lab)
        out.write(json.dumps({"id": c["id"], "query": c["query"], "gold": c["relevant"], "labels": lab,
                              "hits": [h["product_id"] for h in p.get("hits", [])], "stages": st,
                              "rerank_status": (p.get("retrieval_diagnostics") or {}).get("rerank_status"),
                              "titles": {x.product_id: x.title for x in await repo.find_by_ids(sorted(ids))}}, ensure_ascii=False) + "\n")
        if i % 50 == 0: print(i, flush=True)
    out.close()
asyncio.run(main())
