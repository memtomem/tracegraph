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
from tracegraph.model import NormalizedTrace, StepSource, check_provenance
from tracegraph.normalize import validate_structure

#: Bump when the on-disk shape changes incompatibly.
ARTIFACT_SCHEMA_VERSION = 7

#: Versions this build can read. 1 needs a migration (see ``_migrate_v1``); 2 -> 3 is the
#: identity, because 3 adds only a new ``Step.source`` value and changes nothing existing.
#: 4 signals that TREE_PARENT was derived under EdgeOrigin precedence rather than
#: legacy temporal-only selection. 5 adds only the ``interrupt`` ``Step.source`` value. 6 adds
#: the ``scheduled`` ``Step.source`` value and the optional ``schedule_graph`` trace header. 7 adds
#: the optional ``capture`` trace header and ``Step.unknown_fields``.
_READABLE_VERSIONS = (1, 2, 3, 4, 5, 6, ARTIFACT_SCHEMA_VERSION)


def _required_version(nt: NormalizedTrace) -> int:
    """The lowest version that can read these bytes.

    Stamped from the *content*, not from the build, so serializing an artifact that uses no
    new vocabulary keeps producing byte-identical v2 output. That matters because the
    envelope is inside the hash: ``review_candidates`` binds each candidate to the sha256 of
    the artifact file, and ``LadybugStore`` round-trips artifacts byte for byte. A blanket
    bump would invalidate digests already exported in review-candidate reports for artifacts
    whose content did not change at all.

    * An artifact that records capture provenance — a ``capture`` header, or a step with
      ``unknown_fields`` — is stamped 7. Only adapters write it, so loading and re-saving an
      older artifact never adds it and its bytes stay as they were.
    * An artifact read against a supplied graph — it has a ``schedule_graph`` header, whether
      or not that graph scheduled anything, or a scheduled step — is stamped 6, for the same
      reason as 5 below.
    * An artifact that carries an interrupt step is stamped 5, whatever its projection: a
      v5 reader also reads the v4 projection, and an older reader would otherwise fail on
      the unknown enum value instead of refusing the version by name.
    * An artifact whose derived TREE_PARENT relies on EdgeOrigin priority is stamped 4,
      so older readers (which used temporal-only parent selection) refuse it with an
      explicit "unsupported artifact schema_version 4" rather than failing canonical
      validation with a confusing mismatch error.
    * An artifact that carries a derived task step is stamped 3.
    * Otherwise, stamped 2.
    """
    from tracegraph.normalize import requires_origin_priority_projection

    if nt.trace.capture is not None or any(step.unknown_fields for step in nt.steps):
        return 7
    scheduled = StepSource.SCHEDULED
    if nt.trace.schedule_graph is not None or any(step.source is scheduled for step in nt.steps):
        return 6
    interrupt = StepSource.INTERRUPT
    if any(step.source is interrupt for step in nt.steps):
        return 5
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
    # The model refuses this pairing on validation; a trace changed afterwards must not become
    # a file this module's own reader rejects.
    check_provenance(nt.trace, nt.steps)
    trace = nt.model_dump(mode="json")
    # An empty ``unknown_fields`` is left out so a step with nothing substituted keeps the
    # bytes it had before the field existed. Done here rather than in a Step serializer: under
    # pydantic 2.13 a wrap ``model_serializer`` turns a nested NaN into null in JSON mode, which
    # would silently write the value this function exists to refuse.
    for step in trace["steps"]:
        if not step["unknown_fields"]:
            del step["unknown_fields"]
    payload = {"schema_version": _required_version(nt), "trace": trace}
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


def _checked_dumps(nt: NormalizedTrace) -> str:
    """Serialize ``nt`` only if tracegraph's own readers would accept the result.

    Pydantic does not revalidate on attribute assignment, so a trace changed in place after it
    was built can hold a field the model refuses (``seq="zero"``) or a structure no reader
    accepts (a dangling edge). Both writers check before touching the destination: the payload
    must round-trip through the model, and the trace must pass :func:`validate_structure`, the
    cheap structural subset every reader relies on. Raises ``ValueError`` (pydantic's
    ``ValidationError`` is one). The model goes first and checks everything it can, including
    a container replaced by ``None`` and the provenance pairing; anything run before it would
    dereference unvalidated values (:func:`dumps` re-derives the projection to pick a schema
    version, and on a mistyped field fails with a raw ``TypeError``).
    """
    # Check and write the validated copy, not ``nt``: a value the model coerces (``"0"`` for a
    # seq, ``"CAUSED_BY"`` for an edge type) would otherwise reach the structural check and the
    # serializer still unconverted.
    checked = NormalizedTrace.model_validate(nt.model_dump(mode="json"))
    validate_structure(checked)
    return dumps(checked)


def save(nt: NormalizedTrace, path: str | Path) -> None:
    """Write an artifact; refuses, before writing, one the readers would reject."""
    Path(path).write_text(_checked_dumps(nt), encoding="utf-8")


def save_atomic(nt: NormalizedTrace, path: str | Path, *, replace: bool = True) -> None:
    """Validate and atomically replace an artifact without leaving partial output."""
    write_text_atomic(Path(path), _checked_dumps(nt), replace=replace)


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
