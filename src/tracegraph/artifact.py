"""The portable artifact: tracegraph's system of record.

A :class:`~tracegraph.model.NormalizedTrace` serializes to a single self-describing
JSON file (schema-versioned). This artifact — not any graph-DB file — is the source
of truth: query backends (the in-memory store and optional Kùzu cache) are
always rebuildable from it. Parquet is a future option; JSON keeps Phase 0 boring.
"""

from __future__ import annotations

import json
from pathlib import Path

from tracegraph.model import NormalizedTrace

#: Bump when the on-disk shape changes incompatibly.
ARTIFACT_SCHEMA_VERSION = 1


def dumps(nt: NormalizedTrace) -> str:
    """Serialize a trace to a JSON string (stable key order for diff-friendly output)."""
    payload = {"schema_version": ARTIFACT_SCHEMA_VERSION, "trace": nt.model_dump(mode="json")}
    return json.dumps(payload, indent=2, sort_keys=True)


def loads(text: str) -> NormalizedTrace:
    """Parse a trace from a JSON string, validating the schema version."""
    try:
        payload = json.loads(text)
    except RecursionError as exc:
        # Deeply-nested JSON overflows json's recursive scanner with a RecursionError (a
        # RuntimeError, *not* a ValueError). Normalize it so the load boundary treats an
        # over-nested stray as bad input — otherwise it escapes the CLI's (OSError, ValueError)
        # handler and crashes the whole command with a raw traceback.
        raise ValueError("JSON nesting too deep") from exc
    # The artifact envelope is a JSON object. A top-level array/string/number/null is valid
    # JSON but not an artifact (e.g. a stray data export) — reject it as a clean ValueError
    # rather than letting ``payload.get`` raise an opaque AttributeError that callers can't
    # distinguish from a real bug. CLI directory globbing relies on this to skip non-artifacts.
    if not isinstance(payload, dict):
        raise ValueError(
            f"artifact JSON must be a top-level object, got {type(payload).__name__}"
        )
    version = payload.get("schema_version")
    if version != ARTIFACT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported artifact schema_version {version!r} "
            f"(this build reads {ARTIFACT_SCHEMA_VERSION})"
        )
    return NormalizedTrace.model_validate(payload["trace"])


def save(nt: NormalizedTrace, path: str | Path) -> None:
    Path(path).write_text(dumps(nt), encoding="utf-8")


def load(path: str | Path) -> NormalizedTrace:
    return loads(Path(path).read_text(encoding="utf-8"))
