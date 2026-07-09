from __future__ import annotations

import re
from typing import Any


def allowed_publication_types(source_config: dict[str, Any]) -> set[str]:
    values = source_config.get("allowed_publication_types") or []
    if not isinstance(values, list):
        return set()
    return {_normalize(value) for value in values if str(value or "").strip()}


def publication_type_allowed(raw_type: Any, allowed_types: set[str]) -> bool:
    if not allowed_types or raw_type in (None, ""):
        return True
    if isinstance(raw_type, list):
        return any(_normalize(value) in allowed_types for value in raw_type if str(value or "").strip())
    return _normalize(raw_type) in allowed_types


def _normalize(value: Any) -> str:
    return re.sub(r"[-_\s]+", "", str(value).strip().lower())
