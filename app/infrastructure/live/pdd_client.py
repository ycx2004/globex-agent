# -*- coding: utf-8 -*-
"""拼多多开放平台（多多客）HTTP 客户端

仅服务端持有 client_secret；签名按官方规则：
client_secret + 按键名升序拼接 key+value + client_secret，MD5 后大写。
失败抛 PddClientError，调用方决定降级，不做静默重试。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

GATEWAY = "https://gw-api.pinduoduo.com/api/router"


class PddClientError(RuntimeError):
    def __init__(self, code: str, message: str, sub_code: str = ""):
        self.code = code
        self.sub_code = sub_code
        super().__init__(message)


class PddClient:
    def __init__(self, client_id: str, client_secret: str, timeout_seconds: float = 10.0) -> None:
        if not client_id or not client_secret:
            raise ValueError("PDD_CLIENT_ID / PDD_CLIENT_SECRET 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("PDD 超时必须为正数")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout_seconds

    async def call(self, api_type: str, **biz) -> dict:
        params: dict[str, str] = {
            "type": api_type,
            "client_id": self._client_id,
            "timestamp": str(int(time.time())),
            "data_type": "JSON",
            "version": "V1",
        }
        for key, value in biz.items():
            if value is None:
                continue
            params[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        raw = self._client_secret + "".join(f"{k}{params[k]}" for k in sorted(params)) + self._client_secret
        params["sign"] = hashlib.md5(raw.encode()).hexdigest().upper()
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                response = await client.post(
                    GATEWAY,
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                result = response.json()
        except httpx.TimeoutException:
            raise PddClientError("timeout", "拼多多接口超时") from None
        except httpx.HTTPError:
            raise PddClientError("network_error", "拼多多接口网络错误") from None
        except ValueError:
            raise PddClientError("invalid_json", "拼多多接口返回无效 JSON") from None
        error = result.get("error_response")
        if error:
            sub_msg = str(error.get("sub_msg") or error.get("error_msg") or "业务错误")
            if "pid" in sub_msg or "备案" in sub_msg:
                code = "pid_required"
            elif "签名" in sub_msg or "sign" in sub_msg.lower():
                code = "sign_error"
            elif "限流" in sub_msg or "LIMIT" in sub_msg:
                code = "rate_limited"
            else:
                code = "service_error"
            raise PddClientError(code, sub_msg, sub_code=str(error.get("sub_code") or ""))
        return result

    async def search_goods(self, keyword: str, *, page_size: int = 20, page: int = 1, pid: str = "") -> list[dict]:
        """关键词搜索商品；pid 缺失或未授权备案时平台拒绝。"""
        if not keyword.strip():
            return []
        result = await self.call(
            "pdd.ddk.goods.search",
            keyword=keyword.strip(),
            page_size=max(10, min(100, page_size)),
            page=page,
            sort_type=0,
            pid=pid or None,
        )
        body = result.get("goods_search_response", {})
        goods_list = body.get("goods_list") or []
        search_id = str(body.get("search_id") or "")
        for goods in goods_list:
            goods.setdefault("search_id", search_id)
        return goods_list

    async def recommend_goods(self, *, channel_type: int = 0, limit: int = 20, offset: int = 0) -> tuple[list[dict], str]:
        """推荐商品流（不需 pid），返回 (goods_list, search_id)。无关键词能力，仅作降级兜底。"""
        result = await self.call(
            "pdd.ddk.goods.recommend.get",
            channel_type=channel_type,
            limit=max(1, min(100, limit)),
            offset=offset,
        )
        body = result.get("goods_basic_detail_response", {})
        return body.get("goods_list") or [], str(body.get("search_id") or "")

    async def promotion_url(self, goods_sign: str, *, pid: str, search_id: str = "") -> str:
        """生成带佣金追踪的推广链接；失败抛 PddClientError，由调用方回退到商品页直链。"""
        if not pid:
            raise PddClientError("pid_required", "生成推广链接需要 PDD_PID")
        if not goods_sign:
            raise PddClientError("invalid_params", "生成推广链接需要 goods_sign")
        result = await self.call(
            "pdd.ddk.goods.promotion.url.generate",
            p_id=pid,
            goods_sign_list=[goods_sign],
            search_id=search_id or None,
            generate_we_app=False,
        )
        body = result.get("goods_promotion_url_generate_response", {})
        urls = body.get("goods_promotion_url_list") or []
        if not urls:
            raise PddClientError("empty_response", "推广链接生成返回为空")
        first = urls[0]
        url = first.get("url") or first.get("mobile_url") or ""
        if not url:
            raise PddClientError("empty_response", "推广链接字段缺失")
        return url
