"""Shape checks at untrusted JSON boundaries; diagnostics never echo field values."""

import json
from typing import Any


def loads(text: str) -> Any:
    try:
        return json.loads(text)
    except RecursionError as exc:
        raise ValueError("JSON nesting too deep") from exc


def object_field(value: Any, path: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value
