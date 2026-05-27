"""The portable artifact: tracegraph's system of record.

A :class:`~tracegraph.model.NormalizedTrace` serializes to a single self-describing
JSON file (schema-versioned). This artifact — not any graph-DB file — is the source
of truth: query backends (the in-memory store now, an optional Kùzu cache later) are
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
    payload = json.loads(text)
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
