"""Matter (collaboration / sharing) endpoints.

Lets a user create matters, see the matters they belong to, and — as the matter
owner — invite or remove colleagues by email. Membership is what grants access
to a matter's documents, chats, and analyses (see app/services/matters.py).

Creating, sharing and deleting a matter decides who can read privileged
documents, so those actions require the ``matters.manage`` permission and every
one of them is written to the audit trail. Reading the matters you belong to
only requires authentication.

Thin HTTP layer: all membership logic lives in the service. Literal routes are
defined before the parameterized /{matter_id} catch-alls.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import BadRequestError, NotFoundError, ServiceUnavailableError
from app.models.audit import AuditLog as AuditLogDB
from app.models.auth import User as DBUser
from app.models.clause_intel import ContractAnalysisRun
from app.models.matters import Matter
from app.models.schemas import (
    AddMatterMemberRequest,
    ChatSessionListResponse,
    CreateMatterRequest,
    DocumentList,
    MatterActivityEvent,
    MatterActivityResponse,
    MatterAnalysesResponse,
    MatterAnalysisItem,
    MatterDetailResponse,
    MatterFactsRequest,
    MatterFactsResponse,
    MatterListResponse,
    MatterMemberResponse,
    MatterResponse,
    MoveMatterDocumentsRequest,
    MoveMatterDocumentsResponse,
    TransferMatterRequest,
    UpdateMatterRequest,
)
from app.routers._matter_deps import owned_matter_ids as _owned_matter_ids
from app.services import chat_sessions as chat_session_service
from app.services import matters as matters_service
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData, get_current_user
from app.services.documents import DocumentMoveError, document_service
from app.services.permissions import require_permission
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/matters", tags=["matters"])


async def _audit(
    request: Request,
    current_user: TokenData,
    event_type: AuditEventType,
    matter_id: str,
    details: dict,
    success: bool = True,
) -> None:
    """Audit a matter change. Ids only — matter and client names are confidential."""
    await audit_service.log_event(
        event_type=event_type,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="matter",
        resource_id=matter_id,
        ip_address=get_client_ip(request),
        details=details,
        success=success,
    )


async def _to_response(
    db: AsyncSession, matter: Matter, caller_id: str, role: str | None = None
) -> MatterResponse:
    role = role or ("owner" if matter.owner_id == caller_id else "member")
    return MatterResponse(
        id=matter.id,
        name=matter.name,
        client_name=matter.client_name,
        is_personal=matter.is_personal,
        owner_id=matter.owner_id,
        role=role,
        member_count=await matters_service.member_count(db, matter.id),
        created_at=matter.created_at,
    )


async def _members_with_identity(db: AsyncSession, matter_id: str) -> list[MatterMemberResponse]:
    members = await matters_service.list_members(db, matter_id)
    user_ids = [m.user_id for m in members]
    identities: dict[str, DBUser] = {}
    if user_ids:
        rows = (await db.execute(select(DBUser).where(DBUser.id.in_(user_ids)))).scalars().all()
        identities = {u.id: u for u in rows}
    out = []
    for m in members:
        u = identities.get(m.user_id)
        out.append(
            MatterMemberResponse(
                user_id=m.user_id,
                email=u.email if u else None,
                name=u.name if u else None,
                role=m.role,
            )
        )
    return out


_ADD_MEMBER_FAILED = (
    "That person could not be added. Check the email address, and that they "
    "have an active account on this instance."
)


async def _require_owned_matter(db: AsyncSession, matter_id: str, caller_id: str) -> Matter:
    """Return the matter only if the caller is its owner; else 404 (no existence leak)."""
    matter = await matters_service.get_matter(db, matter_id)
    if matter is None or matter.owner_id != caller_id:
        raise NotFoundError("Matter not found")
    return matter


async def _require_member_matter(db: AsyncSession, matter_id: str, caller_id: str) -> Matter:
    """Return the matter only if the caller is a member; else 404 (no existence leak)."""
    if not await matters_service.is_member(db, matter_id, caller_id):
        raise NotFoundError("Matter not found")
    matter = await matters_service.get_matter(db, matter_id)
    if matter is None:
        raise NotFoundError("Matter not found")
    return matter


def _personal_owner(matter: Matter) -> str | None:
    """The owner id for personal-scope lookups, or None for a shared matter.

    Content in the personal scope predates matters and carries no matter_id,
    so a personal matter's workspace must also gather its owner's untagged
    documents, chats and analyses.
    """
    return str(matter.owner_id) if matter.is_personal else None


def _facts_fields(matter: Matter | None) -> dict[str, Any]:
    """A matter's facts fields for a response (columns read as plain values)."""
    if matter is None:
        return {}
    return {
        "facts": matter.facts,
        "facts_updated_at": matter.facts_updated_at,
        "facts_updated_by": matter.facts_updated_by,
    }


async def _detail_response(
    db: AsyncSession, matter: Matter, matter_id: str, caller_id: str
) -> MatterDetailResponse:
    base = await _to_response(db, matter, caller_id)
    return MatterDetailResponse(
        **base.model_dump(),
        members=await _members_with_identity(db, matter_id),
        **_facts_fields(matter),
    )


@router.get("", response_model=MatterListResponse)
async def list_matters(
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatterListResponse:
    """List the matters the caller belongs to (personal first, then newest)."""
    matters = await matters_service.list_matters_for_user(db, current_user.user_id)
    await db.commit()  # list_matters_for_user may have created the personal matter
    return MatterListResponse(
        matters=[await _to_response(db, m, current_user.user_id) for m in matters]
    )


@router.get("/all", response_model=MatterListResponse)
async def list_all_matters(
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
    db: AsyncSession = Depends(get_db),
) -> MatterListResponse:
    """Admin oversight: every shared matter on the instance (newest first).

    Lets an administrator review who has set up sharing — names, owners and
    member counts — without granting access to any matter's content. Personal
    matters are private workspaces and are not listed. ``role`` is the admin's
    own role in each matter ("owner", "member", or "none").
    """
    rows = await db.execute(
        select(Matter).where(Matter.is_personal.is_(False)).order_by(Matter.created_at.desc())
    )
    mine = set(await matters_service.get_member_matter_ids(db, current_user.user_id))
    out = []
    for matter in rows.scalars().all():
        role = None if matter.id in mine else "none"
        out.append(await _to_response(db, matter, current_user.user_id, role=role))
    await audit_service.log_event(
        event_type=AuditEventType.DATA_ACCESS,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="matter",
        ip_address=get_client_ip(request),
        details={"action": "admin_list_all_matters", "count": len(out)},
    )
    return MatterListResponse(matters=out)


@router.post("", response_model=MatterResponse, status_code=201)
async def create_matter(
    body: CreateMatterRequest,
    request: Request,
    current_user: TokenData = require_permission("matters.manage"),
    db: AsyncSession = Depends(get_db),
) -> MatterResponse:
    """Create a shared matter owned by the caller."""
    matter = await matters_service.create_matter(
        db, owner_id=current_user.user_id, name=body.name, client_name=body.client_name
    )
    await db.commit()
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        matter.id,
        {"action": "matter_created"},
    )
    return await _to_response(db, matter, current_user.user_id)


@router.get("/{matter_id}", response_model=MatterDetailResponse)
async def get_matter(
    matter_id: str,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatterDetailResponse:
    """A matter with its members. Only accessible to members (else 404)."""
    if not await matters_service.is_member(db, matter_id, current_user.user_id):
        raise NotFoundError("Matter not found")
    matter = await matters_service.get_matter(db, matter_id)
    base = await _to_response(db, matter, current_user.user_id)
    return MatterDetailResponse(
        **base.model_dump(),
        members=await _members_with_identity(db, matter_id),
        **_facts_fields(matter),
    )


@router.put("/{matter_id}/facts", response_model=MatterFactsResponse)
async def set_matter_facts(
    matter_id: str,
    body: MatterFactsRequest,
    request: Request,
    current_user: TokenData = require_permission("documents.upload"),
    db: AsyncSession = Depends(get_db),
) -> MatterFactsResponse:
    """Replace the matter's facts. Any member who may add content to it may edit them.

    Every chat and strategy brief in the matter reads these facts as the case
    team's account, so the audit trail records who changed them, never the text.
    """
    matter = await _require_member_matter(db, matter_id, current_user.user_id)
    await matters_service.set_matter_facts(db, matter, body.facts, current_user.user_id)
    await db.commit()
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        matter_id,
        {"action": "matter_facts_updated", "length": len(matter.facts or "")},
    )
    return MatterFactsResponse(**_facts_fields(matter))


@router.post("/{matter_id}/members", response_model=MatterDetailResponse)
async def add_matter_member(
    matter_id: str,
    body: AddMatterMemberRequest,
    request: Request,
    current_user: TokenData = require_permission("matters.manage"),
    db: AsyncSession = Depends(get_db),
) -> MatterDetailResponse:
    """Invite a colleague (by email) to a matter. Owner only."""
    matter = await _require_owned_matter(db, matter_id, current_user.user_id)
    if matter.is_personal:
        raise BadRequestError("A personal matter cannot be shared.")

    email = body.email.lower().strip()
    invitee = (
        await db.execute(select(DBUser).where(DBUser.email == email, DBUser.is_active.is_(True)))
    ).scalar_one_or_none()
    if invitee is None:
        # One generic answer for "no such account", "deactivated account" and a
        # mistyped address, so this endpoint cannot be used to probe which email
        # addresses have accounts. The attempt is audited.
        await _audit(
            request,
            current_user,
            AuditEventType.DATA_MODIFICATION,
            matter_id,
            {"action": "matter_member_add_failed"},
            success=False,
        )
        raise BadRequestError(_ADD_MEMBER_FAILED)

    try:
        await matters_service.add_member(db, matter_id, invitee.id, added_by=current_user.user_id)
    except matters_service.MatterAccessError as e:
        raise BadRequestError(str(e))
    await db.commit()
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        matter_id,
        {"action": "matter_member_added", "member_user_id": invitee.id},
    )

    base = await _to_response(db, matter, current_user.user_id)
    return MatterDetailResponse(
        **base.model_dump(),
        members=await _members_with_identity(db, matter_id),
        **_facts_fields(matter),
    )


@router.delete("/{matter_id}/members/{user_id}", response_model=MatterDetailResponse)
async def remove_matter_member(
    matter_id: str,
    user_id: str,
    request: Request,
    current_user: TokenData = require_permission("matters.manage"),
    db: AsyncSession = Depends(get_db),
) -> MatterDetailResponse:
    """Remove a member from a matter. Owner only; the owner cannot be removed."""
    matter = await _require_owned_matter(db, matter_id, current_user.user_id)
    try:
        removed = await matters_service.remove_member(db, matter_id, user_id)
    except matters_service.MatterAccessError as e:
        raise BadRequestError(str(e))
    if not removed:
        raise NotFoundError("That user is not a member of this matter.")
    await db.commit()
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        matter_id,
        {"action": "matter_member_removed", "member_user_id": user_id},
    )

    base = await _to_response(db, matter, current_user.user_id)
    return MatterDetailResponse(
        **base.model_dump(),
        members=await _members_with_identity(db, matter_id),
        **_facts_fields(matter),
    )


@router.delete("/{matter_id}")
async def delete_matter(
    matter_id: str,
    request: Request,
    current_user: TokenData = require_permission("matters.manage"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a matter and its memberships. Owner only; personal matters can't be deleted.

    Content filed under the matter (documents, chats, analyses) is NOT deleted;
    it reverts to owner-only visibility as its matter no longer exists.
    """
    matter = await _require_owned_matter(db, matter_id, current_user.user_id)
    if matter.is_personal:
        raise BadRequestError("A personal matter cannot be deleted.")
    await db.delete(matter)  # memberships cascade via MatterMember.matter_id FK
    await db.commit()
    await _audit(
        request, current_user, AuditEventType.DATA_DELETION, matter_id, {"action": "matter_deleted"}
    )
    return {"status": "deleted", "id": matter_id}


@router.patch("/{matter_id}", response_model=MatterResponse)
async def update_matter(
    matter_id: str,
    body: UpdateMatterRequest,
    request: Request,
    current_user: TokenData = require_permission("matters.manage"),
    db: AsyncSession = Depends(get_db),
) -> MatterResponse:
    """Rename a matter or change its client name. Owner only."""
    if body.name is None and body.client_name is None:
        raise BadRequestError("Nothing to change: send a name and/or a client_name.")
    matter = await _require_owned_matter(db, matter_id, current_user.user_id)
    try:
        await matters_service.update_matter(
            db, matter, name=body.name, client_name=body.client_name
        )
    except matters_service.MatterAccessError as e:
        raise BadRequestError(str(e))
    await db.commit()
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        matter_id,
        {
            "action": "matter_updated",
            "fields": [f for f in ("name", "client_name") if getattr(body, f) is not None],
        },
    )
    return await _to_response(db, matter, current_user.user_id)


@router.post("/{matter_id}/transfer", response_model=MatterDetailResponse)
async def transfer_matter(
    matter_id: str,
    body: TransferMatterRequest,
    request: Request,
    current_user: TokenData = require_permission("matters.manage"),
    db: AsyncSession = Depends(get_db),
) -> MatterDetailResponse:
    """Hand the matter to another member. Owner only; the caller stays a member.

    This is the offboarding path: transfer first, then deactivate or delete
    the departing owner, and the matter and everything filed under it stays
    with the firm.
    """
    matter = await _require_owned_matter(db, matter_id, current_user.user_id)
    try:
        await matters_service.transfer_ownership(db, matter, body.new_owner_id)
    except matters_service.MatterAccessError as e:
        raise BadRequestError(str(e))
    await db.commit()
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        matter_id,
        {"action": "matter_ownership_transferred", "new_owner_id": body.new_owner_id},
    )
    return await _detail_response(db, matter, matter_id, current_user.user_id)


@router.get("/{matter_id}/documents", response_model=DocumentList)
async def list_matter_documents(
    matter_id: str,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentList:
    """Every document filed under the matter (members only, unpaged)."""
    matter = await _require_member_matter(db, matter_id, current_user.user_id)
    docs = document_service.documents_in_matter(matter_id, _personal_owner(matter))
    return DocumentList(documents=docs, count=len(docs), total=len(docs), limit=None, offset=0)


@router.post("/{matter_id}/documents/move", response_model=MoveMatterDocumentsResponse)
async def move_documents_to_matter(
    matter_id: str,
    body: MoveMatterDocumentsRequest,
    request: Request,
    current_user: TokenData = require_permission("documents.upload"),
    db: AsyncSession = Depends(get_db),
    owned_matters: set[str] = Depends(_owned_matter_ids),
) -> MoveMatterDocumentsResponse:
    """File documents under this matter (bulk move between matters).

    The destination must be a matter the caller belongs to. Each document
    must be the caller's own, or filed under a matter the caller OWNS: the
    same rule as deleting it, since re-filing decides who may read it. Any
    document outside that rule answers 404 and nothing is moved. Moving into
    the caller's personal matter returns a document to owner-only visibility.
    """
    matter = await _require_member_matter(db, matter_id, current_user.user_id)
    docs = []
    for doc_id in dict.fromkeys(body.document_ids):  # de-duplicated, order kept
        doc = document_service.documents.get(doc_id)
        if doc is None or not (
            doc.user_id == current_user.user_id
            or (doc.matter_id and doc.matter_id in owned_matters)
        ):
            raise NotFoundError("Document not found")
        docs.append(doc)
    target = None if matter.is_personal else matter_id
    try:
        moved = await document_service.move_documents_to_matter(docs, target)
    except DocumentMoveError as e:
        raise ServiceUnavailableError(str(e))
    for doc_id in moved:
        await audit_service.log_event(
            event_type=AuditEventType.DATA_MODIFICATION,
            user_id=current_user.user_id,
            user_email=current_user.email,
            resource_type="document",
            resource_id=doc_id,
            ip_address=get_client_ip(request),
            details={"action": "document_moved_to_matter", "matter_id": matter_id},
        )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        matter_id,
        {"action": "matter_documents_moved", "count": len(moved)},
    )
    return MoveMatterDocumentsResponse(matter_id=matter_id, moved=moved)


@router.get("/{matter_id}/chats", response_model=ChatSessionListResponse)
async def list_matter_chats(
    matter_id: str,
    limit: int = Query(50, ge=1, le=200),
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionListResponse:
    """Chat sessions filed under the matter, newest first (members only).

    Colleagues' sessions are listed read-only; each carries its ``user_id``
    so the UI can say whose it is.
    """
    matter = await _require_member_matter(db, matter_id, current_user.user_id)
    sessions = await chat_session_service.list_sessions_for_matter(
        matter_id, _personal_owner(matter), limit=limit
    )
    return ChatSessionListResponse.model_validate({"sessions": sessions})


def _analysis_scope(matter: Matter, matter_id: str):
    """Runs filed under the matter; for a personal matter also the owner's untagged runs."""
    scope = ContractAnalysisRun.matter_id == matter_id
    personal_owner = _personal_owner(matter)
    if personal_owner:
        scope = or_(
            scope,
            (ContractAnalysisRun.user_id == personal_owner)
            & ContractAnalysisRun.matter_id.is_(None),
        )
    return scope


@router.get("/{matter_id}/analyses", response_model=MatterAnalysesResponse)
async def list_matter_analyses(
    matter_id: str,
    limit: int = Query(50, ge=1, le=200),
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatterAnalysesResponse:
    """Contract analyses filed under the matter, newest first (members only)."""
    matter = await _require_member_matter(db, matter_id, current_user.user_id)
    rows = await db.execute(
        select(
            ContractAnalysisRun.id,
            ContractAnalysisRun.document_id,
            ContractAnalysisRun.contract_type,
            ContractAnalysisRun.created_at,
            ContractAnalysisRun.summary,
            ContractAnalysisRun.user_id,
        )
        .where(_analysis_scope(matter, matter_id))
        .order_by(ContractAnalysisRun.created_at.desc())
        .limit(limit)
    )
    items = []
    for run_id, document_id, contract_type, created_at, summary, user_id in rows.all():
        doc = document_service.documents.get(document_id) if document_id else None
        issues = (summary or {}).get("issues") if isinstance(summary, dict) else None
        items.append(
            MatterAnalysisItem(
                analysis_id=run_id,
                document_id=document_id,
                filename=doc.filename if doc else None,
                contract_type=contract_type,
                created_at=created_at,
                issues=len(issues) if isinstance(issues, list) else 0,
                user_id=user_id,
            )
        )
    return MatterAnalysesResponse(analyses=items)


_ACTIVITY_LIMIT_MAX = 200
# Bound on the ids placed in one IN clause (a matter with more documents shows
# the newest ones' activity; the matter-level entries are always complete).
_IDS_PER_IN_CLAUSE = 500


@router.get("/{matter_id}/activity", response_model=MatterActivityResponse)
async def matter_activity(
    matter_id: str,
    limit: int = Query(50, ge=1, le=_ACTIVITY_LIMIT_MAX),
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatterActivityResponse:
    """What happened in this matter, newest first (members only).

    Drawn from the audit trail's database copy: entries about the matter
    itself (created, shared, renamed, transferred, documents moved) and about
    the documents and contract analyses currently filed under it. Emails and
    IPs are already masked in that copy. Reading the feed is not itself
    audited, in line with the other member reads of a matter.
    """
    matter = await _require_member_matter(db, matter_id, current_user.user_id)
    doc_ids = [
        d.id for d in document_service.documents_in_matter(matter_id, _personal_owner(matter))
    ]
    run_ids = list(
        (
            await db.execute(
                select(ContractAnalysisRun.id).where(_analysis_scope(matter, matter_id))
            )
        ).scalars()
    )

    clauses = [(AuditLogDB.resource_type == "matter") & (AuditLogDB.resource_id == matter_id)]
    if doc_ids:
        clauses.append(
            (AuditLogDB.resource_type == "document")
            & AuditLogDB.resource_id.in_(doc_ids[:_IDS_PER_IN_CLAUSE])
        )
    if run_ids:
        clauses.append(
            (AuditLogDB.resource_type == "contract_analysis")
            & AuditLogDB.resource_id.in_(run_ids[:_IDS_PER_IN_CLAUSE])
        )
    rows = await db.execute(
        select(
            AuditLogDB.id,
            AuditLogDB.timestamp,
            AuditLogDB.event_type,
            AuditLogDB.action_details,
            AuditLogDB.user_id,
            AuditLogDB.user_email,
            AuditLogDB.resource_type,
            AuditLogDB.resource_id,
            AuditLogDB.success,
        )
        .where(or_(*clauses))
        .order_by(AuditLogDB.timestamp.desc(), AuditLogDB.id.desc())
        .limit(limit)
    )
    events = []
    for row in rows.all():
        details = row.action_details if isinstance(row.action_details, dict) else {}
        action = details.get("action")
        events.append(
            MatterActivityEvent(
                id=row.id,
                timestamp=row.timestamp,
                event_type=row.event_type,
                action=str(action) if action is not None else None,
                user_id=row.user_id,
                user_email=row.user_email,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                success=bool(row.success) if row.success is not None else True,
            )
        )
    return MatterActivityResponse(events=events)
