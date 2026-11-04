"""Ethical walls: users who must never see a matter.

A wall is an admin-managed deny-list entry on a matter. A walled user cannot
be a member (adding them is refused with the same generic answer as an
unknown address, so the wall itself is not disclosed to the inviter), their
existing membership is removed when the wall goes up, and any work they
filed under that matter — documents (with their index tags and stored
files), conversations, analyses, review tables, workflows — passes to the
matter's owner, so nothing in the matter stays reachable through ownership.
The matter is also left out of the admin oversight list for a walled admin.

Membership resolution (``matters.get_member_matter_ids``) subtracts walls
defensively as well, so even a membership row created around the wall would
grant nothing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_sessions import ChatSessionDB
from app.models.clause_intel import ContractAnalysisRun
from app.models.matters import Matter, MatterMember, MatterWall
from app.models.review_tables import ReviewTable
from app.models.workflows import Workflow

logger = logging.getLogger(__name__)


class WallError(Exception):
    """A wall change that is not allowed. Safe to show."""


async def walled_matter_ids(session: AsyncSession, user_id: str) -> set[str]:
    rows = await session.execute(select(MatterWall.matter_id).where(MatterWall.user_id == user_id))
    return {str(r) for r in rows.scalars().all()}


async def is_walled(session: AsyncSession, matter_id: str, user_id: str) -> bool:
    row = await session.execute(
        select(MatterWall.id).where(
            MatterWall.matter_id == matter_id, MatterWall.user_id == user_id
        )
    )
    return row.first() is not None


async def list_walls(session: AsyncSession, matter_id: str) -> list[MatterWall]:
    rows = await session.execute(
        select(MatterWall).where(MatterWall.matter_id == matter_id).order_by(MatterWall.created_at)
    )
    return list(rows.scalars().all())


async def add_wall(
    session: AsyncSession,
    matter: Matter,
    user_id: str,
    *,
    reason: str | None,
    created_by: str,
) -> tuple[MatterWall, dict[str, int]]:
    """Wall ``user_id`` off ``matter``: remove their membership and hand their
    work in the matter to its owner. Idempotent. Returns the wall and what
    was reassigned."""
    from app.services import offboarding

    if matter.is_personal:
        raise WallError("A personal matter cannot have an ethical wall")
    if user_id == matter.owner_id:
        raise WallError(
            "The matter's owner cannot be walled off. Transfer the matter to another member first."
        )
    existing = (
        await session.execute(
            select(MatterWall).where(
                MatterWall.matter_id == matter.id, MatterWall.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    moved: dict[str, int] = {}
    if existing is None:
        existing = MatterWall(
            id=str(uuid.uuid4()),
            matter_id=matter.id,
            user_id=user_id,
            reason=(reason or "").strip()[:500] or None,
            created_by=created_by,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(existing)
    # Membership goes, and so does every route to the matter's content through
    # ownership: their work in the matter now belongs to the matter's owner.
    member = (
        await session.execute(
            select(MatterMember).where(
                MatterMember.matter_id == matter.id, MatterMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if member is not None:
        await session.delete(member)
    moved["documents"] = await offboarding.transfer_documents(
        user_id, matter.owner_id, matter_id=matter.id
    )
    for key, model, owner_col in (
        ("chat_sessions", ChatSessionDB, ChatSessionDB.user_id),
        ("contract_analyses", ContractAnalysisRun, ContractAnalysisRun.user_id),
        ("review_tables", ReviewTable, ReviewTable.owner_id),
        ("workflows", Workflow, Workflow.owner_id),
    ):
        result = await session.execute(
            update(model)
            .where(owner_col == user_id, model.matter_id == matter.id)
            .values({owner_col.key: matter.owner_id})
        )
        moved[key] = int(result.rowcount or 0)
    await session.flush()
    return existing, moved


async def remove_wall(session: AsyncSession, matter_id: str, user_id: str) -> bool:
    """Lift a wall. Membership is not restored; the owner invites them again."""
    row = (
        await session.execute(
            select(MatterWall).where(
                MatterWall.matter_id == matter_id, MatterWall.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
