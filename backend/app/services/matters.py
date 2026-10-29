"""Matter membership & access service.

Central authority for "which matters can this user see?" and "is this user a
member of this matter?". Every resource-ownership check in the app resolves
through here so the isolation rule lives in exactly one place.

Design notes:
- A personal matter is created lazily the first time a user needs one. Solo
  users therefore keep working exactly as before — one user, one matter.
- ``get_member_matter_ids`` returns the full set a user may access; the vector
  store and SQL queries scope to that set (fail-closed: an empty set matches
  nothing, never everything).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.matters import (
    MATTER_ROLE_MEMBER,
    MATTER_ROLE_OWNER,
    Matter,
    MatterMember,
)


class MatterAccessError(Exception):
    """Raised when a user is not permitted to act on a matter."""


async def get_or_create_personal_matter(session: AsyncSession, user_id: str) -> Matter:
    """Return the user's personal matter, creating it (and membership) if absent."""
    existing = (
        await session.execute(
            select(Matter).where(Matter.owner_id == user_id, Matter.is_personal.is_(True))
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    matter = Matter(
        id=str(uuid.uuid4()),
        name="Personal",
        owner_id=user_id,
        is_personal=True,
    )
    session.add(matter)
    session.add(
        MatterMember(
            id=str(uuid.uuid4()),
            matter_id=matter.id,
            user_id=user_id,
            role=MATTER_ROLE_OWNER,
            added_by=user_id,
        )
    )
    await session.flush()
    return matter


async def create_matter(
    session: AsyncSession,
    owner_id: str,
    name: str,
    client_name: str | None = None,
) -> Matter:
    """Create a shared matter owned by ``owner_id`` and add them as owner-member."""
    matter = Matter(
        id=str(uuid.uuid4()),
        name=name.strip(),
        client_name=(client_name or "").strip() or None,
        owner_id=owner_id,
        is_personal=False,
    )
    session.add(matter)
    session.add(
        MatterMember(
            id=str(uuid.uuid4()),
            matter_id=matter.id,
            user_id=owner_id,
            role=MATTER_ROLE_OWNER,
            added_by=owner_id,
        )
    )
    await session.flush()
    return matter


async def update_matter(
    session: AsyncSession,
    matter: Matter,
    *,
    name: str | None = None,
    client_name: str | None = None,
) -> Matter:
    """Rename a matter and/or change its client name. ``None`` keeps a field.

    An empty client name clears it. Personal matters keep their fixed name.
    """
    if matter.is_personal:
        raise MatterAccessError("A personal matter cannot be renamed")
    values: dict[str, str | None] = {}
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise MatterAccessError("A matter needs a name")
        values["name"] = cleaned
    if client_name is not None:
        values["client_name"] = client_name.strip() or None
    if values:
        await session.execute(update(Matter).where(Matter.id == matter.id).values(**values))
        await session.refresh(matter)
    return matter


async def transfer_ownership(session: AsyncSession, matter: Matter, new_owner_id: str) -> Matter:
    """Make an existing member the owner; the previous owner stays as a member.

    Ownership decides who may share, rename, transfer and delete the matter
    and who may delete content filed under it, so the new owner must already
    be a member (added through the normal invite flow, which is audited and
    refuses deactivated accounts).
    """
    if matter.is_personal:
        raise MatterAccessError("A personal matter cannot be transferred")
    if new_owner_id == matter.owner_id:
        raise MatterAccessError("That user already owns this matter")
    member_ids = set(
        (
            await session.execute(
                select(MatterMember.user_id).where(MatterMember.matter_id == matter.id)
            )
        ).scalars()
    )
    if new_owner_id not in member_ids:
        raise MatterAccessError("The new owner must already be a member of this matter")
    await session.execute(
        update(MatterMember)
        .where(MatterMember.matter_id == matter.id, MatterMember.user_id == matter.owner_id)
        .values(role=MATTER_ROLE_MEMBER)
    )
    await session.execute(
        update(MatterMember)
        .where(MatterMember.matter_id == matter.id, MatterMember.user_id == new_owner_id)
        .values(role=MATTER_ROLE_OWNER)
    )
    await session.execute(
        update(Matter).where(Matter.id == matter.id).values(owner_id=new_owner_id)
    )
    await session.refresh(matter)
    return matter


async def get_matter(session: AsyncSession, matter_id: str) -> Matter | None:
    return (
        await session.execute(select(Matter).where(Matter.id == matter_id))
    ).scalar_one_or_none()


async def is_member(session: AsyncSession, matter_id: str, user_id: str) -> bool:
    """True if the user is a member of the matter."""
    if not matter_id or not user_id:
        return False
    row = (
        await session.execute(
            select(MatterMember.id).where(
                MatterMember.matter_id == matter_id,
                MatterMember.user_id == user_id,
            )
        )
    ).first()
    return row is not None


async def get_personal_matter_id(session: AsyncSession, user_id: str) -> str | None:
    """The id of the user's personal matter, or None if it was never created.

    A pure read: the personal matter is created when matters are listed, and
    a client can only name a matter it has been shown.
    """
    row = await session.execute(
        select(Matter.id).where(Matter.owner_id == user_id, Matter.is_personal.is_(True))
    )
    found = row.scalars().first()
    return str(found) if found else None


async def get_member_matter_ids(session: AsyncSession, user_id: str) -> list[str]:
    """All matter IDs the user is a member of.

    This is the set every scoped read filters against. It is a PURE query with
    no side effects — reads must never write (a personal matter is created
    lazily elsewhere, e.g. when the user creates/lists matters). An empty set is
    safe and fail-closed: it matches no shared matter, and the caller still sees
    their own content through the separate owner (user_id) check.
    """
    rows = (
        (
            await session.execute(
                select(MatterMember.matter_id).where(MatterMember.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def list_matters_for_user(session: AsyncSession, user_id: str) -> list[Matter]:
    """Matters the user is a member of, personal first then newest."""
    await get_or_create_personal_matter(session, user_id)
    matters = (
        (
            await session.execute(
                select(Matter)
                .join(MatterMember, MatterMember.matter_id == Matter.id)
                .where(MatterMember.user_id == user_id)
                .order_by(Matter.is_personal.desc(), Matter.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(matters)


async def list_members(session: AsyncSession, matter_id: str) -> list[MatterMember]:
    """All membership rows for a matter, owner first then join order."""
    rows = (
        (
            await session.execute(
                select(MatterMember)
                .where(MatterMember.matter_id == matter_id)
                .order_by(MatterMember.role.desc(), MatterMember.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def member_count(session: AsyncSession, matter_id: str) -> int:
    """Number of members in a matter."""
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(MatterMember)
                .where(MatterMember.matter_id == matter_id)
            )
        ).scalar_one()
    )


async def add_member(
    session: AsyncSession,
    matter_id: str,
    user_id: str,
    added_by: str,
    role: str = MATTER_ROLE_MEMBER,
) -> MatterMember:
    """Add a member to a matter. Idempotent; personal matters cannot be shared."""
    matter = await get_matter(session, matter_id)
    if matter is None:
        raise MatterAccessError("Matter not found")
    if matter.is_personal:
        raise MatterAccessError("A personal matter cannot be shared")

    existing = (
        await session.execute(
            select(MatterMember).where(
                MatterMember.matter_id == matter_id,
                MatterMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    member = MatterMember(
        id=str(uuid.uuid4()),
        matter_id=matter_id,
        user_id=user_id,
        role=role,
        added_by=added_by,
    )
    session.add(member)
    await session.flush()
    return member


async def remove_member(session: AsyncSession, matter_id: str, user_id: str) -> bool:
    """Remove a member. The matter owner cannot be removed. Returns True if removed."""
    matter = await get_matter(session, matter_id)
    if matter is None:
        return False
    if matter.owner_id == user_id:
        raise MatterAccessError("The matter owner cannot be removed")

    member = (
        await session.execute(
            select(MatterMember).where(
                MatterMember.matter_id == matter_id,
                MatterMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        return False
    await session.delete(member)
    await session.flush()
    return True
