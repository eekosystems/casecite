"""
Direction-aware unified issues report.

Turns the pipeline's structured findings (clause deviations, missing clauses,
jurisdiction flags, risk findings, and the general-purpose LLM contract
analysis) into a single lawyer-facing issues list, framed for the side of the
table the user represents.

Design:
  - collect_findings()  — pure; assigns stable server-side refs (dev-0, miss-2,
                          gen-1, ...)
  - merge_issues()      — pure; joins LLM annotations back by ref, attaches
                          spans/slugs deterministically, sorts by severity,
                          and mechanically covers anything the LLM dropped
  - detect_contract_type() / annotate_issues() — the two utility-LLM calls

The LLM never produces offsets and can never break the endpoint: any parse
or transport failure degrades to the mechanical issue list.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.services.clause_intel.taxonomy_seed import CONTRACT_TYPE_REQUIREMENTS
from app.services.contract_analysis.instructions import bounded_instructions
from app.services.llm_clients import openai_chat, utility_model
from app.services.quote_verify import find_quote_offset
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

# Authoritative contract-type list (nda / msa / saas / employment / license / sow).
KNOWN_CONTRACT_TYPES: list[str] = sorted(CONTRACT_TYPE_REQUIREMENTS)

# Unknown types are tolerated downstream (missing-clause detector returns
# confidence 0.5 and no requirements), so "other" is a safe generic bucket.
FALLBACK_CONTRACT_TYPE = "other"

VALID_STATUSES = {"off_market", "missing", "unusual", "info"}
VALID_SEVERITIES = {"critical", "major", "minor"}

_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}
_MATCHED_TEXT_LIMIT = 200
# A jurisdiction note ends with its as-of caveat; never cut that off.
_JURISDICTION_NOTE_LIMIT = 700
# Only the contract-TYPE classifier reads this much (the title, recitals and
# opening clauses identify the type). It is not an analysis cap: issue
# detection reads the whole document (see ai_review).
_DETECT_TEXT_LIMIT = 6000

# Mechanical status when the LLM gives none / an invalid one.
_STATUS_BY_KIND = {
    "ai": "unusual",
    "deviation": "off_market",
    "missing": "missing",
    "jurisdiction": "unusual",
    "risk": "unusual",
    "general_risk": "unusual",
    "general_missing": "missing",
    "general_unusual": "unusual",
}

# General-analyzer vocabularies → issues severities.
_GENERAL_RISK_SEVERITY = {"high": "critical", "medium": "major", "low": "minor"}
# Mirrors the rules engine: required clauses are "major", the rest "minor".
_GENERAL_IMPORTANCE_SEVERITY = {"critical": "major", "recommended": "minor", "optional": "minor"}


def _severity(value: Any, default: str = "minor") -> str:
    sev = str(value or "").strip().lower()
    if sev == "informational":  # jurisdiction flags use this
        return "minor"
    return sev if sev in VALID_SEVERITIES else default


def collect_findings(
    clause_result: dict[str, Any],
    general_analysis: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Flatten pipeline findings into ref-addressable dicts.

    Refs are assigned server-side and are the only join key the LLM echoes
    back; spans/slugs stay attached here and never round-trip through the LLM.

    `general_analysis` is the general-purpose LLM contract analysis
    (document_analyzer.analyze_contract): its risks / missing_clauses /
    unusual_provisions become "gen-N" findings so documents outside the seeded
    taxonomy still produce a substantive issues report.
    """
    findings: list[dict[str, Any]] = []

    for i, d in enumerate(clause_result.get("deviations") or []):
        slug = d.get("canonical_slug")
        dev_type = d.get("deviation_type") or "deviation"
        sub = d.get("sub_element")
        name = f"{slug}: {dev_type}" + (f" ({sub})" if sub else "")
        findings.append(
            {
                "ref": f"dev-{i}",
                "kind": "deviation",
                "name": name,
                "severity": _severity(d.get("severity"), "major"),
                "matched_text": (d.get("matched_text") or "")[:_MATCHED_TEXT_LIMIT],
                "span_start": d.get("span_start"),
                "span_end": d.get("span_end"),
                "clause_slug": slug,
            }
        )

    missing = clause_result.get("missing") or {}
    idx = 0
    for bucket, sev in (("missing_required", "major"), ("missing_recommended", "minor")):
        for m in missing.get(bucket) or []:
            findings.append(
                {
                    "ref": f"miss-{idx}",
                    "kind": "missing",
                    "name": f"Missing clause: {m.get('name') or m.get('canonical_slug')}",
                    "severity": sev,
                    "matched_text": "",
                    "span_start": None,
                    "span_end": None,
                    "clause_slug": m.get("canonical_slug"),
                }
            )
            idx += 1

    for i, f in enumerate(clause_result.get("jurisdiction_flags") or []):
        slug = f.get("canonical_slug")
        findings.append(
            {
                "ref": f"jur-{i}",
                "kind": "jurisdiction",
                "name": (f.get("note") or f"Jurisdiction issue: {slug}")[:_JURISDICTION_NOTE_LIMIT],
                "severity": _severity(f.get("severity"), "major"),
                "matched_text": "",
                "span_start": None,
                "span_end": None,
                "clause_slug": slug,
            }
        )

    if general_analysis:
        findings.extend(_general_findings(general_analysis, findings))

    return findings


def _norm_key(value: Any) -> str:
    """Lowercase alnum-token normalization used for crude dedup."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


def _general_findings(
    general_analysis: dict[str, Any], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map the general LLM analysis into "gen-N" findings.

    These come from free-form LLM output, so they carry no text offsets
    (span_start/span_end are always None). Crude dedup against the rule
    findings: a general finding whose normalized title/clause overlaps an
    existing finding's slug or name is dropped — the rules version wins
    because it has spans.
    """
    keys: set[str] = set()
    for f in existing:
        for k in (_norm_key(f.get("clause_slug")), _norm_key(f.get("name"))):
            if k:
                keys.add(k)

    def _is_dup(title: str) -> bool:
        t = _norm_key(title)
        if not t:
            return False
        return any(t == k or (len(t) > 3 and (t in k or k in t)) for k in keys)

    out: list[dict[str, Any]] = []
    idx = 0

    def _add(kind: str, name: str, severity: str, dedup_key: str | None = None) -> None:
        nonlocal idx
        if _is_dup(dedup_key or name):
            return
        out.append(
            {
                "ref": f"gen-{idx}",
                "kind": kind,
                "name": name[:_MATCHED_TEXT_LIMIT],
                "severity": severity,
                "matched_text": "",
                "span_start": None,
                "span_end": None,
                "clause_slug": None,
            }
        )
        keys.add(_norm_key(name))
        idx += 1

    for r in general_analysis.get("risks") or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("description") or r.get("risk_type") or "").strip()
        if not name:
            continue
        sev = _GENERAL_RISK_SEVERITY.get(str(r.get("severity") or "").strip().lower(), "major")
        _add("general_risk", name, sev)

    for m in general_analysis.get("missing_clauses") or []:
        if not isinstance(m, dict):
            continue
        clause = str(m.get("clause") or "").strip()
        if not clause:
            continue
        sev = _GENERAL_IMPORTANCE_SEVERITY.get(
            str(m.get("importance") or "").strip().lower(), "minor"
        )
        _add("general_missing", f"Missing clause: {clause}", sev, dedup_key=clause)

    for u in general_analysis.get("unusual_provisions") or []:
        if not isinstance(u, dict):
            continue
        name = str(u.get("provision") or u.get("concern") or "").strip()
        if not name:
            continue
        _add("general_unusual", name, "minor")

    return out


def _mechanical_issue(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": finding["ref"],
        "title": finding["name"],
        "status": _STATUS_BY_KIND.get(finding["kind"], "info"),
        "severity": finding["severity"],
        "why": finding.get("why"),
        "suggested_language": finding.get("suggested_language"),
        "span_start": finding.get("span_start"),
        "span_end": finding.get("span_end"),
        "clause_slug": finding.get("clause_slug"),
        "source_kind": finding["kind"],
    }


ABSENCE_VERIFY_TEXT_CAP = 100_000

# Finding kinds that ASSERT AN ABSENCE ("the contract has no X"). Only these
# may be absence-verified: asking "does the contract address it?" about a
# risk finding ("uncapped liability") gets a confident "yes" plus a quote of
# the very clause that IS the risk — and the risk would silently become an
# "Addressed" info note. Mirrors the kinds redlines._SOURCE_LABELS describes
# as "not found" / "flagged as missing".
ABSENCE_SOURCE_KINDS: frozenset[str] = frozenset({"missing", "general_missing"})


def is_absence_claim(issue: dict[str, Any]) -> bool:
    """True only for a span-less finding that claims something is MISSING."""
    if issue.get("span_start") is not None:
        return False
    kind = str(issue.get("source_kind") or "")
    if kind:
        return kind in ABSENCE_SOURCE_KINDS
    # Legacy issues without a source_kind: the miss- refs are the checklist.
    return str(issue.get("ref") or "").startswith("miss-")


async def verify_absence_claims(
    client, text: str, issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop absence claims the contract actually refutes.

    The missing-clause checklist works off pattern-matched clause tags; odd
    formatting or terms incorporated by reference (an MSA) make it accuse
    contracts of lacking clauses they have. Before any such claim reaches the
    user (or worse, becomes a proposed addition), ask the model to point at
    where the contract addresses it — and require the quote to verify
    verbatim. Refuted claims are downgraded; everything else passes through
    untouched. Never raises.

    Only findings whose ``source_kind`` asserts an absence are candidates
    (see ``ABSENCE_SOURCE_KINDS``). Span-less risk / unusual / AI findings
    keep their severity and their ``grounding == "unverified"`` treatment —
    they are model conclusions, not accusations of absence.
    """
    targets = [
        (i, issue)
        for i, issue in enumerate(issues)
        if issue.get("grounding") in ("absence", "unverified") and is_absence_claim(issue)
    ]
    if client is None or not targets:
        return issues
    try:
        numbered = "\n".join(
            f"{n}: {issue.get('title') or issue.get('ref')} — {issue.get('why') or ''}"
            for n, (_i, issue) in enumerate(targets)
        )
        prompt = (
            f"{UNTRUSTED_CONTENT_RULE}\n\n"
            "Each numbered claim below asserts that this contract LACKS or "
            "fails to address something. For each: does the contract in fact "
            "address it — directly, or expressly by incorporating another "
            "document (e.g. a governing MSA)? If yes, provide ONE contiguous "
            "quote copied EXACTLY, character for character, from the contract "
            "text that addresses it. If the contract truly does not address "
            'it, set "addressed": false.\n\n'
            f"CLAIMS:\n{numbered}\n\n"
            'Return STRICT JSON: {"verdicts": [{"index": 0, "addressed": '
            'true|false, "quote": "..."}, ...]} with one entry per claim.\n\n'
            "CONTRACT TEXT:\n" + untrusted_block("Contract text", text[:ABSENCE_VERIFY_TEXT_CAP])
        )
        resp = await openai_chat(
            client,
            model=utility_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        verdicts = data.get("verdicts")
        if not isinstance(verdicts, list):
            return issues
        downgraded = 0
        for verdict in verdicts:
            if not isinstance(verdict, dict) or not verdict.get("addressed"):
                continue
            idx = verdict.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(targets)):
                continue
            quote = str(verdict.get("quote") or "").strip()
            # The refutation itself must be grounded — no quote, no downgrade.
            offsets = find_quote_offset(text, quote) if quote else None
            if offsets is None:
                continue
            # Nothing vanishes silently: the refuted accusation becomes a
            # visible, span-grounded "addressed" note pointing at the text
            # that answers it — and never becomes a redline.
            issue = issues[targets[idx][0]]
            issue["title"] = f"Addressed: {issue.get('title') or issue.get('ref')}"
            issue["status"] = "info"
            issue["severity"] = "minor"
            issue["grounding"] = "span"
            issue["span_start"], issue["span_end"] = offsets
            issue["matched_text"] = text[offsets[0] : offsets[1]][:600]
            issue["why"] = (
                "Initially flagged as missing, but the contract addresses it "
                "in the quoted text (possibly by incorporating another "
                "agreement)."
            )
            issue["suggested_language"] = None
            downgraded += 1
        if downgraded:
            logger.info("Absence verification downgraded %d refuted claim(s)", downgraded)
        return issues
    except Exception as e:  # verification must never break analysis
        logger.warning("Absence verification failed; keeping all issues: %s", e)
        return issues


def merge_issues(
    findings: list[dict[str, Any]], llm_payload: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Join LLM annotations back to source findings by ref.

    Pure and total: unknown refs are dropped, omitted findings get a
    mechanical entry, spans/slugs come only from the source findings, and a
    None/garbage payload degrades to the fully mechanical list.
    """
    by_ref = {f["ref"]: f for f in findings}
    seen: set[str] = set()
    issues: list[dict[str, Any]] = []

    entries = []
    if isinstance(llm_payload, dict):
        raw = llm_payload.get("issues")
        if isinstance(raw, list):
            entries = raw

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ref = entry.get("ref")
        src = by_ref.get(ref)
        if src is None or ref in seen:
            continue
        seen.add(ref)

        status = str(entry.get("status") or "").strip().lower()
        if status not in VALID_STATUSES:
            status = _STATUS_BY_KIND.get(src["kind"], "info")
        severity = str(entry.get("severity") or "").strip().lower()
        if severity not in VALID_SEVERITIES:
            severity = src["severity"]
        title = str(entry.get("title") or "").strip() or src["name"]
        why = entry.get("why")
        why = str(why).strip() or None if why is not None else None
        suggested = entry.get("suggested_language")
        suggested = str(suggested).strip() or None if suggested is not None else None

        issues.append(
            {
                "ref": ref,
                "title": title,
                "status": status,
                "severity": severity,
                "why": why,
                "suggested_language": suggested,
                # Offsets/slugs are attached here, deterministically — never by the LLM.
                "span_start": src.get("span_start"),
                "span_end": src.get("span_end"),
                "clause_slug": src.get("clause_slug"),
                "source_kind": src["kind"],
            }
        )

    for finding in findings:
        if finding["ref"] not in seen:
            issues.append(_mechanical_issue(finding))

    issues.sort(key=lambda i: _SEVERITY_ORDER.get(i["severity"], len(_SEVERITY_ORDER)))
    return issues


async def detect_contract_type(client, text: str) -> str:
    """Classify the document into one known contract type with one utility call."""
    prompt = (
        "Classify this contract into exactly one of the following types: "
        + ", ".join(KNOWN_CONTRACT_TYPES)
        + f'. If none fits, use "{FALLBACK_CONTRACT_TYPE}".\n'
        'Return STRICT JSON: {"contract_type":"..."}.\n\n'
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        "DOCUMENT:\n" + untrusted_block("Contract text", text[:_DETECT_TEXT_LIMIT])
    )
    try:
        resp = await openai_chat(
            client,
            model=utility_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        ct = str(data.get("contract_type") or "").strip().lower()
        if ct in KNOWN_CONTRACT_TYPES:
            return ct
    except Exception as e:
        logger.warning("contract-type detection failed: %s", e)
    return FALLBACK_CONTRACT_TYPE


async def annotate_issues(
    client,
    *,
    findings: list[dict[str, Any]],
    parties: list[dict[str, Any]],
    representing: str | None,
    posture: str,
    contract_type: str,
    instructions: str | None = None,
) -> dict[str, Any] | None:
    """One utility-LLM call that annotates findings for the represented side.

    Returns the parsed payload, or None on any failure (caller falls back to
    the mechanical issue list).
    """
    compact = [
        {
            "ref": f["ref"],
            "kind": f["kind"],
            "name": f["name"],
            "severity": f["severity"],
            "matched_text": f["matched_text"],
        }
        for f in findings
    ]
    side = representing or "neither side (neutral review)"
    posture_note = (
        "Posture is STRICT: flag everything that could hurt the client, use aggressive "
        "fallback language, and do not soften severities."
        if posture == "strict"
        else "Posture is BALANCED: focus on material issues and market-reasonable fallbacks."
    )
    # The firm's own review style beats any canned posture. Free text, bounded.
    instructions_note = (
        "FIRM REVIEW INSTRUCTIONS (follow these; they override the default "
        "posture wherever they conflict):\n" + bounded_instructions(instructions) + "\n"
        if instructions and instructions.strip()
        else ""
    )
    prompt = (
        "You are reviewing contract findings for a lawyer who represents: "
        f"{side}.\nContract type: {contract_type}.\n{posture_note}\n{instructions_note}\n"
        "For each finding below, assess it FROM THE REPRESENTED SIDE'S PERSPECTIVE. "
        "Echo the finding's ref exactly — never invent refs and never produce text "
        "offsets.\n\n"
        "Return STRICT JSON:\n"
        '{"issues":[{"ref":"...","title":"...",'
        '"status":"off_market"|"missing"|"unusual"|"info",'
        '"severity":"critical"|"major"|"minor",'
        '"why":"one sentence framed for the represented side",'
        '"suggested_language":"fallback/redline snippet or null"}]}\n\n'
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"PARTIES:\n{json.dumps(parties)}\n\n"
        # matched_text is contract text: third-party content, delimited.
        "FINDINGS:\n" + untrusted_block("Findings with contract excerpts", json.dumps(compact))
    )
    try:
        resp = await openai_chat(
            client,
            model=utility_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("issues annotation failed; using mechanical issues: %s", e)
        return None
