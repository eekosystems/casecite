"""Bound a step's stored output.

A step's ``data`` can be large (every issue of twenty contracts, a long
draft). The stored output is capped at ``WORKFLOW_MAX_OUTPUT_BYTES``: the
summary, document ids and produced ids always survive; the largest data
fields are dropped first until the rest fits, and the output says which ones
were cut so the UI never shows a silent gap.
"""

from __future__ import annotations

import json
from typing import Any


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def cap_output(output: dict[str, Any], limit: int) -> dict[str, Any]:
    """Return ``output`` within ``limit`` bytes, dropping the largest data fields first."""
    if limit <= 0 or _size(output) <= limit:
        return output
    data = dict(output.get("data") or {})
    capped = {**output, "data": data}
    dropped: list[str] = []
    by_size = sorted(data, key=lambda k: _size(data[k]), reverse=True)
    for key in by_size:
        if _size(capped) <= limit:
            break
        value = data[key]
        if isinstance(value, str):
            # A long text: keep the head rather than nothing.
            keep = max(0, len(value) - (_size(capped) - limit) - 200)
            data[key] = value[:keep] + ("…" if keep < len(value) else "")
        elif isinstance(value, list):
            # Shrink from the end until it fits or nothing is left.
            while value and _size(capped) > limit:
                value = value[:-1]
                data[key] = value
            if not value:
                data.pop(key, None)
        else:
            data.pop(key, None)
        dropped.append(key)
    capped["truncated"] = True
    capped["truncated_fields"] = dropped
    return capped
