"""Shared FastAPI dependency for matter-scoped access.

Resolves the set of matters the caller may access (their personal matter plus
any shared matters they belong to). Injected into resource reads so a member
sees content shared with them via a matter. Depends only on authentication —
each endpoint's own require_permission still gates the action.
"""

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.matters import Matter
from app.services import matters as matters_service
from app.services.auth import get_current_user


async def accessible_matter_ids(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> set[str]:
    return set(await matters_service.get_member_matter_ids(db, current_user.user_id))


async def personal_matter_id(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> str | None:
    """The caller's personal matter (their unfiled documents), if it exists."""
    return await matters_service.get_personal_matter_id(db, current_user.user_id)


async def owned_matter_ids(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> set[str]:
    """Matters the caller OWNS — the scope for destructive actions.

    Membership grants read access to a matter's content; deleting someone
    else's document or chat is reserved for the content's owner and the
    matter's owner, so delete endpoints scope by this set rather than by
    ``accessible_matter_ids``.
    """
    rows = await db.execute(select(Matter.id).where(Matter.owner_id == current_user.user_id))
    return set(rows.scalars().all())
