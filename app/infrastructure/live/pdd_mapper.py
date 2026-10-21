# -*- coding: utf-8 -*-
"""多多客商品 → Globex 领域 Product 的映射

字段口径：
- 价格取 min_group_price（分），PDD 不提供库存数，stock 为占位值并在 highlights 明示；
- 拼多多为境内平台，ships_to 固定 ["CN"]，跨境收货地会被硬约束如实拒绝；
- 类目按 opt_name/category_name/标题关键词映射到目录八大类，映射不确定时置空，
  实时链路的类目硬过滤由 LiveCatalogSearchUseCase 对空类目放行（关键词检索已收敛结果）。
"""
from __future__ import annotations

from app.domain.catalog.money import Money
from app.domain.catalog.product import Product, ProductHighlight
from app.domain.catalog.sku import Sku

_SOURCE_PLATFORM = "pdd"
_PROVENANCE = "pdd_live: 拼多多实时查询；价格库存以商品页为准"

# 顺序即优先级：旅行/户外等场景类目优先于兜底的家居生活
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("旅行装备", ("旅行", "旅游", "箱包", "行李箱", "登机", "拉杆箱", "护照", "颈枕", "眼罩")),
    ("户外运动", ("户外", "露营", "登山", "运动", "健身", "骑行", "钓", "帐篷", "冲锋")),
    ("数码配件", ("数码", "手机", "电脑", "耳机", "充电", "数据线", "智能", "摄影", "路由器", "键盘", "鼠标")),
    ("母婴宠物", ("母婴", "婴儿", "宝宝", "奶粉", "宠物", "猫", "狗粮", "纸尿裤")),
    ("厨房餐饮", ("厨房", "餐具", "厨具", "锅", "杯", "壶", "餐饮", "保温", "饭盒", "茶具")),
    ("美妆个护", ("美妆", "个护", "护肤", "化妆", "面膜", "洗护", "洗发", "沐浴", "香水", "口红", "面霜")),
    ("办公学习", ("办公", "文具", "学习", "图书", "书籍", "学生", "台灯", "笔记本")),
    ("家居生活", ("家居", "家装", "收纳", "家纺", "日用", "家清", "清洁", "香薰", "床", "枕", "毛巾", "纸巾")),
)


def map_category(*texts: str) -> str:
    haystack = " ".join(text for text in texts if text)
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return category
    return ""


def goods_url(goods_id: str) -> str:
    return f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pdd_goods_to_product(goods: dict) -> Product | None:
    goods_id = str(goods.get("goods_id") or "").strip()
    title = str(goods.get("goods_name") or "").strip()
    price = _to_int(goods.get("min_group_price"))
    if not goods_id or not title or price <= 0:
        return None

    product_id = f"PDD{goods_id}"
    category = map_category(
        str(goods.get("opt_name") or ""),
        str(goods.get("category_name") or ""),
        title,
    )
    brand = str(goods.get("brand_name") or "") or str(goods.get("mall_name") or "") or "拼多多商家"

    highlights = [
        ProductHighlight(label="实时数据", detail="拼多多实时查询，价格库存以商品页为准"),
    ]
    sales_tip = str(goods.get("sales_tip") or "").strip()
    if sales_tip:
        highlights.append(ProductHighlight(label="销量", detail=f"已拼 {sales_tip} 件"))
    if goods.get("has_coupon") and _to_int(goods.get("coupon_discount")) > 0:
        highlights.append(ProductHighlight(label="优惠券", detail=f"可领 {_to_int(goods.get('coupon_discount')) / 100:.0f} 元券"))
    for tag in (goods.get("unified_tags") or [])[:3]:
        name = tag.get("text") if isinstance(tag, dict) else str(tag)
        if name:
            highlights.append(ProductHighlight(label="服务", detail=str(name)))
    lgst = str(goods.get("lgst_txt") or "").strip()
    if lgst:
        highlights.append(ProductHighlight(label="物流", detail=lgst))

    description = str(goods.get("goods_desc") or "").strip() or title
    sku = Sku(
        sku_id=f"{product_id}-S1",
        spec="默认规格（多规格以商品页为准）",
        price=Money.of(price, "CNY"),
        stock=99,  # 占位：PDD 搜索接口不返回库存；下单走跳转，不承诺本地库存
    )
    return Product(
        product_id=product_id,
        title=title,
        brand=brand,
        category=category,
        origin_country="CN",
        description=description,
        highlights=highlights,
        ships_to=["CN"],
        skus=[sku],
        source_platform=_SOURCE_PLATFORM,
        external_product_id=goods_id,
        canonical_product_id="",
        material_tags=[],
        data_provenance=_PROVENANCE,
        model_spec={
            "image_url": str(goods.get("goods_image_url") or goods.get("goods_thumbnail_url") or ""),
            "purchase_url": goods_url(goods_id),
            "search_id": str(goods.get("search_id") or ""),
            "goods_sign": str(goods.get("goods_sign") or ""),
        },
    )
