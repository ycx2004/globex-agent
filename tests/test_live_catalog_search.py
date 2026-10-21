# -*- coding: utf-8 -*-
"""实时商品源（PDD）链路测试：映射、硬约束、精排降级、购买链接回退。"""
from __future__ import annotations

import asyncio

from app.application.usecases.live_catalog_search import LiveCatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.live.pdd_client import PddClientError
from app.infrastructure.live.pdd_mapper import map_category, pdd_goods_to_product

GOODS = [
    {
        "goods_id": 1001, "goods_name": "轻便折叠双肩包 20L 旅行背包", "goods_desc": "轻",
        "min_group_price": 12900, "opt_name": "箱包", "category_name": "箱包皮具",
        "sales_tip": "1.2万", "has_coupon": False, "goods_thumbnail_url": "https://img.example.com/a.jpg",
        "goods_sign": "sign1001",
    },
    {
        "goods_id": 1002, "goods_name": "大容量登山包 60L 户外重装", "goods_desc": "重",
        "min_group_price": 45900, "opt_name": "运动包", "category_name": "运动户外",
        "sales_tip": "300", "has_coupon": True, "coupon_discount": 2000,
        "goods_image_url": "https://img.example.com/b.jpg", "goods_sign": "sign1002",
    },
    {
        "goods_id": 1003, "goods_name": "桌面香薰蜡烛 小众设计礼盒", "goods_desc": "",
        "min_group_price": 5900, "opt_name": "家居日用", "category_name": "家居",
        "goods_sign": "sign1003",
    },
]


class FakePddClient:
    def __init__(self, goods=None, error=None):
        self._goods = goods if goods is not None else GOODS
        self._error = error
        self.promotion_calls = 0

    async def search_goods(self, keyword, *, page_size=20, page=1, pid=""):
        if self._error:
            raise self._error
        return self._goods

    async def promotion_url(self, goods_sign, *, pid, search_id=""):
        self.promotion_calls += 1
        if pid == "bad":
            raise PddClientError("service_error", "模拟失败")
        return f"https://pdd.promo/{goods_sign}?pid={pid}"


def make_usecase(client, pid="", reranker=None):
    return LiveCatalogSearchUseCase(pdd_client=client, pdd_pid=pid, page_size=20, reranker=reranker)


def run(coro):
    return asyncio.run(coro)


def test_mapper_fields_and_category():
    p = pdd_goods_to_product(GOODS[0])
    assert p is not None and p.product_id == "PDD1001"
    assert p.category == "旅行装备"
    assert p.primary_sku().price.amount_in_minor_units == 12900
    assert p.ships_to == ["CN"]
    assert map_category("耳机", "", "") == "数码配件"
    assert map_category("未知类目", "", "") == ""
    assert pdd_goods_to_product({"goods_id": 1, "goods_name": "", "min_group_price": 100}) is None


def test_live_search_budget_and_purchase_url():
    uc = make_usecase(FakePddClient(), pid="pid_1")
    spec = ProductSearchSpec(normalized_query="背包", price_max_major=300, ship_to="CN", top_k=5)
    result = run(uc.execute(spec))
    ids = [h["product_id"] for h in result["hits"]]
    assert "PDD1001" in ids and "PDD1002" not in ids  # 459 元被预算硬过滤
    hit = next(h for h in result["hits"] if h["product_id"] == "PDD1001")
    assert hit["purchase_url"] == "https://pdd.promo/sign1001?pid=pid_1"
    assert hit["image_url"] == "https://img.example.com/a.jpg"
    assert hit["image_kind"] == "live"
    assert "landed_price" not in hit
    assert result["recall_strategy"] == "pdd_live"
    assert any(f["reason"] == "over_price_cap" for f in result["filtered_out"])


def test_live_search_ship_to_cross_border_rejected():
    uc = make_usecase(FakePddClient())
    spec = ProductSearchSpec(normalized_query="背包", ship_to="US", top_k=5)
    result = run(uc.execute(spec))
    assert result["hits"] == []
    assert all(f["reason"] == "ship_to_unavailable" for f in result["filtered_out"])


def test_live_search_promotion_fallback_plain_link():
    uc = make_usecase(FakePddClient(), pid="bad")
    spec = ProductSearchSpec(normalized_query="香薰", top_k=5)
    result = run(uc.execute(spec))
    hit = next(h for h in result["hits"] if h["product_id"] == "PDD1003")
    assert hit["purchase_url"] == "https://mobile.yangkeduo.com/goods.html?goods_id=1003"


def test_live_search_api_failure_returns_empty_honestly():
    uc = make_usecase(FakePddClient(error=PddClientError("pid_required", "未备案")))
    spec = ProductSearchSpec(normalized_query="背包", top_k=5)
    result = run(uc.execute(spec))
    assert result["hits"] == []
    assert result["retrieval_diagnostics"]["pdd_error"] == "pid_required"
