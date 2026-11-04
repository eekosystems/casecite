"""Offboarding: what a departing user owns, and handing it to a colleague.

Deleting an account used to destroy every matter, document, conversation
and analysis the user owned. For a firm that is client work product, not
personal data, so a delete now requires an explicit decision: transfer the
work to another user first, or purge it knowingly. This module is the
inventory and the transfer; ``routers/users.py`` makes the decision.

Transfer moves ownership of everything that is work product — documents
(registry row, encrypted file, vector and keyword index tags), owned shared
matters (the transferee becomes owner and a member), conversations, contract
analyses, review tables, saved review schemas and workflows. Personal data
(API keys, connector credentials, settings, MFA, sessions) is never
transferred; it is purged with the account.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ServiceUnavailableError
from app.models.chat_sessions import ChatSessionDB
from app.models.clause_intel import ContractAnalysisRun
from app.models.matters import MATTER_ROLE_MEMBER, MATTER_ROLE_OWNER, Matter, MatterMember
from app.models.review_tables import ReviewSchema, ReviewTable
from app.models.workflows import Workflow

logger = logging.getLogger(__name__)


@dataclass
class OwnedWork:
    """Counts of the work product a user owns (what a delete must decide about)."""

    shared_matters: int = 0
    documents: int = 0
    chat_sessions: int = 0
    contract_analyses: int = 0
    review_tables: int = 0
    review_schemas: int = 0
    workflows: int = 0

    @property
    def total(self) -> int:
        return sum(asdict(self).values())

    def as_dict(self) -> dict[str, int]:
        return {**asdict(self), "total": self.total}


class TransferError(ServiceUnavailableError):
    """A document's index tags could not be rewritten for the new owner, so
    the transfer stopped there. Documents already moved stay moved; the
    account is untouched. Safe to show the user."""


async def _count(session: AsyncSession, model, *where) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(model).where(*where))).scalar() or 0
    )


async def owned_work(session: AsyncSession, user_id: str) -> OwnedWork:
    """Inventory of what ``user_id`` owns, for the delete decision and the UI."""
    from app.services.documents import document_service

    return OwnedWork(
        shared_matters=await _count(
            session, Matter, Matter.owner_id == user_id, Matter.is_personal.is_(False)
        ),
        documents=sum(1 for d in document_service.documents.values() if d.user_id == user_id),
        chat_sessions=await _count(session, ChatSessionDB, ChatSessionDB.user_id == user_id),
        contract_analyses=await _count(
            session, ContractAnalysisRun, ContractAnalysisRun.user_id == user_id
        ),
        review_tables=await _count(session, ReviewTable, ReviewTable.owner_id == user_id),
        review_schemas=await _count(session, ReviewSchema, ReviewSchema.owner_id == user_id),
        workflows=await _count(session, Workflow, Workflow.owner_id == user_id),
    )


async def transfer_documents(from_user: str, to_user: str, matter_id: str | None = None) -> int:
    """Re-own every document of ``from_user`` (or only those filed under
    ``matter_id``): index tags first, then the encrypted file, then the
    registry row — each document all-or-nothing.

    Raises TransferError on the first document whose tags cannot be
    rewritten; the ones before it stay transferred, that one stays with the
    old owner, and nothing is deleted.
    """
    from app.services.documents import document_service
    from app.services.vectordb import get_vector_db

    vector_db = get_vector_db()
    moved = 0
    docs = [
        d
        for d in document_service.documents.values()
        if d.user_id == from_user and (matter_id is None or d.matter_id == matter_id)
    ]
    try:
        for doc in docs:
            if vector_db is not None and not await vector_db.update_document_owner(doc.id, to_user):
                raise TransferError(
                    f"'{doc.filename}' could not be transferred: its search index entries "
                    "could not be re-owned. Documents before it were transferred; the "
                    "account was not deleted. Try again."
                )
            src = document_service._stored_file_path(doc)
            if src:
                dst_dir = document_service._get_user_upload_dir(to_user)
                dst = os.path.join(dst_dir, os.path.basename(src))
                try:
                    os.replace(src, dst)
                except OSError as e:
                    # Put the tags back so index and registry agree on the owner.
                    if vector_db is not None:
                        await vector_db.update_document_owner(doc.id, from_user)
                    raise TransferError(
                        f"'{doc.filename}' could not be transferred: its stored file could "
                        f"not be moved ({e.strerror or e}). The account was not deleted."
                    ) from e
            doc.user_id = to_user
            moved += 1
    finally:
        if moved:
            document_service._save_index()
    return moved


async def transfer_matters(session: AsyncSession, from_user: str, to_user: str) -> int:
    """Make ``to_user`` the owner (and a member) of every shared matter
    ``from_user`` owns. The departing owner's membership rows are removed by
    the caller with the rest of the account."""
    import uuid

    matters = (
        (
            await session.execute(
                select(Matter).where(Matter.owner_id == from_user, Matter.is_personal.is_(False))
            )
        )
        .scalars()
        .all()
    )
    for matter in matters:
        existing = (
            await session.execute(
                select(MatterMember).where(
                    MatterMember.matter_id == matter.id, MatterMember.user_id == to_user
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                MatterMember(
                    id=str(uuid.uuid4()),
                    matter_id=matter.id,
                    user_id=to_user,
                    role=MATTER_ROLE_OWNER,
                    added_by=from_user,
                )
            )
        else:
            existing.role = MATTER_ROLE_OWNER
        await session.execute(
            update(MatterMember)
            .where(MatterMember.matter_id == matter.id, MatterMember.user_id == from_user)
            .values(role=MATTER_ROLE_MEMBER)
        )
        matter.owner_id = to_user
    await session.flush()
    return len(matters)


async def transfer_rows(session: AsyncSession, from_user: str, to_user: str) -> dict[str, int]:
    """Re-own the relational work product: conversations, analyses, review
    tables, saved schemas and workflows."""
    counts: dict[str, int] = {}
    for key, model, column in (
        ("chat_sessions", ChatSessionDB, ChatSessionDB.user_id),
        ("contract_analyses", ContractAnalysisRun, ContractAnalysisRun.user_id),
        ("review_tables", ReviewTable, ReviewTable.owner_id),
        ("review_schemas", ReviewSchema, ReviewSchema.owner_id),
        ("workflows", Workflow, Workflow.owner_id),
    ):
        result = await session.execute(
            update(model).where(column == from_user).values({column.key: to_user})
        )
        counts[key] = int(result.rowcount or 0)
    return counts


async def transfer_all(session: AsyncSession, from_user: str, to_user: str) -> dict[str, int]:
    """Hand everything ``from_user`` owns to ``to_user``. Documents go first
    (the only step that can fail part-way); the relational rows follow in
    the caller's transaction."""
    documents = await transfer_documents(from_user, to_user)
    matters = await transfer_matters(session, from_user, to_user)
    rows = await transfer_rows(session, from_user, to_user)
    return {"documents": documents, "shared_matters": matters, **rows}
