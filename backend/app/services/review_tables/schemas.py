"""Saved review schemas: reusable field sets, personal or shared with a matter.

Built-in schemas come from ``templates.py`` and are never stored; a saved
schema is owned by whoever created it. A personal schema (no matter) is
visible to its owner only; one filed under a matter is visible to the
matter's members, and still edited and deleted by its owner alone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.models.review_tables import ReviewSchema


class ReviewSchemaService:
    @staticmethod
    def can_read(
        schema: ReviewSchema, user_id: str, accessible_matter_ids: set[str] | None
    ) -> bool:
        if schema.owner_id == user_id:
            return True
        return bool(
            accessible_matter_ids and schema.matter_id and schema.matter_id in accessible_matter_ids
        )

    @staticmethod
    def role_for(schema: ReviewSchema, user_id: str) -> str:
        return "owner" if schema.owner_id == user_id else "member"

    async def list_for_user(
        self, session: Any, user_id: str, accessible_matter_ids: set[str] | None
    ) -> list[ReviewSchema]:
        clause = ReviewSchema.owner_id == user_id
        if accessible_matter_ids:
            clause = clause | ReviewSchema.matter_id.in_(sorted(accessible_matter_ids))
        rows = await session.execute(
            select(ReviewSchema).where(clause).order_by(ReviewSchema.name.asc())
        )
        return list(rows.scalars().all())

    async def get_accessible(
        self,
        session: Any,
        schema_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None,
    ) -> ReviewSchema | None:
        schema: ReviewSchema | None = await session.get(ReviewSchema, schema_id)
        if schema is None or not self.can_read(schema, user_id, accessible_matter_ids):
            return None
        return schema

    async def create(
        self,
        session: Any,
        *,
        owner_id: str,
        matter_id: str | None,
        name: str,
        description: str | None,
        fields: list[dict[str, Any]],
    ) -> ReviewSchema:
        schema = ReviewSchema(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            matter_id=matter_id,
            name=name.strip(),
            description=(description or "").strip() or None,
            fields=fields,
            is_builtin=False,
        )
        session.add(schema)
        await session.commit()
        await session.refresh(schema)
        return schema

    async def update(
        self,
        session: Any,
        schema: ReviewSchema,
        *,
        name: str | None = None,
        description: str | None = None,
        fields: list[dict[str, Any]] | None = None,
        matter_id: Any = None,
        matter_given: bool = False,
    ) -> ReviewSchema:
        if name is not None:
            schema.name = name.strip()
        if description is not None:
            schema.description = description.strip() or None
        if fields is not None:
            schema.fields = fields
        if matter_given:
            schema.matter_id = matter_id
        schema.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(schema)
        return schema

    async def delete(self, session: Any, schema: ReviewSchema) -> None:
        await session.delete(schema)
        await session.commit()


review_schema_service = ReviewSchemaService()
