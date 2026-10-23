# -*- coding: utf-8 -*-
"""MilvusProductIndex

商品向量索引的 Milvus 实现（COSINE 距离 + HNSW 索引），与 QdrantProductIndex 行为等价：
    - point id 用 product_id 的确定性 UUID5
    - payload（标量字段）保存 product_id / embedding_key / text_hash / embedding_schema
    - search 按 embedding_key 过滤，保证换模型/维度后旧向量不被错误复用
    - ensure_ready 幂等，且拒绝维度/距离不匹配的旧集合（禁止覆盖旧向量）
    - MILVUS_LEXICAL_ENABLED=1 时同集合额外保存 searchable_text，并由 Milvus BM25 Function
      生成稀疏向量，供大目录混合检索在服务端做词法召回（应用进程不再全库扫描）

数据路径（search/upsert/query）走异步客户端 AsyncMilvusClient；
建库与元数据校验（has_collection/describe_*）只在启动期发生，走同步 MilvusClient（to_thread 包裹）。
"""
from __future__ import annotations

import asyncio
import uuid

from pymilvus import AsyncMilvusClient, DataType, Function, FunctionType, MilvusClient

from app.domain.catalog.ports.retrieval_ports import ProductVectorIndex, VectorHit
from app.domain.catalog.product import Product
from app.infrastructure.settings import Settings
from app.infrastructure.vector.embedding_identity import embedding_identity, product_embedding_payload

_OUTPUT_FIELDS = ["id", "product_id", "embedding_key", "text_hash", "embedding_schema"]
# 全文字段上限（字节）；ESCI 10w 条 searchable_text 最长约 4.2KB，留足余量
_TEXT_MAX_LENGTH = 16384


def _point_id(product_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"globex/product/{product_id}"))


def _truncate_utf8(text: str, limit: int) -> str:
    """按 UTF-8 字节截断且不切断多字节字符，满足 VARCHAR max_length。"""
    data = text.encode()
    return text if len(data) <= limit else data[:limit].decode(errors="ignore")


def _quote(value: str) -> str:
    """转义进 Milvus 布尔表达式的字符串字面量，防止注入/语法破坏。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MilvusProductIndex(ProductVectorIndex):
    def __init__(self, settings: Settings) -> None:
        token = settings.milvus_token or ""
        self._async = AsyncMilvusClient(uri=settings.milvus_uri, token=token)
        self._admin = MilvusClient(uri=settings.milvus_uri, token=token)
        self._collection = settings.milvus_collection
        self.embedding_key = embedding_identity(settings)
        self._search_ef = settings.milvus_search_ef
        self._lexical = settings.milvus_lexical_enabled
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self._async.load_collection(self._collection)
            self._loaded = True

    async def products_needing_embeddings(self, products: list[Product]) -> list[Product]:
        if not await asyncio.to_thread(self._admin.has_collection, self._collection):
            return products
        await self._ensure_loaded()
        existing: dict[str, dict] = {}
        for start in range(0, len(products), 256):
            ids = [_point_id(p.product_id) for p in products[start:start + 256]]
            rows = await self._async.query(
                collection_name=self._collection,
                filter=f"id in [{', '.join(_quote(i) for i in ids)}]",
                output_fields=_OUTPUT_FIELDS,
            )
            existing.update({str(r["id"]): r for r in rows})
        return [p for p in products if any(
            existing.get(_point_id(p.product_id), {}).get(key) != value
            for key, value in product_embedding_payload(p, self.embedding_key).items()
        )]

    async def ensure_ready(self, vector_dim: int) -> None:
        if not await asyncio.to_thread(self._admin.has_collection, self._collection):
            schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
            schema.add_field("vec", DataType.FLOAT_VECTOR, dim=vector_dim)
            schema.add_field("product_id", DataType.VARCHAR, max_length=256)
            schema.add_field("embedding_key", DataType.VARCHAR, max_length=128)
            schema.add_field("text_hash", DataType.VARCHAR, max_length=128)
            schema.add_field("embedding_schema", DataType.VARCHAR, max_length=64)
            index_params = self._admin.prepare_index_params()
            index_params.add_index(
                field_name="vec", index_type="HNSW", metric_type="COSINE",
                params={"M": 32, "efConstruction": 200},
            )
            if self._lexical:
                # standard 分析器：Unicode 分词 + 小写；英文商品够用，中文按单字切分
                schema.add_field("text", DataType.VARCHAR, max_length=_TEXT_MAX_LENGTH,
                                 enable_analyzer=True, analyzer_params={"type": "standard"})
                schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
                schema.add_function(Function(name="text_bm25", function_type=FunctionType.BM25,
                                             input_field_names=["text"], output_field_names=["sparse"]))
                index_params.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX",
                                       metric_type="BM25", params={"inverted_index_algo": "DAAT_MAXSCORE"})
            await self._async.create_collection(
                collection_name=self._collection, schema=schema, index_params=index_params,
            )
        else:
            desc = await asyncio.to_thread(self._admin.describe_collection, self._collection)
            vec = next((f for f in desc.get("fields", []) if f.get("name") == "vec"), None)
            dim = (vec or {}).get("params", {}).get("dim")
            if vec is None or int(dim) != int(vector_dim):
                raise ValueError("已有 Milvus 集合的维度不匹配；请配置新集合，禁止覆盖旧向量")
            idx = await asyncio.to_thread(self._admin.describe_index, self._collection, "vec")
            if str(idx.get("metric_type", "")).upper() != "COSINE":
                raise ValueError("已有 Milvus 集合的距离类型不是 COSINE；请配置新集合，禁止覆盖旧向量")
            if self._lexical and not any(f.get("name") == "sparse" for f in desc.get("fields", [])):
                raise ValueError("已有 Milvus 集合没有 BM25 全文字段；请配置新集合或关闭 MILVUS_LEXICAL_ENABLED")
        self._loaded = False
        await self._ensure_loaded()

    async def upsert_products(self, products: list[Product], embeddings: list[list[float]]) -> None:
        if len(products) != len(embeddings):
            raise ValueError("products 与 embeddings 数量不一致")
        if not products:
            return
        rows = []
        for product, embedding in zip(products, embeddings):
            payload = product_embedding_payload(product, self.embedding_key)
            row = {"id": _point_id(product.product_id), "vec": embedding, **payload}
            if self._lexical:
                # 与 text_hash、精排同源：正文变化会同时触发重嵌入与全文索引更新
                row["text"] = _truncate_utf8(product.searchable_text(), _TEXT_MAX_LENGTH)
            rows.append(row)
        await self._async.upsert(collection_name=self._collection, data=rows)

    def _filter(self, product_ids: list[str] | None = None) -> str:
        expr = f"embedding_key == {_quote(self.embedding_key)}"
        if product_ids is not None:
            ids = ", ".join(_quote(_point_id(pid)) for pid in product_ids)
            expr += f" and id in [{ids}]"
        return expr

    async def _search(self, embedding: list[float], top_n: int, expr: str) -> list[VectorHit]:
        await self._ensure_loaded()
        result = await self._async.search(
            collection_name=self._collection,
            data=[embedding], anns_field="vec", limit=top_n,
            # HNSW 要求 ef >= limit；补召回翻倍到 256 时同步抬高 ef，否则服务端直接拒绝
            search_params={"metric_type": "COSINE", "params": {"ef": max(self._search_ef, top_n)}},
            filter=expr, output_fields=["product_id"],
        )
        hits = result[0] if result else []
        out: list[VectorHit] = []
        for hit in hits:
            entity = hit.get("entity") or {}
            pid = entity.get("product_id")
            if pid:
                out.append(VectorHit(product_id=pid, score=float(hit.get("distance", 0.0))))
        return out

    async def search(self, embedding: list[float], top_n: int) -> list[VectorHit]:
        return await self._search(embedding, top_n, self._filter())

    def supports_lexical_search(self) -> bool:
        return self._lexical

    async def search_lexical(self, query: str, top_n: int) -> list[VectorHit]:
        if not self._lexical:
            raise RuntimeError("未启用 MILVUS_LEXICAL_ENABLED")
        if not query.strip():
            return []
        await self._ensure_loaded()
        result = await self._async.search(
            collection_name=self._collection, data=[query], anns_field="sparse", limit=top_n,
            search_params={"metric_type": "BM25"}, filter=self._filter(), output_fields=["product_id"],
        )
        hits = result[0] if result else []
        return [VectorHit(product_id=pid, score=float(hit.get("distance", 0.0)))
                for hit in hits if (pid := (hit.get("entity") or {}).get("product_id"))]

    async def search_filtered(self, embedding: list[float], top_n: int, *, product_ids: list[str]) -> list[VectorHit]:
        if not product_ids:
            return []
        return await self._search(embedding, top_n, self._filter(product_ids))

    async def close(self) -> None:
        await self._async.close()
        try:
            self._admin.close()
        except Exception:  # noqa: BLE001 —— 同步客户端关闭失败不影响停机
            pass
