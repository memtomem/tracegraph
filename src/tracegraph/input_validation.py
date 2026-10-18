"""Shape checks at untrusted JSON boundaries; diagnostics never echo field values."""

import json
from typing import Any


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON contains the non-standard constant {name}")


def loads(text: str, *, strict: bool = False) -> Any:
    """Parse JSON. ``strict`` also refuses ``NaN``/``Infinity``, which strict parsers reject."""
    try:
        if strict:
            return json.loads(text, parse_constant=_reject_constant)
        return json.loads(text)
    except RecursionError as exc:
        raise ValueError("JSON nesting too deep") from exc


def object_field(value: Any, path: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value
