# -*- coding: utf-8 -*-
"""商品向量索引后端工厂。

按 settings.vector_backend 选择实现：
    - "qdrant"（默认）：QdrantProductIndex，demo 3700 条
    - "milvus"：MilvusProductIndex，10w 商品库（需配置 MILVUS_URI）

品类知识库 RAG 不经过此工厂，始终走 AgentScope 的 QdrantStore，不受影响。
"""
from __future__ import annotations

from app.domain.catalog.ports.retrieval_ports import ProductVectorIndex
from app.infrastructure.settings import Settings


def build_product_index(settings: Settings) -> ProductVectorIndex:
    backend = (settings.vector_backend or "qdrant").strip().lower()
    if backend == "milvus":
        if not settings.milvus_uri:
            raise ValueError("VECTOR_BACKEND=milvus 需要配置 MILVUS_URI")
        from app.infrastructure.vector.milvus_product_index import MilvusProductIndex
        return MilvusProductIndex(settings)
    if backend == "qdrant":
        from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex
        return QdrantProductIndex(settings)
    raise ValueError(f"未知的 VECTOR_BACKEND：{backend!r}（支持 qdrant / milvus）")
