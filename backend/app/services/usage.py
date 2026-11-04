"""AI usage metering and the server-key quota.

Every provider call the app makes passes through one of a few wrappers
(``llm_clients.openai_chat``, ``generation.call_llm``,
``generation.chosen_provider_text``); each hands the provider's response
here, and the token counts it reported are recorded against the user of
the current request (``app.rls.request_user_id``) together with the
provider, the model, what the call was for and whether the instance's own
key carried it. Recording is best-effort: a failure is logged and the call
is not affected.

``SERVER_KEY_DAILY_TOKEN_QUOTA`` (0 = unlimited) caps, per user and per UTC
day, the tokens a user who has no provider key of their own may spend on the
instance's keys. It is checked when a request resolves its keys, before any
call is made, and answered with 429 and the time the window resets.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.config import settings

logger = logging.getLogger(__name__)


class QuotaExceeded(Exception):
    """The user has spent today's server-key token allowance."""

    def __init__(self, used: int, quota: int):
        self.used = used
        self.quota = quota
        super().__init__(
            f"You have used {used:,} of the {quota:,} AI tokens per day this instance allows "
            "on its own provider keys. The allowance resets at midnight UTC; add your own "
            "API key in Settings to continue now."
        )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def tokens_from_response(provider: str, response: Any) -> tuple[int, int, int]:
    """(prompt, completion, total) as the provider reported them, else zeros.

    OpenAI-compatible: ``usage.prompt_tokens`` / ``completion_tokens``.
    Anthropic: ``usage.input_tokens`` / ``output_tokens``.
    Google (google-genai): ``usage_metadata.prompt_token_count`` /
    ``candidates_token_count``; the REST fallback carries ``usageMetadata``.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage") or response.get("usageMetadata")
    if provider == "google":
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            payload = getattr(response, "payload", None)
            if isinstance(payload, dict):
                meta = payload.get("usageMetadata")
        if meta is not None:
            get = meta.get if isinstance(meta, dict) else lambda k, d=None: getattr(meta, k, d)
            prompt = _int(get("prompt_token_count") or get("promptTokenCount"))
            completion = _int(get("candidates_token_count") or get("candidatesTokenCount"))
            total = _int(get("total_token_count") or get("totalTokenCount")) or (
                prompt + completion
            )
            return prompt, completion, total
        return 0, 0, 0
    if usage is None:
        return 0, 0, 0
    get = usage.get if isinstance(usage, dict) else lambda k, d=None: getattr(usage, k, d)
    if provider == "anthropic":
        prompt, completion = _int(get("input_tokens")), _int(get("output_tokens"))
        return prompt, completion, prompt + completion
    prompt, completion = _int(get("prompt_tokens")), _int(get("completion_tokens"))
    total = _int(get("total_tokens")) or (prompt + completion)
    return prompt, completion, total


def _event(
    *,
    user_id: str | None,
    provider: str,
    model: str | None,
    purpose: str,
    tokens: tuple[int, int, int],
    server_key: bool,
):
    from app.models.usage import UsageEvent

    prompt, completion, total = tokens
    return UsageEvent(
        user_id=user_id,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        provider=provider,
        model=(model or "")[:120] or None,
        purpose=(purpose or "llm")[:40],
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        server_key=bool(server_key),
    )


def _annotate_span(
    provider: str, model: str | None, tokens: tuple[int, int, int], server_key: bool
):
    """Token counts onto the current trace span (gen_ai semantic conventions)."""
    try:
        from app.telemetry import annotate

        annotate(
            **{
                "gen_ai.system": provider,
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": tokens[0],
                "gen_ai.usage.output_tokens": tokens[1],
                "casecite.server_key": bool(server_key),
            }
        )
    except Exception:  # noqa: BLE001 - never affect the call
        pass


def current_user_id() -> str | None:
    from app import rls

    return rls.request_user_id.get()


async def record_usage(
    provider: str,
    response: Any,
    *,
    model: str | None,
    purpose: str,
    server_key: bool,
    user_id: str | None = None,
) -> None:
    """Record one call. Never raises; never blocks the call it describes."""
    try:
        tokens = tokens_from_response(provider, response)
        _annotate_span(provider, model, tokens, server_key)
        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            db.add(
                _event(
                    user_id=user_id or current_user_id(),
                    provider=provider,
                    model=model,
                    purpose=purpose,
                    tokens=tokens,
                    server_key=server_key,
                )
            )
            await db.commit()
    except Exception as e:  # noqa: BLE001 - metering must not affect the request
        logger.debug(f"Usage not recorded ({provider}/{purpose}): {e}")


def record_usage_sync(
    provider: str,
    response: Any,
    *,
    model: str | None,
    purpose: str,
    server_key: bool,
    user_id: str | None = None,
) -> None:
    """The same, from code running in a worker thread."""
    try:
        tokens = tokens_from_response(provider, response)
        _annotate_span(provider, model, tokens, server_key)
        from app.database import SyncSessionLocal

        with SyncSessionLocal() as db:
            db.add(
                _event(
                    user_id=user_id or current_user_id(),
                    provider=provider,
                    model=model,
                    purpose=purpose,
                    tokens=tokens,
                    server_key=server_key,
                )
            )
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Usage not recorded ({provider}/{purpose}): {e}")


# ── Quota ─────────────────────────────────────────────────────────────────


def _day_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def server_key_tokens_today_sync(user_id: str) -> int:
    from app.database import SyncSessionLocal
    from app.models.usage import UsageEvent

    with SyncSessionLocal() as db:
        value = db.execute(
            select(func.coalesce(func.sum(UsageEvent.total_tokens), 0)).where(
                UsageEvent.user_id == user_id,
                UsageEvent.server_key.is_(True),
                UsageEvent.created_at >= _day_start(),
            )
        ).scalar()
        return int(value or 0)


def check_server_key_quota(user_id: str) -> None:
    """Raise QuotaExceeded when the user has spent today's allowance.

    A failure to read usage lets the request through: the quota is a
    budget, not a security control, and a metering outage must not stop
    the firm's work.
    """
    quota = int(settings.server_key_daily_token_quota or 0)
    if quota <= 0:
        return
    try:
        used = server_key_tokens_today_sync(user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read usage for quota check: {e}")
        return
    if used >= quota:
        raise QuotaExceeded(used, quota)


# ── Reporting ─────────────────────────────────────────────────────────────


async def summary(days: int, user_id: str | None = None) -> dict[str, Any]:
    """Totals for the last ``days`` days: by day, by purpose and (for an
    admin, when ``user_id`` is None) by user."""
    from app.database import AsyncSessionLocal
    from app.models.usage import UsageEvent

    since = (datetime.now(UTC) - timedelta(days=max(1, days))).replace(tzinfo=None)
    where = [UsageEvent.created_at >= since]
    if user_id is not None:
        where.append(UsageEvent.user_id == user_id)
    async with AsyncSessionLocal() as db:
        total_tokens, calls = (
            await db.execute(
                select(
                    func.coalesce(func.sum(UsageEvent.total_tokens), 0),
                    func.count(UsageEvent.id),
                ).where(*where)
            )
        ).one()
        server_key_tokens = (
            await db.execute(
                select(func.coalesce(func.sum(UsageEvent.total_tokens), 0)).where(
                    *where, UsageEvent.server_key.is_(True)
                )
            )
        ).scalar()
        by_day_rows = (
            await db.execute(
                select(
                    func.date(UsageEvent.created_at),
                    func.coalesce(func.sum(UsageEvent.total_tokens), 0),
                )
                .where(*where)
                .group_by(func.date(UsageEvent.created_at))
                .order_by(func.date(UsageEvent.created_at))
            )
        ).all()
        by_purpose_rows = (
            await db.execute(
                select(UsageEvent.purpose, func.coalesce(func.sum(UsageEvent.total_tokens), 0))
                .where(*where)
                .group_by(UsageEvent.purpose)
            )
        ).all()
        by_user_rows = []
        if user_id is None:
            by_user_rows = (
                await db.execute(
                    select(
                        UsageEvent.user_id,
                        func.coalesce(func.sum(UsageEvent.total_tokens), 0),
                        func.count(UsageEvent.id),
                    )
                    .where(*where)
                    .group_by(UsageEvent.user_id)
                    .order_by(func.sum(UsageEvent.total_tokens).desc())
                )
            ).all()
    quota = int(settings.server_key_daily_token_quota or 0)
    out: dict[str, Any] = {
        "days": days,
        "total_tokens": int(total_tokens or 0),
        "server_key_tokens": int(server_key_tokens or 0),
        "calls": int(calls or 0),
        "by_day": [{"day": str(day), "tokens": int(tokens)} for day, tokens in by_day_rows],
        "by_purpose": {str(purpose): int(tokens) for purpose, tokens in by_purpose_rows},
        "quota_per_day": quota or None,
    }
    if user_id is None:
        out["by_user"] = [
            {"user_id": uid, "tokens": int(tokens), "calls": int(calls)}
            for uid, tokens, calls in by_user_rows
        ]
    else:
        out["server_key_tokens_today"] = await _server_key_tokens_today_async(user_id)
        out["remaining_today"] = max(quota - out["server_key_tokens_today"], 0) if quota else None
    return out


async def _server_key_tokens_today_async(user_id: str) -> int:
    from app.database import AsyncSessionLocal
    from app.models.usage import UsageEvent

    async with AsyncSessionLocal() as db:
        value = (
            await db.execute(
                select(func.coalesce(func.sum(UsageEvent.total_tokens), 0)).where(
                    UsageEvent.user_id == user_id,
                    UsageEvent.server_key.is_(True),
                    UsageEvent.created_at >= _day_start(),
                )
            )
        ).scalar()
        return int(value or 0)
