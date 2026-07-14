"""The portable artifact: tracegraph's system of record.

A :class:`~tracegraph.model.NormalizedTrace` serializes to a single self-describing
JSON file (schema-versioned). This artifact — not any graph-DB file — is the source
of truth: query backends (the in-memory store and optional LadybugDB cache) are
always rebuildable from it. Parquet is a future option; JSON keeps Phase 0 boring.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from tracegraph.model import NormalizedTrace

#: Bump when the on-disk shape changes incompatibly.
ARTIFACT_SCHEMA_VERSION = 2


def dumps(nt: NormalizedTrace) -> str:
    """Serialize a trace to a JSON string (stable key order for diff-friendly output)."""
    payload = {"schema_version": ARTIFACT_SCHEMA_VERSION, "trace": nt.model_dump(mode="json")}
    return json.dumps(payload, indent=2, sort_keys=True)


def _migrate_v1(trace_payload: dict) -> dict:
    """Add only v2 evidence metadata; never reinterpret legacy causal semantics."""
    migrated = json.loads(json.dumps(trace_payload))
    header = migrated.get("trace")
    if isinstance(header, dict):
        header.setdefault("causal_fidelity", "legacy_unknown")
        header.setdefault("links_preserved", None)
    for edge in migrated.get("edges") or []:
        if isinstance(edge, dict) and edge.get("type") == "CAUSED_BY":
            edge.setdefault("origin", "legacy_unknown")
    return migrated


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
    if version not in (1, ARTIFACT_SCHEMA_VERSION):
        raise ValueError(
            f"unsupported artifact schema_version {version!r} "
            f"(this build reads 1 and {ARTIFACT_SCHEMA_VERSION})"
        )
    # Guard the last raw dereference: a dict with the right schema_version but no "trace" key
    # (e.g. `{"schema_version": 1}`) would otherwise raise a bare KeyError, which — like the
    # AttributeError and RecursionError above — is neither OSError nor ValueError and would
    # escape the CLI's load handler. With this guard, loads() raises *only* ValueError for any
    # malformed input, so every load path (strict file, lenient directory skip) stays clean.
    if "trace" not in payload:
        raise ValueError('artifact JSON is missing the required "trace" object')
    trace_payload = payload["trace"]
    if not isinstance(trace_payload, dict):
        raise ValueError('artifact "trace" must be an object')
    if version == 1:
        trace_payload = _migrate_v1(trace_payload)
    return NormalizedTrace.model_validate(trace_payload)


def save(nt: NormalizedTrace, path: str | Path) -> None:
    Path(path).write_text(dumps(nt), encoding="utf-8")


def save_atomic(nt: NormalizedTrace, path: str | Path) -> None:
    """Validate and atomically replace an artifact without leaving partial output."""
    target = Path(path)
    text = dumps(nt)
    loads(text)  # writer-side contract check before touching the destination
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.replace(temp_name, target)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def load(path: str | Path) -> NormalizedTrace:
    return loads(Path(path).read_text(encoding="utf-8"))
