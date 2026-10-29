"""Step conditions: run a step only when an earlier step's output says so.

A condition is ``{step, path, op, value}``. The path is walked over the
earlier step's output exactly as a ``{{step.path}}`` template reference is
(dictionary keys and list indexes, nothing evaluated); the operator compares
what it finds with ``value``. The evaluation never raises: a path that does
not exist is simply "not found", and a comparison that makes no sense (a
number against prose) is false. Every evaluation returns what it saw, so the
step run can record why it ran or did not.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.workflows.substitution import _walk

# What ``actual`` is cut to when recorded on the step run.
_ACTUAL_CHARS = 200


def _present(value: Any) -> bool:
    """A value counts as present unless it is None, empty or blank."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict | tuple | set):
        return len(value) > 0
    return True


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        # Leading number in prose such as "36 months" or "USD 250,000".
        for token in text.replace("$", " ").split():
            try:
                return float(token)
            except ValueError:
                continue
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str).lower()


def _count(value: Any) -> int | None:
    if isinstance(value, list | dict | tuple | set | str):
        return len(value)
    return None


def _equals(actual: Any, expected: Any) -> bool:
    a_num, e_num = _as_number(actual), _as_number(expected)
    if a_num is not None and e_num is not None and isinstance(actual, int | float | bool):
        return a_num == e_num
    return _as_text(actual) == _as_text(expected)


def _contains(actual: Any, expected: Any) -> bool:
    needle = _as_text(expected)
    if isinstance(actual, dict):
        return any(_as_text(k) == needle for k in actual)
    if isinstance(actual, list | tuple | set):
        return any(_as_text(item) == needle or needle in _as_text(item) for item in actual)
    return needle in _as_text(actual)


def _compare(actual: Any, expected: Any, op: str) -> bool:
    a, e = _as_number(actual), _as_number(expected)
    if a is None or e is None:
        return False
    return {"gt": a > e, "lt": a < e, "gte": a >= e, "lte": a <= e}[op]


def evaluate(condition: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Evaluate ``condition`` against ``outputs`` (step name -> output).

    Returns ``{"result": bool, "step", "path", "op", "value", "found": bool,
    "actual": <what the path held, truncated>}``.
    """
    step = str(condition.get("step") or "")
    path = [p for p in str(condition.get("path") or "").split(".") if p]
    op = str(condition.get("op") or "exists")
    expected = condition.get("value")
    lowered = {name.lower(): out for name, out in outputs.items()}
    output = lowered.get(step.lower())
    found, actual = (False, None) if output is None else _walk(output, path)

    if op == "exists":
        result = found and _present(actual)
    elif op == "not_exists":
        result = not (found and _present(actual))
    elif not found:
        result = False
    elif op == "equals":
        result = _equals(actual, expected)
    elif op == "not_equals":
        result = not _equals(actual, expected)
    elif op == "contains":
        result = _contains(actual, expected)
    elif op in ("gt", "lt", "gte", "lte"):
        result = _compare(actual, expected, op)
    elif op in ("count_gt", "count_eq"):
        count, e = _count(actual), _as_number(expected)
        result = (
            count is not None and e is not None and (count > e if op == "count_gt" else count == e)
        )
    else:
        result = False

    shown = json.dumps(actual, ensure_ascii=False, default=str) if found else None
    return {
        "result": bool(result),
        "step": step,
        "path": ".".join(path),
        "op": op,
        "value": expected,
        "found": bool(found),
        "actual": shown[:_ACTUAL_CHARS] if shown is not None else None,
    }
