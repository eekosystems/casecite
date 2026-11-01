"""
Idempotent seeder for clause taxonomy + jurisdiction rules + contract-type
requirements. Computes and caches embeddings on first seed.

Called from app startup (lifespan).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clause_intel import (
    ClauseCanonical,
    ClauseJurisdictionRule,
    ContractTypeRequirement,
)
from app.services.clause_intel.jurisdiction_seed import JURISDICTION_RULES
from app.services.clause_intel.taxonomy_seed import (
    CANONICAL_CLAUSES,
    CONTRACT_TYPE_REQUIREMENTS,
)

logger = logging.getLogger(__name__)


def needs_embedding(row: Any, embedding_model: str | None) -> bool:
    """Missing, or embedded by another model (vectors from two models can't be compared).

    ``row`` is a loaded ClauseCanonical (typed Any: its columns read as plain values).
    """
    if not row.embedding:
        return True
    return bool(embedding_model) and row.embedding_model != embedding_model


async def seed_clause_intelligence(
    session: AsyncSession, embed_fn=None, embedding_model: str | None = None
) -> dict:
    """
    Seed canonicals, rules, and requirements.

    embed_fn: optional async callable list[str]->list[list[float]]; if provided
    and a canonical is missing an embedding, or has one from a model other than
    ``embedding_model``, it is computed and stored.
    """
    result = {
        "canonicals_inserted": 0,
        "canonicals_updated": 0,
        "embeddings_computed": 0,
        "rules_inserted": 0,
        "requirements_inserted": 0,
    }

    # ---- canonicals ----
    existing_rows = (await session.execute(select(ClauseCanonical))).scalars().all()
    by_slug = {c.slug: c for c in existing_rows}

    to_embed: list[tuple[ClauseCanonical, str]] = []

    for spec in CANONICAL_CLAUSES:
        row = by_slug.get(spec["slug"])
        if row is None:
            row = ClauseCanonical(
                slug=spec["slug"],
                name=spec["name"],
                category=spec["category"],
                description=spec.get("description"),
                sub_elements=spec.get("sub_elements", []),
                market_standard_text=spec["market_standard_text"],
                regex_anchors=spec.get("regex_anchors", []),
                required_keywords=spec.get("required_keywords", []),
            )
            session.add(row)
            result["canonicals_inserted"] += 1
        else:
            # Refresh non-embedding fields
            row.name = spec["name"]
            row.category = spec["category"]
            row.description = spec.get("description")
            row.sub_elements = spec.get("sub_elements", [])
            row.market_standard_text = spec["market_standard_text"]
            row.regex_anchors = spec.get("regex_anchors", [])
            row.required_keywords = spec.get("required_keywords", [])
            result["canonicals_updated"] += 1
        if embed_fn is not None and needs_embedding(row, embedding_model):
            to_embed.append((row, spec["market_standard_text"]))

    await session.flush()  # ensure rows have IDs before linking rules

    # ---- embeddings ----
    if to_embed and embed_fn is not None:
        try:
            texts = [t for _, t in to_embed]
            vectors = await embed_fn(texts)
            for (row, _), vec in zip(to_embed, vectors):
                row.embedding = vec
                row.embedding_model = embedding_model or "unknown"
                result["embeddings_computed"] += 1
        except Exception as e:
            logger.warning(f"Failed to compute canonical embeddings during seed: {e}")

    # ---- jurisdiction rules ----
    by_slug = {c.slug: c for c in (await session.execute(select(ClauseCanonical))).scalars().all()}
    existing_rules = (await session.execute(select(ClauseJurisdictionRule))).scalars().all()
    rule_index = {(r.canonical_id, r.jurisdiction): r for r in existing_rules}

    for spec in JURISDICTION_RULES:
        canonical = by_slug.get(spec["canonical_slug"])
        if canonical is None:
            logger.warning(
                f"Jurisdiction rule references unknown canonical slug {spec['canonical_slug']}"
            )
            continue
        key = (canonical.id, spec["jurisdiction"].upper())
        if key in rule_index:
            row = rule_index[key]
            row.enforceability = spec["enforceability"]
            row.constraints = spec.get("constraints", {})
            row.authorities = spec.get("authorities", [])
            row.note = spec.get("note")
            row.recommended_text = spec.get("recommended_text")
        else:
            session.add(
                ClauseJurisdictionRule(
                    canonical_id=canonical.id,
                    jurisdiction=spec["jurisdiction"].upper(),
                    enforceability=spec["enforceability"],
                    constraints=spec.get("constraints", {}),
                    authorities=spec.get("authorities", []),
                    note=spec.get("note"),
                    recommended_text=spec.get("recommended_text"),
                )
            )
            result["rules_inserted"] += 1

    # ---- contract-type requirements ----
    existing_reqs = (await session.execute(select(ContractTypeRequirement))).scalars().all()
    req_index = {(r.contract_type, r.canonical_slug): r for r in existing_reqs}

    for ct, sets in CONTRACT_TYPE_REQUIREMENTS.items():
        for slug in sets.get("required", []):
            key = (ct, slug)
            if key not in req_index:
                session.add(
                    ContractTypeRequirement(
                        contract_type=ct, canonical_slug=slug, requirement="required"
                    )
                )
                result["requirements_inserted"] += 1
        for slug in sets.get("recommended", []):
            key = (ct, slug)
            if key not in req_index:
                session.add(
                    ContractTypeRequirement(
                        contract_type=ct, canonical_slug=slug, requirement="recommended"
                    )
                )
                result["requirements_inserted"] += 1

    await session.commit()
    return result
