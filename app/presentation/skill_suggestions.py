"""买家侧 Skill 建议接口：分析会话、列出待确认、确认保存、忽略。所有入口复用买家身份校验。"""
import asyncio

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.application.skill_suggestions import TRIGGERS, SuggestionRejected
from app.infrastructure.buyer_skills import BuyerSkillConflict
from app.presentation.identity import require_buyer


class AnalyzeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=128)
    trigger: str = Field(pattern="^(" + "|".join(TRIGGERS) + ")$")


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=400)
    body: str = Field(min_length=1, max_length=12000)


def register_skill_suggestion_routes(api, get_service):
    @api.post("/commerce/skill-suggestions/analyze")
    async def analyze(body: AnalyzeBody, request: Request, buyer_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        try:
            return await get_service().analyze(buyer, body.session_id, body.trigger)
        except SuggestionRejected as error:
            raise HTTPException(409, str(error)) from error

    @api.get("/commerce/skill-suggestions")
    async def pending(request: Request, buyer_id: str = Query(min_length=1),
                      session_id: str | None = Query(default=None, max_length=128)):
        buyer = await require_buyer(request, buyer_id)
        return {"suggestions": await asyncio.to_thread(get_service().list_pending, buyer, session_id)}

    @api.post("/commerce/skill-suggestions/{suggestion_id}/accept")
    async def accept(suggestion_id: str, body: AcceptBody, request: Request, buyer_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        try:
            return await asyncio.to_thread(get_service().accept, buyer, suggestion_id,
                                           title=body.title, description=body.description, body=body.body)
        except BuyerSkillConflict as error:
            raise HTTPException(409, str(error)) from error
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @api.post("/commerce/skill-suggestions/{suggestion_id}/dismiss")
    async def dismiss(suggestion_id: str, request: Request, buyer_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        try:
            return await asyncio.to_thread(get_service().dismiss, buyer, suggestion_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
