"""Built-in review schemas: field sets a reviewer starts from.

Defined in code so every install has them and no row has to be seeded; the
API lists them beside the firm's saved schemas with ``is_builtin`` set and an
id of the form ``builtin:<key>``. They are read-only — "save as template"
from a table makes an editable copy owned by the caller.
"""

from __future__ import annotations

from typing import Any

from app.models.schemas import field_key_from_name

BUILTIN_PREFIX = "builtin:"


def _field(name: str, type_: str, description: str) -> dict[str, Any]:
    return {
        "key": field_key_from_name(name),
        "name": name,
        "type": type_,
        "description": description,
    }


BUILTIN_SCHEMAS: list[dict[str, Any]] = [
    {
        "key": "nda",
        "name": "Non-disclosure agreement",
        "description": "The terms that decide whether an NDA is acceptable: who is bound, for how long, and what is carved out.",
        "fields": [
            _field(
                "Parties", "text", "The disclosing and receiving parties as named in the preamble"
            ),
            _field("Effective date", "date", "The date the agreement takes effect"),
            _field(
                "Mutual or one-way", "text", "Whether both parties disclose (mutual) or only one"
            ),
            _field(
                "Purpose",
                "text",
                "The stated purpose for which confidential information may be used",
            ),
            _field(
                "Confidentiality term",
                "duration",
                "How long the confidentiality obligations last after disclosure or termination",
            ),
            _field("Agreement term", "duration", "How long the agreement itself runs"),
            _field(
                "Standard exclusions",
                "boolean",
                "Are the usual carve-outs present (public domain, already known, independently developed, third-party source)?",
            ),
            _field(
                "Compelled disclosure",
                "boolean",
                "May information be disclosed when required by law or court order, with notice?",
            ),
            _field(
                "Residuals clause",
                "boolean",
                "May the recipient use information retained in unaided memory?",
            ),
            _field(
                "Return or destruction",
                "text",
                "What must happen to the information on request or termination",
            ),
            _field("Governing law", "text", "The state or country whose law governs"),
            _field(
                "Injunctive relief",
                "boolean",
                "Does the agreement acknowledge that equitable relief is available for breach?",
            ),
        ],
    },
    {
        "key": "msa",
        "name": "Master services agreement",
        "description": "Commercial, risk and exit terms of a services agreement.",
        "fields": [
            _field("Parties", "text", "The customer and the provider as named in the preamble"),
            _field("Effective date", "date", "The date the agreement takes effect"),
            _field("Initial term", "duration", "The length of the initial term"),
            _field("Auto-renewal", "boolean", "Does the agreement renew automatically?"),
            _field(
                "Termination for convenience notice",
                "duration",
                "Notice required to terminate without cause, and by whom",
            ),
            _field(
                "Payment terms",
                "text",
                "When invoices are due, e.g. net 30, and any late-payment interest",
            ),
            _field(
                "Liability cap",
                "money",
                "The cap on each party's aggregate liability (amount or formula, e.g. fees paid in the prior 12 months)",
            ),
            _field(
                "Liability cap exclusions",
                "text",
                "What is carved out of the cap (confidentiality, indemnities, gross negligence, IP)",
            ),
            _field(
                "Indemnities",
                "text",
                "Who indemnifies whom, and for what (IP infringement, third-party claims, data breach)",
            ),
            _field(
                "Service levels",
                "text",
                "Any uptime or response commitments and the remedy for missing them",
            ),
            _field("IP ownership", "text", "Who owns deliverables and work product"),
            _field("Governing law", "text", "The state or country whose law governs"),
        ],
    },
    {
        "key": "lease",
        "name": "Commercial lease",
        "description": "The economic and control terms of a commercial lease.",
        "fields": [
            _field("Landlord", "text", "The landlord as named in the lease"),
            _field("Tenant", "text", "The tenant as named in the lease"),
            _field("Premises", "text", "The address and area of the leased premises"),
            _field("Commencement date", "date", "The date the term begins"),
            _field("Lease term", "duration", "The length of the initial term"),
            _field(
                "Base rent",
                "money",
                "The base rent and the period it is stated for (per month or per year)",
            ),
            _field(
                "Rent escalation",
                "text",
                "How and when base rent increases (fixed percentage, CPI, stepped)",
            ),
            _field("Security deposit", "money", "The security deposit amount"),
            _field(
                "Renewal options", "text", "Any option to renew, its length and the notice required"
            ),
            _field("Permitted use", "text", "The use the premises may be put to"),
            _field(
                "Assignment and subletting",
                "text",
                "Whether assignment or subletting is permitted and on what consent",
            ),
            _field(
                "Repair and maintenance",
                "text",
                "Which party is responsible for repairs, structure, HVAC and common areas",
            ),
        ],
    },
    {
        "key": "employment",
        "name": "Employment agreement",
        "description": "Compensation, restrictive covenants and exit terms of an employment agreement.",
        "fields": [
            _field("Employee", "text", "The employee's name"),
            _field("Employer", "text", "The employing entity"),
            _field("Position", "text", "The title or role"),
            _field("Start date", "date", "The employment start date"),
            _field("Base salary", "money", "The base salary and its period"),
            _field("Bonus", "text", "Any bonus, its target and the conditions for earning it"),
            _field("Equity", "text", "Any equity grant, its size and vesting schedule"),
            _field(
                "Non-compete",
                "duration",
                "The length of any post-employment non-compete, and its scope",
            ),
            _field(
                "Non-solicit",
                "duration",
                "The length of any non-solicitation of employees or customers",
            ),
            _field(
                "Termination notice", "duration", "Notice required to terminate, by either side"
            ),
            _field(
                "Severance",
                "text",
                "Severance payable on termination without cause, and any conditions (release, good reason)",
            ),
            _field("Governing law", "text", "The state or country whose law governs"),
        ],
    },
]


def builtin_schema_id(key: str) -> str:
    return f"{BUILTIN_PREFIX}{key}"


def builtin_schemas() -> list[dict[str, Any]]:
    """The built-ins as the schema API lists them (read-only, no owner)."""
    return [
        {
            "id": builtin_schema_id(s["key"]),
            "name": s["name"],
            "description": s["description"],
            "fields": [dict(f) for f in s["fields"]],
            "is_builtin": True,
            "matter_id": None,
            "owner_id": None,
            "role": "builtin",
            "created_at": None,
            "updated_at": None,
        }
        for s in BUILTIN_SCHEMAS
    ]


def builtin_schema(schema_id: str) -> dict[str, Any] | None:
    for s in builtin_schemas():
        if s["id"] == schema_id:
            return s
    return None
