"""Deep research: persist runs, plan, and execute a plan as one job.

Access follows the other work-product services: the owner, or a member of the
run's matter, may read a run; the owner alone deletes it; anyone else is told
it does not exist. The client that researches is the caller's own (BYOK key,
else the instance key) — the same resolution research chat uses.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.deep_research import (
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RUNNING,
    DeepResearchRun,
)
from app.services.deep_research.pipeline import ResearchRunner, RunConfig, SynthesisError
from app.services.deep_research.planner import (
    TOOL_CASE_LAW,
    TOOL_DOCUMENTS,
    PlanningError,
    plan_research,
)
from app.services.llm_clients import make_openai, research_model

logger = logging.getLogger(__name__)


class DeepResearchError(Exception):
    """A run failed before producing an answer.

    Deliberately not a RuntimeError: the job queue retries RuntimeErrors, and a
    research run is far too expensive to re-run wholesale on a failed call.
    """


class DeepResearchService:
    # ------------------------------------------------------------------ access

    @staticmethod
    def can_read(
        run: DeepResearchRun, user_id: str, accessible_matter_ids: set[str] | None
    ) -> bool:
        if run.owner_id == user_id:
            return True
        return bool(
            accessible_matter_ids and run.matter_id and run.matter_id in accessible_matter_ids
        )

    @staticmethod
    def role_for(run: DeepResearchRun, user_id: str) -> str:
        return "owner" if run.owner_id == user_id else "member"

    @staticmethod
    def _llm_client(user_keys: Any) -> Any:
        """The caller's OpenAI-compatible client (BYOK key, else the instance key)."""
        key = user_keys.openai if user_keys and getattr(user_keys, "openai", None) else None
        client = make_openai(key or settings.openai_api_key, async_=True)
        if client is None:
            raise ValueError(
                "An OpenAI API key or a custom LLM endpoint is required. Configure one in Settings."
            )
        return client

    @staticmethod
    def caps() -> dict[str, int]:
        return {
            "max_subquestions": max(1, int(settings.deep_research_max_subquestions)),
            "max_tool_calls": max(1, int(settings.deep_research_max_tool_calls)),
            "max_passages": max(1, int(settings.deep_research_max_passages)),
            "followup_depth": max(0, int(settings.deep_research_followup_depth)),
            "concurrency": max(1, int(settings.deep_research_concurrency)),
        }

    @staticmethod
    def tools_available() -> dict[str, bool]:
        """Case law needs CourtListener; the citation check additionally needs a token."""
        return {TOOL_DOCUMENTS: True, TOOL_CASE_LAW: True}

    @staticmethod
    def citation_check_available() -> bool:
        return bool(getattr(settings, "courtlistener_api_token", None))

    # ---------------------------------------------------------------- planning

    async def plan(self, user_keys: Any, question: str) -> list[dict[str, Any]]:
        client = self._llm_client(user_keys)
        return await plan_research(
            client,
            question,
            available=self.tools_available(),
            max_subquestions=self.caps()["max_subquestions"],
        )

    # ------------------------------------------------------------- persistence

    async def create_run(
        self,
        session: Any,
        *,
        owner_id: str,
        matter_id: str | None,
        question: str,
        plan: list[dict[str, Any]],
        scope: dict[str, Any],
    ) -> DeepResearchRun:
        run = DeepResearchRun(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            matter_id=matter_id,
            question=question.strip(),
            plan=plan,
            scope=scope,
            status=RUN_RUNNING,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run

    async def set_job(self, session: Any, run_id: str, job_id: str) -> None:
        await session.execute(
            update(DeepResearchRun).where(DeepResearchRun.id == run_id).values(job_id=job_id)
        )
        await session.commit()

    async def list_for_user(
        self,
        session: Any,
        user_id: str,
        accessible_matter_ids: set[str] | None,
        matter_id: str | None = None,
        limit: int = 50,
    ) -> list[DeepResearchRun]:
        clause = DeepResearchRun.owner_id == user_id
        if accessible_matter_ids:
            clause = clause | DeepResearchRun.matter_id.in_(sorted(accessible_matter_ids))
        stmt = select(DeepResearchRun).where(clause)
        if matter_id:
            stmt = stmt.where(DeepResearchRun.matter_id == matter_id)
        rows = await session.execute(
            stmt.order_by(DeepResearchRun.created_at.desc()).limit(max(1, min(limit, 200)))
        )
        return list(rows.scalars().all())

    async def get_accessible(
        self,
        session: Any,
        run_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None,
    ) -> DeepResearchRun | None:
        run: DeepResearchRun | None = await session.get(DeepResearchRun, run_id)
        if run is None or not self.can_read(run, user_id, accessible_matter_ids):
            return None
        return run

    async def delete(self, session: Any, run: DeepResearchRun) -> None:
        await session.delete(run)
        await session.commit()

    async def _finish(
        self, run_id: str, status: str, result: dict[str, Any] | None, error: str | None
    ) -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(DeepResearchRun)
                .where(DeepResearchRun.id == run_id)
                .values(status=status, result=result, error=error, completed_at=datetime.utcnow())
            )
            await session.commit()

    # --------------------------------------------------------------------- run

    async def run(
        self,
        *,
        run_id: str,
        user_id: str,
        user_keys: Any,
        document_ids: list[str] | None,
        matter_ids: list[str] | None,
    ) -> dict[str, Any]:
        """Execute the stored plan; the job body. Persists the outcome whatever happens."""
        async with AsyncSessionLocal() as session:
            run = await session.get(DeepResearchRun, run_id)
            if run is None:
                raise LookupError("Deep research run not found")
            question, plan, matter_id = run.question, list(run.plan or []), run.matter_id

        caps = self.caps()
        config = RunConfig(
            user_id=user_id,
            user_keys=user_keys,
            document_ids=document_ids,
            matter_ids=matter_ids,
            concurrency=caps["concurrency"],
            max_tool_calls=caps["max_tool_calls"],
            max_passages=caps["max_passages"],
            followup_depth=caps["followup_depth"],
            case_law_available=self.tools_available()[TOOL_CASE_LAW],
            citation_check_available=self.citation_check_available(),
        )
        try:
            client = self._llm_client(user_keys)
            result = await ResearchRunner(client, config).run(question, plan)
        except asyncio.CancelledError:
            await self._finish(run_id, RUN_CANCELLED, None, "Cancelled before completion")
            raise
        except (SynthesisError, ValueError) as e:
            await self._finish(run_id, RUN_FAILED, None, str(e)[:500])
            raise DeepResearchError(str(e)) from e
        except Exception as e:
            logger.error("Deep research run %s failed: %s", run_id, e, exc_info=True)
            await self._finish(run_id, RUN_FAILED, None, f"{type(e).__name__}: {e}"[:500])
            raise DeepResearchError("The research run failed. Please try again.") from e

        result["scope"]["matter_id"] = matter_id
        result["run_id"] = run_id
        await self._finish(run_id, RUN_COMPLETED, result, None)
        return result

    @staticmethod
    def summary_counts(result: dict[str, Any] | None) -> dict[str, Any]:
        """Ids and counts only — what the completion audit entry carries."""
        if not result:
            return {}
        verification = result.get("verification") or {}
        scope = result.get("scope") or {}
        return {
            "sub_questions": len(result.get("sub_questions") or []),
            "sources_cited": len(result.get("sources") or []),
            "tool_calls": scope.get("tool_calls"),
            "passages_collected": scope.get("passages_collected"),
            "quotes_verified": verification.get("quotes_verified"),
            "quotes_unverified": verification.get("quotes_unverified"),
            "invalid_refs_stripped": verification.get("invalid_refs_stripped"),
            "citations_checked": verification.get("citations_checked"),
            "model": result.get("model") or research_model(),
        }


deep_research_service = DeepResearchService()
__all__ = ["DeepResearchError", "DeepResearchService", "PlanningError", "deep_research_service"]
