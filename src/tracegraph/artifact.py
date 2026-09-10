"""The portable artifact: tracegraph's system of record.

A :class:`~tracegraph.model.NormalizedTrace` serializes to a single self-describing
JSON file (schema-versioned). This artifact — not any graph-DB file — is the source
of truth: query backends (the in-memory store and optional LadybugDB cache) are
always rebuildable from it. Parquet is a future option; JSON keeps Phase 0 boring.
"""

from __future__ import annotations

import copy
import json
import hashlib
import re
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
    # copy.deepcopy, not a JSON round-trip: from_obj accepts an *already-parsed* payload, which
    # a caller may have built in memory rather than read from a file. A JSON round-trip raises
    # TypeError on any value json can't serialize (a set, a datetime), and TypeError is neither
    # OSError nor ValueError — it would escape every load-boundary handler and crash the
    # command, breaking from_obj's documented "raises only ValueError" contract. deepcopy
    # copies anything; the model validation below is what rejects values that aren't valid.
    migrated = copy.deepcopy(trace_payload)
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
    return from_obj(payload)


def from_obj(payload: object) -> NormalizedTrace:
    """Validate an already-parsed artifact payload (the envelope ``loads`` would parse).

    Exists so a caller that already holds the parsed JSON (e.g. the CLI, which sniffs
    ``schema_version`` first) doesn't re-parse the whole document. Raises only ``ValueError``
    for malformed input, same contract as :func:`loads`.
    """
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


def save_atomic(nt: NormalizedTrace, path: str | Path, *, replace: bool = True) -> None:
    """Validate and atomically replace an artifact without leaving partial output."""
    target = Path(path)
    text = dumps(nt)
    # Writer-side contract check before touching the destination: the payload we just
    # serialized must round-trip through the model. Validates the parsed shape directly —
    # no need to re-parse the JSON string we produced ourselves.
    NormalizedTrace.model_validate(nt.model_dump(mode="json"))
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
        if replace:
            os.replace(temp_name, target)
        else:
            os.link(temp_name, target)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def load(path: str | Path) -> NormalizedTrace:
    return loads(Path(path).read_text(encoding="utf-8"))


def default_path(identifier: str) -> Path:
    """Opaque source identifiers never become directories or reserved filenames."""
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
    if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identifier)
            and identifier.split(".")[0].upper() not in reserved
            and not identifier.endswith(".")):
        return Path(identifier + ".json")
    return Path("trace-" + hashlib.sha256(identifier.encode()).hexdigest() + ".json")
