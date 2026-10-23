"""账号登录与学习审核接口。账号只由服务器命令行创建；令牌由服务端密钥签发。"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.infrastructure.account_store import AccountLocked
from app.infrastructure.identity import IdentityError
from app.presentation.identity import _token, identity_policy

TOKEN_TTL_SECONDS = 12 * 3600


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_days: int = Field(default=90, ge=1, le=365)


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


def _verified(request: Request) -> str:
    try:
        return identity_policy(request).verify(_token(request))
    except IdentityError as error:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录", headers={"WWW-Authenticate": "Bearer"}) from error


def require_learning_admin(request: Request) -> str:
    admins = getattr(request.app.state, "learning_admin_buyers", ())
    if not admins:
        raise HTTPException(status_code=404, detail="学习审核未启用")
    if identity_policy(request).mode != "hmac":
        raise HTTPException(status_code=503, detail="学习审核要求严格签名身份")
    buyer = _verified(request)
    if buyer not in admins:
        raise HTTPException(status_code=403, detail="该账号没有学习审核权限")
    return buyer


def _call_error(error: Exception) -> HTTPException:
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail="候选不存在")
    return HTTPException(status_code=409, detail=str(error))


def register_auth_routes(api, get_accounts, get_review_service) -> None:
    @api.get("/commerce/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        if identity_policy(request).mode != "hmac":
            return {"mode": "demo", "buyer_id": None, "is_learning_admin": False}
        buyer = _verified(request)
        return {"mode": "hmac", "buyer_id": buyer,
                "is_learning_admin": buyer in getattr(request.app.state, "learning_admin_buyers", ())}

    @api.post("/commerce/auth/login")
    async def login(request: Request) -> dict[str, Any]:
        # 手动解析：默认 422 会回显 input，可能把口令原文写回响应。
        try:
            body = LoginRequest.model_validate(await request.json())
        except (ValueError, ValidationError):
            raise HTTPException(status_code=422, detail="请填写账号和口令") from None
        policy = identity_policy(request)
        if policy.mode != "hmac":
            raise HTTPException(status_code=404, detail="当前部署未启用账号登录")
        accounts = get_accounts()
        try:
            ok = await asyncio.to_thread(accounts.verify, body.username, body.password)
        except AccountLocked as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        if not ok:
            # 不区分账号不存在与口令错误。
            raise HTTPException(status_code=401, detail="账号或口令错误")
        buyer = body.username.strip()
        return {"access_token": policy.issue(buyer, ttl_seconds=TOKEN_TTL_SECONDS), "token_type": "bearer",
                "expires_in": TOKEN_TTL_SECONDS, "buyer_id": buyer,
                "is_learning_admin": buyer in getattr(request.app.state, "learning_admin_buyers", ())}

    # ---------- 学习审核：全部要求管理员签名身份 ----------
    @api.get("/commerce/learning-review")
    async def overview(request: Request) -> dict[str, Any]:
        require_learning_admin(request)
        service = get_review_service()
        data = await asyncio.to_thread(service.overview)
        return data | {"running": service.running, "running_since": service.running_since,
                       "last_run": await asyncio.to_thread(service.last_report),
                       "run_history": await asyncio.to_thread(service.history, 10),
                       "job_totals": await asyncio.to_thread(service.job_totals),
                       "schedule_timezone": getattr(request.app.state, "learning_schedule_timezone", "") or None}

    @api.post("/commerce/learning-review/run")
    async def run(request: Request) -> dict[str, Any]:
        require_learning_admin(request)
        service = get_review_service()
        if service.running:
            raise HTTPException(status_code=409, detail="已有提炼任务在执行")
        # 提炼可能持续数分钟，后台执行；页面轮询概览获取进度。
        task = asyncio.create_task(service.run_cycle(trigger="manual"))
        request.app.state.learning_manual_task = task
        return {"started": True}

    @api.post("/commerce/learning-review/candidates/{candidate_id}/validate")
    async def validate(request: Request, candidate_id: str) -> dict[str, Any]:
        require_learning_admin(request)
        try:
            return await asyncio.to_thread(get_review_service().validate, candidate_id)
        except (LookupError, ValueError) as error:
            raise _call_error(error) from error

    @api.post("/commerce/learning-review/candidates/{candidate_id}/review")
    async def review(request: Request, candidate_id: str, body: ReviewRequest) -> dict[str, Any]:
        admin = require_learning_admin(request)
        try:
            row = await asyncio.to_thread(get_review_service().review, candidate_id, accepted=body.accepted,
                expected_hash=body.expected_hash, expected_revision=body.expected_revision, reviewer=admin, note=body.note)
        except (LookupError, ValueError) as error:
            raise _call_error(error) from error
        return {"id": row["id"], "status": row["status"]}

    @api.post("/commerce/learning-review/candidates/{candidate_id}/publish")
    async def publish(request: Request, candidate_id: str, body: PublishRequest) -> dict[str, Any]:
        admin = require_learning_admin(request)
        try:
            published = await asyncio.to_thread(get_review_service().publish, candidate_id,
                expected_hash=body.expected_hash, actor=admin, expires_days=body.expires_days)
        except (LookupError, ValueError) as error:
            raise _call_error(error) from error
        except Exception as error:  # noqa: BLE001 - 注册表 CAS/校验失败
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"id": candidate_id, "status": "published", "published": published}

    @api.post("/commerce/learning-review/candidates/{candidate_id}/withdraw")
    async def withdraw(request: Request, candidate_id: str, body: WithdrawRequest) -> dict[str, Any]:
        admin = require_learning_admin(request)
        try:
            row = await asyncio.to_thread(get_review_service().withdraw, candidate_id, actor=admin, reason=body.reason)
        except (LookupError, ValueError) as error:
            raise _call_error(error) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"id": row["id"], "status": row["status"]}
