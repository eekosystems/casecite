"""AI usage: what a user spent, and what every user spent (admins)."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.services import usage as usage_service
from app.services.auth import TokenData
from app.services.permissions import require_permission

router = APIRouter(tags=["usage"])


@router.get("/usage/me")
async def my_usage(
    days: int = Query(30, ge=1, le=366),
    current_user: TokenData = require_permission("chat.use"),
) -> dict:
    """The caller's AI usage over the last ``days`` days, by day and by
    purpose, with today's spend on the instance's own keys against the
    daily quota (when one is set)."""
    return await usage_service.summary(days, user_id=current_user.user_id)


@router.get("/admin/usage")
async def all_usage(
    days: int = Query(30, ge=1, le=366),
    current_user: TokenData = require_permission("admin.users"),
) -> dict:
    """Every user's AI usage over the last ``days`` days: totals, by day, by
    purpose and per user (most tokens first)."""
    return await usage_service.summary(days)
