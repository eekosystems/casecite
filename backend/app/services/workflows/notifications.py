"""Email the people a workflow run concerns when it ends or needs them.

Events: ``completed``, ``failed``, ``awaiting_approval`` (sent to the
approvers as well as the runner) and ``rejected``. Sending is off unless
``WORKFLOW_NOTIFICATIONS`` is on and a SendGrid key is configured; each user
may opt out in Settings (``workflow_email_notifications``). The mail names
the workflow, the run, its status, the failed step and its error, and links
to the run; it never carries document text or step outputs. A delivery
failure is logged at warning level and never affects the run.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.auth import User
from app.models.workflows import Workflow, WorkflowRun
from app.services.email import _send_via_sendgrid
from app.services.user_settings import load_user_settings

logger = logging.getLogger(__name__)

EVENTS = ("completed", "failed", "awaiting_approval", "rejected")

_SUBJECTS = {
    "completed": "Workflow finished",
    "failed": "Workflow failed",
    "awaiting_approval": "Workflow needs your approval",
    "rejected": "Workflow rejected",
}


def enabled() -> bool:
    return bool(settings.workflow_notifications) and bool(
        getattr(settings, "sendgrid_api_key", None)
    )


def run_url(workflow_id: str, run_id: str) -> str:
    base = (settings.frontend_url or "").rstrip("/")
    return f"{base}/workflows/{workflow_id}?run={run_id}"


def build_html(
    *,
    event: str,
    workflow_name: str,
    run_started: str,
    status: str,
    url: str,
    failed_step: str | None = None,
    error: str | None = None,
    approval_title: str | None = None,
    note: str | None = None,
) -> str:
    """The message body. Every dynamic value is escaped; no document text is included."""
    name = html.escape(workflow_name)
    lines = [
        f"<p>The workflow <strong>{name}</strong> started {html.escape(run_started)} is "
        f"<strong>{html.escape(status.replace('_', ' '))}</strong>.</p>"
    ]
    if event == "awaiting_approval":
        title = html.escape(approval_title or "Approval")
        lines.append(
            f"<p>It is waiting at the step <strong>{title}</strong> for your decision.</p>"
        )
    if failed_step:
        lines.append(f"<p>Failed step: <strong>{html.escape(failed_step)}</strong></p>")
    if error:
        lines.append(f"<p>Error: {html.escape(error[:500])}</p>")
    if note:
        lines.append(f"<p>Note: {html.escape(note[:1000])}</p>")
    lines.append(f'<p><a href="{html.escape(url, quote=True)}" class="button">Open the run</a></p>')
    body = "\n".join(lines)
    return f"""<!DOCTYPE html>
<html><head><style>
body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
.container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
.button {{ display: inline-block; padding: 12px 24px; background: #1a365d; color: white; text-decoration: none; border-radius: 6px; }}
.footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
</style></head><body><div class="container">
<h1>{html.escape(_SUBJECTS.get(event, "Workflow update"))}</h1>
{body}
<div class="footer"><p>Every workflow output is model output. Verify it against the source documents and current law before relying on it.</p>
<p>&copy; {html.escape(settings.app_display_name)}. You can turn these emails off under Settings.</p></div>
</div></body></html>"""


async def _recipients(user_ids: list[str]) -> list[tuple[str, str]]:
    """(user id, email) for every id that exists and has not opted out."""
    ids = list(dict.fromkeys(i for i in user_ids if i))
    if not ids:
        return []
    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(User.id, User.email).where(User.id.in_(ids)))
        found = [(str(uid), str(email)) for uid, email in rows.all() if email]
    return [
        (uid, email) for uid, email in found if load_user_settings(uid).workflow_email_notifications
    ]


async def _deliver(to_email: str, subject: str, html_content: str) -> None:
    """One send, off the event loop. Raises on failure; the caller logs."""
    key = str(getattr(settings, "sendgrid_api_key", "") or "")
    await asyncio.to_thread(_send_via_sendgrid, key, to_email, subject, html_content)


async def notify_run_event(
    event: str,
    run_id: str,
    *,
    extra_user_ids: list[str] | None = None,
    note: str | None = None,
) -> int:
    """Send the event's email to the runner (and ``extra_user_ids``); the number sent.

    Never raises. Returns 0 when notifications are off, the run is unknown, or
    nobody is left after opt-outs.
    """
    if event not in EVENTS or not enabled():
        return 0
    try:
        async with AsyncSessionLocal() as session:
            run = await session.get(WorkflowRun, run_id)
            workflow = await session.get(Workflow, run.workflow_id) if run else None
            if run is None or workflow is None:
                return 0
            context = _context(event, run, workflow, note)
            user_ids = [run.runner_id, *(extra_user_ids or [])]
        recipients = await _recipients(user_ids)
        subject = f"[{settings.app_display_name}] {_SUBJECTS[event]}: {workflow.name}"[:200]
        sent = 0
        for _uid, email in recipients:
            try:
                await _deliver(email, subject, build_html(**context))
                sent += 1
            except Exception as e:  # SendGrid raises its own classes; delivery must never raise
                logger.warning("Workflow notification not sent (%s): %s", event, type(e).__name__)
        return sent
    except Exception as e:  # a database or settings problem must never surface into the run
        logger.warning("Workflow notification skipped (%s): %s", event, type(e).__name__)
        return 0


def _context(event: str, run: WorkflowRun, workflow: Workflow, note: str | None) -> dict[str, Any]:
    failed_step: str | None = None
    approval_title: str | None = None
    steps = list(run.steps or [])
    error = run.error or None
    if event == "failed" and error and error.startswith("Step "):
        # "Step 2/3 'review' failed: ..." -> review
        start, end = error.find("'"), error.find("'", error.find("'") + 1)
        if 0 <= start < end:
            failed_step = error[start + 1 : end]
    if event == "awaiting_approval":
        for step in steps:
            if step.get("kind") == "approval":
                approval_title = str((step.get("params") or {}).get("title") or step.get("name"))
    return {
        "event": event,
        "workflow_name": workflow.name,
        "run_started": run.created_at.strftime("%Y-%m-%d %H:%M UTC") if run.created_at else "",
        "status": run.status,
        "url": run_url(workflow.id, run.id),
        "failed_step": failed_step,
        "error": error if event in ("failed", "rejected") else None,
        "approval_title": approval_title,
        "note": note,
    }
