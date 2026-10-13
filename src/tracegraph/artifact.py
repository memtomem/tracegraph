"""The portable artifact: tracegraph's system of record.

A :class:`~tracegraph.model.NormalizedTrace` serializes to a single self-describing
JSON file (schema-versioned). This artifact — not any graph-DB file — is the source
of truth: query backends (the in-memory store and optional LadybugDB cache) are
always rebuildable from it. Parquet is a future option; JSON keeps Phase 0 boring.
"""

from __future__ import annotations

import json
import hashlib
from collections.abc import Iterable, Mapping
import re
from pathlib import Path

from tracegraph._atomic_write import write_text_atomic
from tracegraph.input_validation import loads as json_loads
from tracegraph.model import NormalizedTrace, StepSource

#: Bump when the on-disk shape changes incompatibly.
ARTIFACT_SCHEMA_VERSION = 4

#: Versions this build can read. 1 needs a migration (see ``_migrate_v1``); 2 -> 3 is the
#: identity, because 3 adds only a new ``Step.source`` value and changes nothing existing.
#: 4 signals that TREE_PARENT was derived under EdgeOrigin precedence rather than
#: legacy temporal-only selection.
_READABLE_VERSIONS = (1, 2, 3, ARTIFACT_SCHEMA_VERSION)


def _required_version(nt: NormalizedTrace) -> int:
    """The lowest version that can read these bytes.

    Stamped from the *content*, not from the build, so serializing an artifact that uses no
    new vocabulary keeps producing byte-identical v2 output. That matters because the
    envelope is inside the hash: ``review_candidates`` binds each candidate to the sha256 of
    the artifact file, and ``LadybugStore`` round-trips artifacts byte for byte. A blanket
    bump would invalidate digests already exported in review-candidate reports for artifacts
    whose content did not change at all.

    * An artifact whose derived TREE_PARENT relies on EdgeOrigin priority is stamped 4,
      so older readers (which used temporal-only parent selection) refuse it with an
      explicit "unsupported artifact schema_version 4" rather than failing canonical
      validation with a confusing mismatch error.
    * An artifact that carries a derived task step is stamped 3.
    * Otherwise, stamped 2.
    """
    from tracegraph.normalize import requires_origin_priority_projection

    if requires_origin_priority_projection(nt):
        return 4
    task = StepSource.TASK
    return 3 if any(step.source is task for step in nt.steps) else 2


def dumps(nt: NormalizedTrace) -> str:
    """Serialize a trace to a JSON string (stable key order for diff-friendly output).

    Raises ``ValueError`` rather than emitting the non-standard ``NaN``/``Infinity`` tokens,
    which strict JSON parsers reject. The model refuses non-finite floats on validation, so
    only a model built around validation (``model_construct``, attribute assignment) gets here.
    """
    payload = {"schema_version": _required_version(nt), "trace": nt.model_dump(mode="json")}
    return json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)


def _migrate_v1(trace_payload: dict) -> dict:
    """Add only v2 evidence metadata; never reinterpret legacy causal semantics.

    Copies exactly the containers it writes to — the envelope, the trace header, and the
    CAUSED_BY edges — and shares everything else with the caller. Nothing outside those is
    mutated, so sharing is safe. The cost still scales with the edge collection, which it has
    to walk; what it no longer does is traverse unrelated nested values it never reads.

    Whole-payload copies were tried twice and both leaked an exception type this module
    promises never to raise. A JSON round-trip raises ``TypeError`` on any value json cannot
    serialize, and ``from_obj`` accepts already-parsed payloads that may hold such values.
    ``copy.deepcopy`` fixed that but recurses per level, so a deeply-nested value in a field
    the model ignores raised ``RecursionError`` on an artifact that used to load. Neither is
    ``OSError`` or ``ValueError``, so both escaped every load-boundary handler and crashed the
    command. Touching only what we modify avoids the whole class: no traversal, no recursion.
    """
    migrated = dict(trace_payload)
    header = migrated.get("trace")
    if isinstance(header, dict):
        header = dict(header)
        header.setdefault("causal_fidelity", "legacy_unknown")
        header.setdefault("links_preserved", None)
        migrated["trace"] = header
    edges = migrated.get("edges")
    # Match the *shape*, not a list of concrete types. The model accepts any iterable for its
    # edge sequence (list, tuple, deque, a one-shot iterator), and enumerating types here
    # meant every shape not on the list silently skipped migration, so its CAUSED_BY edges
    # lost the legacy origin default. Strings, bytes and mappings are iterable but are never
    # an edge sequence; anything else non-iterable falls through untouched for the model to
    # reject as a clean ValueError. Materializing once also leaves a one-shot iterator usable,
    # which iterating in place did not.
    if isinstance(edges, Iterable) and not isinstance(edges, (str, bytes, bytearray, Mapping)):
        try:
            # Mapping, not dict: the model accepts any mapping as an edge record, and matching
            # dict alone left mapping-shaped records without the legacy origin default.
            migrated["edges"] = [
                {"origin": "legacy_unknown", **edge}
                if isinstance(edge, Mapping) and edge.get("type") == "CAUSED_BY"
                else edge
                for edge in edges
            ]
        except Exception as exc:
            # Consuming a caller-supplied lazy sequence can raise anything at all. This is a
            # load boundary that promises ValueError, and the v2 path already reports a
            # failing iterator that way (as a ValidationError), so v1 must not differ.
            raise ValueError(f"artifact edges could not be read: {exc}") from exc
    return migrated


def loads(text: str) -> NormalizedTrace:
    """Parse a trace from a JSON string, validating the schema version.

    Over-nested JSON raises ``ValueError`` (see ``input_validation.loads``), not the
    ``RecursionError`` json itself raises, so it cannot escape the CLI's load handler.
    """
    return from_obj(json_loads(text))


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
    # Exact int, not equality: `True == 1` and `2.0 == 2`, and `true` used to take the v1
    # migration path. `dumps` only ever writes an int.
    if type(version) is not int or version not in _READABLE_VERSIONS:
        readable = ", ".join(str(v) for v in _READABLE_VERSIONS)
        raise ValueError(
            f"unsupported artifact schema_version {version!r} (this build reads {readable})"
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
    nt = NormalizedTrace.model_validate(trace_payload)
    # The envelope is the signal an older reader acts on, so it may overstate what the content
    # needs (a v3 file without task steps loads today) but never understate it: a v2 envelope
    # around an origin-priority projection would be read under the wrong projection by every
    # reader that trusts it, and a re-save would quietly become a different file. A v1
    # envelope is migrated to v2 semantics, hence the floor of 2.
    required = _required_version(nt)
    if required > max(version, 2):
        raise ValueError(
            f"artifact declares schema_version {version} but its content requires {required}"
        )
    return nt


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
    write_text_atomic(target, text, replace=replace)


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
