# -*- coding: utf-8 -*-
"""实时商品源检索用例（方案 A：纯 API 实时查询，不落本地商品库）

链路：关键词 → 多多客搜索 API → 领域映射 → 硬约束过滤 → qwen3-rerank 精排 → 商品卡。
与本地目录的差异：
- 不做向量召回（商品在平台侧，API 粗筛替代 embedding 召回）；
- 精排复用现有 Reranker，失败降级为平台返回顺序；
- 类目未映射成功时放行类目硬过滤（关键词检索已收敛结果），价格/收货地等硬约束保持不变；
- 商品卡附 purchase_url（有 PID 时为带佣推广链，否则商品页直链），landed_price 不适用境内直发。
"""
from __future__ import annotations

import logging
from typing import Optional

from app.application.usecases.catalog_search import (
    _FILTERED_OUT_LIMIT,
    CatalogSearchUseCase,
)
from app.domain.catalog.product import Product
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.live.pdd_client import PddClient, PddClientError
from app.infrastructure.live.pdd_mapper import goods_url, pdd_goods_to_product

logger = logging.getLogger(__name__)


class LiveCatalogSearchUseCase(CatalogSearchUseCase):
    def __init__(self, *, pdd_client: PddClient, pdd_pid: str = "", page_size: int = 20, **base_kwargs) -> None:
        # 实时链路不用本地仓储/向量索引；reranker、tariff 等沿用基类装配
        super().__init__(product_repo=None, embedder=None, vector_index=None, **base_kwargs)
        if page_size < 10 or page_size > 100:
            raise ValueError("PDD 检索 page_size 须在 10 到 100 之间")
        self._pdd = pdd_client
        self._pdd_pid = pdd_pid
        self._pdd_page_size = page_size

    def _reject_reason(self, product: Product, spec: ProductSearchSpec, primary=None) -> Optional[str]:
        reason = super()._reject_reason(product, spec, primary)
        # 实时商品类目由关键词检索收敛；映射不到目录类目时不施加类目硬过滤，
        # 价格、库存、收货地、材质、重量等其余硬约束保持基类判定。
        if reason == "category_mismatch" and not product.category:
            return None
        return reason

    async def execute(self, spec: ProductSearchSpec) -> dict:
        keyword = (spec.normalized_query or "").strip()
        diagnostics: dict = {"source": "pdd_live", "rerank_status": "not_configured" if self._reranker is None else "not_attempted"}
        scored: list[tuple[float, Product]] = []
        recall_strategy = "pdd_live"

        if not keyword:
            return {
                "hits": [], "total_candidates": 0,
                "recall_strategy": "pdd_live", "rerank_applied": False,
                "retrieval_diagnostics": {**diagnostics, "note": "empty_keyword"},
            }
        try:
            goods_list = await self._pdd.search_goods(keyword, page_size=self._pdd_page_size, pid=self._pdd_pid)
        except PddClientError as err:
            logger.warning("PDD 实时检索失败：%s %s", err.code, err)
            return {
                "hits": [], "total_candidates": 0,
                "recall_strategy": "pdd_live_unavailable", "rerank_applied": False,
                "retrieval_diagnostics": {**diagnostics, "pdd_error": err.code, "pdd_error_detail": str(err)[:200]},
            }

        products = [p for g in goods_list if (p := pdd_goods_to_product(g)) is not None]
        scored = [(1.0, p) for p in products]  # 平台返回顺序，精排前的占位分

        rerank_applied = False
        if scored and self._reranker is not None:
            try:
                scored = await self._rerank(spec, scored)
                recall_strategy = "pdd_live_rerank"
                rerank_applied = True
                diagnostics["rerank_status"] = "success"
            except Exception as err:  # noqa: BLE001 —— 精排失败降级平台顺序
                diagnostics.update(rerank_status="failed", reranker_error=getattr(err, "code", type(err).__name__))
                logger.warning("实时链路精排不可用，按平台返回顺序：%s", diagnostics["reranker_error"])

        filtered: list[tuple[float, Product]] = []
        filtered_out: list[dict] = []
        for score, product in scored:
            reason = self._reject_reason(product, spec)
            if reason is None:
                filtered.append((score, product))
            elif len(filtered_out) < _FILTERED_OUT_LIMIT:
                filtered_out.append(self._to_rejected(product, spec, reason))

        filtered = self._rank_preferences_and_deduplicate(filtered, spec)
        cards = []
        for score, product in filtered[: spec.top_k]:
            card = self._to_card(score, product, spec).to_dict()
            card.pop("landed_price", None)  # 境内直发不适用跨境到手价
            card["rating_is_live"] = True
            spec_extra = product.model_spec if isinstance(product.model_spec, dict) else {}
            if spec_extra.get("image_url"):
                card.update(image_url=spec_extra["image_url"], image_kind="live", image_alt=product.title)
            card["purchase_url"] = await self._purchase_url(product, spec_extra)
            cards.append(card)

        result = {
            "hits": cards,
            "total_candidates": len(filtered),
            "recall_strategy": recall_strategy,
            "rerank_applied": rerank_applied,
            "retrieval_diagnostics": diagnostics,
        }
        self._explain_shortfall(result, spec, diagnostics)
        if filtered_out:
            result["filtered_out"] = filtered_out
        return result

    async def _purchase_url(self, product: Product, spec_extra: dict) -> str:
        """有 PID 时优先生成带佣推广链；任何失败回退商品页直链，不阻断出卡。"""
        goods_id = product.external_product_id
        if self._pdd_pid and goods_id:
            try:
                return await self._pdd.promotion_url(goods_id, pid=self._pdd_pid, search_id=spec_extra.get("search_id", ""))
            except PddClientError as err:
                logger.info("推广链接生成失败（%s），回退商品页直链", err.code)
        return spec_extra.get("purchase_url") or (goods_url(goods_id) if goods_id else "")
