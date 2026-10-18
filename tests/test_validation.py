"""Negative-path guards: malformed raw input must be rejected, not silently mangled."""

import json
from collections import UserDict, deque

import pytest

from tracegraph import artifact
from tracegraph.model import Edge, EdgeType, NormalizedTrace, RawTrace, Step, StepStatus, Trace
from tracegraph.normalize import normalize, validate_normalized, validate_tree
from tracegraph.store import InMemoryStore


def _steps(*id_seq: tuple[str, int]) -> list[Step]:
    return [Step(step_id=i, trace_id="t", seq=s) for i, s in id_seq]


def _raw(steps: list[Step], edges: list[Edge]) -> RawTrace:
    return RawTrace(trace=Trace(trace_id="t", source_kind="test"), steps=steps, causal_edges=edges)


def test_unknown_cause_rejected():
    raw = _raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="ghost")])
    with pytest.raises(ValueError, match="not a known step"):
        normalize(raw)


def test_normalize_output_is_canonically_ordered_independent_of_input_order():
    """The contract LadybugDB (and any other cache) relies on: ``normalize`` produces the same
    artifact bytes regardless of how the adapter happened to enumerate steps and edges.

    We feed the SAME logical trace twice — once in canonical (seq-ASC) order, once
    deliberately scrambled — and assert the two normalized outputs are identical."""
    s = _steps(("a", 0), ("b", 1), ("c", 2))
    canonical_edges = [
        Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
        Edge(type=EdgeType.CAUSED_BY, src="c", dst="b"),
    ]
    scrambled_edges = list(reversed(canonical_edges))
    scrambled_steps = list(reversed(s))

    nt_canonical = normalize(_raw(s, canonical_edges))
    nt_scrambled = normalize(_raw(scrambled_steps, scrambled_edges))

    assert nt_canonical == nt_scrambled
    # And the canonical layout itself: steps in seq order, CAUSED_BY before derived edges.
    assert [step.step_id for step in nt_canonical.steps] == ["a", "b", "c"]
    assert [(e.type, e.src, e.dst) for e in nt_canonical.edges_of(EdgeType.CAUSED_BY)] == [
        (EdgeType.CAUSED_BY, "b", "a"),
        (EdgeType.CAUSED_BY, "c", "b"),
    ]


def test_unknown_effect_rejected():
    raw = _raw(_steps(("a", 0)), [Edge(type=EdgeType.CAUSED_BY, src="ghost", dst="a")])
    with pytest.raises(ValueError, match="not a known step"):
        normalize(raw)


def test_self_edge_rejected():
    raw = _raw(_steps(("a", 0)), [Edge(type=EdgeType.CAUSED_BY, src="a", dst="a")])
    with pytest.raises(ValueError, match="self-causal"):
        normalize(raw)


def test_cause_must_precede_effect():
    # b(seq1) "caused by" c(seq2): the cause is later than the effect -> reject.
    # This rule also makes raw cycles impossible (seq can't strictly decrease in a loop).
    raw = _raw(_steps(("b", 1), ("c", 2)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="c")])
    with pytest.raises(ValueError, match="does not precede"):
        normalize(raw)


def test_duplicate_caused_by_edge_rejected():
    raw = _raw(
        _steps(("a", 0), ("b", 1)),
        [
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
        ],
    )
    with pytest.raises(ValueError, match="duplicate CAUSED_BY"):
        normalize(raw)


def test_non_caused_by_edge_in_raw_rejected():
    raw = _raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.TREE_PARENT, src="b", dst="a")])
    with pytest.raises(ValueError, match="CAUSED_BY"):
        normalize(raw)


def test_duplicate_step_id_rejected():
    raw = _raw(
        [Step(step_id="a", trace_id="t", seq=0), Step(step_id="a", trace_id="t", seq=1)], []
    )
    with pytest.raises(ValueError, match="duplicate"):
        normalize(raw)


def test_trace_id_mismatch_rejected():
    raw = RawTrace(
        trace=Trace(trace_id="t", source_kind="x"),
        steps=[Step(step_id="a", trace_id="OTHER", seq=0)],
        causal_edges=[],
    )
    with pytest.raises(ValueError, match="trace_id"):
        normalize(raw)


def test_normalize_is_deterministic():
    raw = _raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="a")])
    assert normalize(raw) == normalize(raw)


def test_ancestors_raises_on_dangling_edge():
    # A dangling edge can no longer be inserted (upsert_edges rejects it), so simulate the
    # corrupted adjacency directly: ancestors() must still surface it rather than drop it.
    store = InMemoryStore()
    store.init_schema()
    store.upsert_nodes(_steps(("b", 1)))
    store._caused_by["b"] = ["ghost"]
    with pytest.raises(KeyError, match="unknown step"):
        store.ancestors("b")


def test_validate_tree_detects_cycle():
    # Each step has exactly one TREE_PARENT (passes the forest single-parent check),
    # but a<->b forms a cycle -> must be caught.
    nt = NormalizedTrace(
        trace=Trace(trace_id="t", source_kind="x"),
        steps=_steps(("a", 0), ("b", 1)),
        edges=[
            Edge(type=EdgeType.TREE_PARENT, src="a", dst="b"),
            Edge(type=EdgeType.TREE_PARENT, src="b", dst="a"),
        ],
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_tree(nt)


def test_from_trace_rejects_non_forest():
    # A NormalizedTrace that smuggled in two TREE_PARENTs must not load into a store.
    nt = NormalizedTrace(
        trace=Trace(trace_id="t", source_kind="x"),
        steps=_steps(("a", 0), ("b", 1), ("c", 2)),
        edges=[
            Edge(type=EdgeType.TREE_PARENT, src="c", dst="a"),
            Edge(type=EdgeType.TREE_PARENT, src="c", dst="b"),
        ],
    )
    with pytest.raises(ValueError, match="more than one TREE_PARENT"):
        InMemoryStore.from_trace(nt)


# --- "is exactly what normalize() would produce" boundary tests -----------------------
#
# These guard the stronger validate_normalized contract: a NormalizedTrace at the load
# boundary must equal normalize(raw_layer). Anything less and a backend rebuilding from
# the raw layer (LadybugStore) would silently emit different bytes — drift the artifact
# format is meant to prevent.


def _canonical_two_step() -> NormalizedTrace:
    return normalize(
        _raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="a")])
    )


def test_validate_normalized_rejects_missing_belongs_to():
    nt = _canonical_two_step()
    stripped = nt.model_copy(
        update={"edges": [e for e in nt.edges if e.type is not EdgeType.BELONGS_TO]}
    )
    with pytest.raises(ValueError, match="canonical form"):
        validate_normalized(stripped)


def test_validate_normalized_rejects_missing_tree_parent():
    nt = _canonical_two_step()
    stripped = nt.model_copy(
        update={"edges": [e for e in nt.edges if e.type is not EdgeType.TREE_PARENT]}
    )
    with pytest.raises(ValueError, match="canonical form"):
        validate_normalized(stripped)


def test_validate_normalized_rejects_wrong_projection_lossy_flag():
    # The flag is a derived signal; a hand-edited artifact that claims a single-cause step
    # was lossy (or vice versa) would mislead RCA — catch it at the boundary.
    nt = _canonical_two_step()
    tampered_steps = [
        s.model_copy(update={"projection_lossy": True}) if s.step_id == "b" else s
        for s in nt.steps
    ]
    tampered = nt.model_copy(update={"steps": tampered_steps})
    with pytest.raises(ValueError, match="canonical form"):
        validate_normalized(tampered)


def test_validate_normalized_rejects_non_canonical_edge_order():
    # Same logical trace, edges shuffled out of canonical order — must be rejected so the
    # LadybugStore (which sorts queries canonically) can't silently disagree with a backend
    # that walked the edges in input order.
    nt = _canonical_two_step()
    shuffled = nt.model_copy(update={"edges": list(reversed(nt.edges))})
    with pytest.raises(ValueError, match="canonical form"):
        validate_normalized(shuffled)


def _corrupt_normalized() -> NormalizedTrace:
    # A NormalizedTrace whose RAW layer is broken: CAUSED_BY points to a missing step.
    return NormalizedTrace(
        trace=Trace(trace_id="t", source_kind="x"),
        steps=_steps(("a", 0), ("b", 1)),
        edges=[Edge(type=EdgeType.CAUSED_BY, src="b", dst="ghost")],
    )


def test_from_trace_validates_raw_layer():
    with pytest.raises(ValueError, match="not a known step"):
        InMemoryStore.from_trace(_corrupt_normalized())


def test_load_artifact_rejects_corrupt_raw_layer(tmp_path):
    # Both writers refuse this trace (below), so write it with the pure serializer: a file
    # from elsewhere can still hold it, and the store load must be the gate.
    path = tmp_path / "bad.json"
    path.write_text(artifact.dumps(_corrupt_normalized()), encoding="utf-8")
    with pytest.raises(ValueError, match="not a known step"):
        InMemoryStore.load_artifact(path)


def _changed_in_place(change: str) -> NormalizedTrace:
    # Pydantic does not revalidate on assignment: each change leaves a model the readers reject.
    nt = _canonical_two_step()
    edge = next(e for e in nt.edges if e.type is EdgeType.CAUSED_BY)
    if change == "seq":
        nt.steps[0].seq = "zero"  # type: ignore[assignment]
    elif change == "status":
        nt.steps[0].status = "maybe"  # type: ignore[assignment]
    elif change == "dangling-edge":
        edge.dst = "ghost"
    elif change == "str-type-dangling-edge":
        # A coercible type string hides the edge from enum-identity filters until validated.
        edge.type = "CAUSED_BY"  # type: ignore[assignment]
        edge.dst = "ghost"
    elif change == "stale-status":
        nt.trace.status = StepStatus.ERROR  # every step is ok
    elif change == "lossy-flag":
        nt.steps[1].projection_lossy = True
    elif change == "no-tree-parent":
        nt.edges = [e for e in nt.edges if e.type is not EdgeType.TREE_PARENT]
    else:  # dangling BELONGS_TO
        next(e for e in nt.edges if e.type is EdgeType.BELONGS_TO).dst = "ghost"
    return nt


@pytest.mark.filterwarnings("ignore::UserWarning")  # pydantic's serializer warns on the bad types
@pytest.mark.parametrize(
    "change",
    ["seq", "status", "dangling-edge", "str-type-dangling-edge", "stale-status", "lossy-flag",
     "no-tree-parent", "dangling-belongs-to"],
)
@pytest.mark.parametrize("writer", ["save", "save_atomic"])
def test_writers_refuse_a_trace_the_readers_would_reject(tmp_path, writer, change):
    target = tmp_path / "a.json"
    target.write_text("previous", encoding="utf-8")
    with pytest.raises(ValueError):
        getattr(artifact, writer)(_changed_in_place(change), target)
    assert target.read_text(encoding="utf-8") == "previous"
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]  # no temp file left behind


@pytest.mark.parametrize("writer", ["save", "save_atomic"])
def test_writers_still_write_a_valid_trace_byte_for_byte(tmp_path, writer):
    nt = _canonical_two_step()
    target = tmp_path / "a.json"
    getattr(artifact, writer)(nt, target)
    assert target.read_text(encoding="utf-8") == artifact.dumps(nt)
    assert artifact.load(target) == nt


# --- trace-level status is part of the system of record ------------------------------------


def test_validate_normalized_rejects_lying_trace_status():
    # A clean-looking status on a trace whose steps tell a different story would let an
    # artifact hide a failed run. The header is system-of-record truth, so reject it.
    nt = _canonical_two_step()  # no error steps -> status ok
    lying = nt.model_copy(
        update={"trace": nt.trace.model_copy(update={"status": StepStatus.ERROR})}
    )
    with pytest.raises(ValueError, match="disagrees with its steps"):
        validate_normalized(lying)


def test_normalize_derives_trace_status_from_steps():
    # normalize() makes trace.status a derived view of the steps regardless of what the raw
    # trace claimed: an erroring step always yields status=error, and the result self-checks.
    steps = [
        Step(step_id="a", trace_id="t", seq=0),
        Step(step_id="b", trace_id="t", seq=1, status=StepStatus.ERROR, error_msg="boom"),
    ]
    raw = RawTrace(
        trace=Trace(trace_id="t", source_kind="test", status=StepStatus.OK),  # raw lies: ok
        steps=steps,
        causal_edges=[Edge(type=EdgeType.CAUSED_BY, src="b", dst="a")],
    )
    nt = normalize(raw)
    assert nt.trace.status is StepStatus.ERROR
    validate_normalized(nt)  # the derived status passes its own consistency check


def test_from_obj_never_leaks_type_error_on_unserializable_payload():
    """from_obj documents "raises only ValueError"; the v1 migration leaked TypeError.

    The migration deep-copied through a JSON round-trip, which raises TypeError on any value
    json cannot serialize. TypeError is neither OSError nor ValueError, so it escaped every
    load-boundary handler and crashed the command with a raw traceback. from_obj takes an
    *already-parsed* payload, so a caller can legitimately hand it in-memory objects.
    """
    def v1(trace_body):
        return {"schema_version": 1, "trace": trace_body}

    # A value json can't serialize, in a field the model ignores: the payload is otherwise
    # a valid trace, so it must load — previously it raised TypeError during migration.
    loaded = artifact.from_obj(
        v1({
            "trace": {"trace_id": "t", "source_kind": "x"},
            "steps": [],
            "edges": [],
            "junk": {1, 2},
        })
    )
    assert loaded.trace.trace_id == "t"

    # The same unserializable value in a field the model *does* read is bad input, and must
    # surface as ValueError rather than TypeError.
    with pytest.raises(ValueError):
        artifact.from_obj(
            v1({"trace": {"trace_id": "t", "source_kind": "x"}, "steps": {1, 2}, "edges": []})
        )


def test_tree_cycle_error_names_the_lowest_seq_step_deterministically():
    """The message used to name a hash-order member of the cycle, so it varied per run."""
    steps = [
        Step(step_id="a", trace_id="t", seq=0),
        Step(step_id="b", trace_id="t", seq=1),
        Step(step_id="c", trace_id="t", seq=2),
    ]
    nt = normalize(
        RawTrace(
            trace=Trace(trace_id="t", source_kind="x"),
            steps=steps,
            causal_edges=[
                Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
                Edge(type=EdgeType.CAUSED_BY, src="c", dst="a"),
            ],
        )
    )
    nt.edges = [e for e in nt.edges if e.type is not EdgeType.TREE_PARENT] + [
        Edge(type=EdgeType.TREE_PARENT, src="b", dst="c"),
        Edge(type=EdgeType.TREE_PARENT, src="c", dst="b"),
    ]
    with pytest.raises(ValueError) as excinfo:
        validate_tree(nt)
    assert "'b'" in str(excinfo.value), str(excinfo.value)


def test_v1_migration_loads_a_deeply_nested_ignored_field():
    """The migration must not recurse over data it does not touch.

    A v1 artifact can carry a deeply nested value in a field the model ignores. Copying the
    whole payload — by JSON round-trip or by deepcopy — walked it and raised, so an artifact
    that used to load stopped loading. Both TypeError and RecursionError escape the load
    boundary, which only handles OSError and ValueError.
    """
    deep = json.loads('{"a":' * 600 + "1" + "}" * 600)
    loaded = artifact.from_obj(
        {
            "schema_version": 1,
            "trace": {
                "trace": {"trace_id": "t", "source_kind": "x"},
                "steps": [],
                "edges": [],
                "junk": deep,
            },
        }
    )
    assert loaded.trace.trace_id == "t"
    assert loaded.trace.causal_fidelity.value == "legacy_unknown"


def test_v1_migration_does_not_mutate_the_callers_payload():
    """Only the containers the migration writes to are copied; the caller's stay untouched."""
    header = {"trace_id": "t", "source_kind": "x"}
    edge = {"type": "CAUSED_BY", "src": "b", "dst": "a"}
    payload = {
        "schema_version": 1,
        "trace": {
            "trace": header,
            "steps": [
                {"step_id": "a", "trace_id": "t", "seq": 0},
                {"step_id": "b", "trace_id": "t", "seq": 1},
            ],
            "edges": [edge],
        },
    }
    artifact.from_obj(payload)
    assert "causal_fidelity" not in header
    assert "origin" not in edge


@pytest.mark.parametrize("sequence", [list, tuple, deque, iter])
def test_v1_migration_covers_every_accepted_edge_sequence(sequence):
    """The model accepts any iterable for its edge sequence, so the migration must too.

    Enumerating concrete types here meant each shape left off the list silently skipped
    migration, and its CAUSED_BY edges came back with no origin instead of the legacy
    default. A one-shot iterator additionally has to survive: iterating it in place consumed
    it before the model ever saw it.
    """
    loaded = artifact.from_obj(
        {
            "schema_version": 1,
            "trace": {
                "trace": {"trace_id": "t", "source_kind": "x"},
                "steps": [
                    {"step_id": "a", "trace_id": "t", "seq": 0},
                    {"step_id": "b", "trace_id": "t", "seq": 1},
                ],
                "edges": sequence([{"type": "CAUSED_BY", "src": "b", "dst": "a"}]),
            },
        }
    )
    origins = [edge.origin for edge in loaded.edges_of(EdgeType.CAUSED_BY)]
    assert [origin.value for origin in origins] == ["legacy_unknown"], origins


def test_v1_migration_leaves_a_non_sequence_edges_field_to_the_model():
    """A malformed edges field stays a clean ValueError, not a migration crash."""
    with pytest.raises(ValueError):
        artifact.from_obj(
            {
                "schema_version": 1,
                "trace": {
                    "trace": {"trace_id": "t", "source_kind": "x"},
                    "steps": [],
                    "edges": 7,
                },
            }
        )


def test_v1_migration_reports_a_failing_edge_iterator_as_value_error():
    """Consuming a caller-supplied lazy sequence must not leak its exception type.

    The v2 path already surfaces a failing iterator as a ValidationError, which is a
    ValueError; v1 must not differ, or the failure escapes every load-boundary handler.
    """
    def edges():
        yield {"type": "CAUSED_BY", "src": "b", "dst": "a"}
        raise RuntimeError("upstream went away")

    with pytest.raises(ValueError):
        artifact.from_obj(
            {
                "schema_version": 1,
                "trace": {
                    "trace": {"trace_id": "t", "source_kind": "x"},
                    "steps": [
                        {"step_id": "a", "trace_id": "t", "seq": 0},
                        {"step_id": "b", "trace_id": "t", "seq": 1},
                    ],
                    "edges": edges(),
                },
            }
        )


@pytest.mark.parametrize(
    ("declared", "expected"),
    [({}, "legacy_unknown"), ({"origin": "graph_parent"}, "graph_parent")],
)
def test_v1_migration_covers_mapping_shaped_edge_records(declared, expected):
    """The model accepts any mapping as an edge record, so the migration must too.

    Matching `dict` alone left these without the legacy origin default, while an equivalent
    plain dictionary got one. An origin the record already declares is never overwritten.
    """
    edge = UserDict({"type": "CAUSED_BY", "src": "b", "dst": "a", **declared})
    loaded = artifact.from_obj(
        {
            "schema_version": 1,
            "trace": {
                "trace": {"trace_id": "t", "source_kind": "x"},
                "steps": [
                    {"step_id": "a", "trace_id": "t", "seq": 0},
                    {"step_id": "b", "trace_id": "t", "seq": 1},
                ],
                "edges": [edge],
            },
        }
    )
    origins = [e.origin.value for e in loaded.edges_of(EdgeType.CAUSED_BY)]
    assert origins == [expected], origins


# --- artifact envelope version: content-stamped, not build-stamped ---


def _one_step_trace(**step_kwargs):
    from tracegraph.model import RawTrace, Step, Trace
    from tracegraph.normalize import normalize

    return normalize(
        RawTrace(
            trace=Trace(trace_id="t", source_kind="langgraph"),
            steps=[Step(step_id="a", trace_id="t", seq=0, **step_kwargs)],
        )
    )


def test_artifact_without_new_vocabulary_still_serializes_as_v2():
    """The envelope is inside the hash, so bumping it for unchanged content breaks digests.

    ``review_candidates`` binds each candidate to the sha256 of the artifact *file*, and those
    digests are already published in exported reports. An artifact that uses nothing new must
    therefore keep producing identical bytes.
    """
    from tracegraph import artifact

    assert json.loads(artifact.dumps(_one_step_trace()))["schema_version"] == 2


def test_artifact_with_a_derived_task_step_declares_v3():
    """A reader that would mis-parse these bytes should be told so, not handed a pydantic error."""
    from tracegraph import artifact
    from tracegraph.model import StepSource

    nt = _one_step_trace(source=StepSource.TASK)
    payload = json.loads(artifact.dumps(nt))
    assert payload["schema_version"] == 3
    assert artifact.loads(artifact.dumps(nt)).steps[0].source is StepSource.TASK


def test_v3_envelope_without_task_steps_still_loads():
    """Version is a floor on the reader, not a claim about the content."""
    from tracegraph import artifact

    text = artifact.dumps(_one_step_trace())
    payload = json.loads(text)
    payload["schema_version"] = 3
    assert artifact.from_obj(payload).steps[0].step_id == "a"


def test_unknown_future_version_is_refused_by_name():
    from tracegraph import artifact

    payload = json.loads(artifact.dumps(_one_step_trace()))
    payload["schema_version"] = 99
    with pytest.raises(ValueError, match="unsupported artifact schema_version"):
        artifact.from_obj(payload)


# --- artifact load boundary: version envelope and store edge endpoints -------------------


@pytest.mark.parametrize("version", [True, 1.0, 2.0, 4.0, "2"])
def test_schema_version_must_be_an_integer(version):
    # `True == 1` and `2.0 == 2`, so an equality check let these through; `true` even took
    # the v1 migration path. The envelope is written by `dumps` as an int, always.
    payload = json.loads(artifact.dumps(normalize(_raw(_steps(("a", 0)), []))))
    payload["schema_version"] = version
    with pytest.raises(ValueError, match="unsupported artifact schema_version"):
        artifact.from_obj(payload)


def _origin_priority_trace() -> NormalizedTrace:
    from tracegraph.model import EdgeOrigin

    raw = _raw(
        _steps(("a", 0), ("b", 1), ("c", 2), ("d", 3)),
        [
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="a", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="c", dst="a", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="b", origin=EdgeOrigin.SPAN_LINK),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="c", origin=EdgeOrigin.GRAPH_PARENT),
        ],
    )
    return normalize(raw, origin_priority=True)


def test_envelope_cannot_understate_an_origin_priority_projection():
    # An older reader trusts the envelope: if it says 2, the reader applies the temporal
    # projection and either rejects the file with a confusing canonical-form error or, worse,
    # a re-save silently rewrites it as a different (v4) file with a different digest.
    payload = json.loads(artifact.dumps(_origin_priority_trace()))
    assert payload["schema_version"] == 4
    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="declares schema_version 2 but its content requires 4"):
        artifact.from_obj(payload)


def test_envelope_cannot_understate_task_steps():
    from tracegraph.model import StepSource

    nt = normalize(
        _raw([Step(step_id="a", trace_id="t", seq=0, source=StepSource.TASK)], [])
    )
    payload = json.loads(artifact.dumps(nt))
    assert payload["schema_version"] == 3
    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="content requires 3"):
        artifact.from_obj(payload)


def test_overstated_envelope_still_loads():
    # Floor semantics: an envelope may claim more than its content needs (a v3 file with no
    # task steps exists in the wild); it may never claim less.
    payload = json.loads(artifact.dumps(normalize(_raw(_steps(("a", 0)), []))))
    payload["schema_version"] = 4
    assert artifact.from_obj(payload).steps[0].step_id == "a"


def test_non_canonical_trace_without_origins_is_not_stamped_v4():
    # The v4 stamp means "TREE_PARENT was chosen by origin priority". A trace with no edge
    # origins at all cannot need that, whatever else is wrong with it.
    nt = normalize(_raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="a")]))
    tampered = nt.model_copy(deep=True)
    tampered.trace.status = StepStatus.ERROR  # disagrees with the all-ok steps
    assert json.loads(artifact.dumps(nt))["schema_version"] == 2
    assert json.loads(artifact.dumps(tampered))["schema_version"] == 2
    # Same for a trace missing its derived edges entirely (a deliberate digest change for
    # non-canonical traces only; see CHANGELOG).
    bare = NormalizedTrace(trace=nt.trace, steps=nt.steps, edges=[])
    assert json.loads(artifact.dumps(bare))["schema_version"] == 2


def test_in_memory_store_rejects_dangling_edge_without_partial_write():
    store = InMemoryStore()
    store.init_schema()
    store.upsert_nodes(_steps(("a", 0), ("b", 1)))
    good = Edge(type=EdgeType.CAUSED_BY, src="b", dst="a")
    with pytest.raises(ValueError, match="ghost"):
        store.upsert_edges([good, Edge(type=EdgeType.CAUSED_BY, src="b", dst="ghost")])
    with pytest.raises(ValueError, match="ghost"):
        store.upsert_edges([good, Edge(type=EdgeType.TREE_PARENT, src="ghost", dst="a")])
    # All or nothing, like LadybugStore: the valid edge in the failed batch did not land.
    assert store.ancestors("b") == []


def test_in_memory_store_checks_belongs_to_against_the_trace_id():
    nt = normalize(_raw(_steps(("a", 0)), []))
    store = InMemoryStore.from_trace(nt)  # a one-step trace's BELONGS_TO must be accepted
    with pytest.raises(ValueError, match="other"):
        store.upsert_edges([Edge(type=EdgeType.BELONGS_TO, src="a", dst="other")])


def test_in_memory_node_upsert_still_replaces_by_id():
    # Documented contract (README): InMemory node upsert replaces by ID.
    store = InMemoryStore.from_trace(normalize(_raw(_steps(("a", 0)), [])))
    store.upsert_nodes([Step(step_id="a", trace_id="t", seq=0, name="renamed")])
    assert [s.name for s in store.trace().steps] == ["renamed"]


@pytest.mark.parametrize("edge_type", [EdgeType.WROTE, EdgeType.READ])
def test_in_memory_store_still_accepts_channel_edges(edge_type):
    # WROTE/READ point at a channel, not a step; the endpoint check must not apply to them.
    store = InMemoryStore.from_trace(normalize(_raw(_steps(("a", 0)), [])))
    store.upsert_edges([Edge(type=edge_type, src="a", dst="messages")])
    assert any(e.type is edge_type for e in store.trace().edges)


def test_envelope_check_does_not_depend_on_the_rest_of_the_trace_being_canonical():
    # An unrelated defect (here a stored status that disagrees with the steps) must not let
    # an origin-priority tree hide behind a v2 envelope; the check reads TREE_PARENT only.
    nt = _origin_priority_trace()
    payload = json.loads(artifact.dumps(nt))
    payload["trace"]["trace"]["status"] = "error"
    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="content requires 4"):
        artifact.from_obj(payload)
    # And the writer stamps it by its tree, not by its other defects.
    tampered = nt.model_copy(deep=True)
    tampered.trace.status = StepStatus.ERROR
    assert json.loads(artifact.dumps(tampered))["schema_version"] == 4


@pytest.mark.parametrize("writer", ["artifact", "report", "candidates", "memo"])
def test_every_atomic_writer_cleans_up_when_the_write_fails(tmp_path, monkeypatch, writer):
    # The artifact and report writers recorded the temp file's name only after writing it,
    # so a failing write or fsync left `.name.*` behind; the memo writer did not. One helper.
    import os

    from tracegraph.analysis import diagnose, memo
    from tracegraph import review_candidates

    nt = normalize(_raw(_steps(("a", 0)), []))
    target = tmp_path / "out.json"
    save = {
        "artifact": lambda: artifact.save_atomic(nt, target),
        "report": lambda: diagnose.save_atomic(diagnose.analyze(nt), target),
        "candidates": lambda: review_candidates.save_atomic(
            review_candidates.ReviewCandidateReport(), target
        ),
        "memo": lambda: memo.save_incident_memo("x", target),
    }[writer]

    def failing_fsync(fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="disk full"):
        save()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.filterwarnings("ignore::UserWarning")  # pydantic's serializer warns on the str seq
@pytest.mark.parametrize("writer", ["save", "save_atomic"])
def test_writers_write_the_validated_value_of_a_coercible_assignment(tmp_path, writer):
    # "0" is what the model would read as 0: write that, rather than crash on "0" < 1.
    nt = _canonical_two_step()
    nt.steps[0].seq = "0"  # type: ignore[assignment]
    target = tmp_path / "a.json"
    getattr(artifact, writer)(nt, target)
    assert artifact.load(target) == _canonical_two_step()


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.parametrize("break_it", ["steps=None", "steps[0]=None", "trace=None", "steps[0]=object"])
@pytest.mark.parametrize("writer", ["save", "save_atomic"])
def test_writers_refuse_a_replaced_container_with_a_value_error(tmp_path, writer, break_it):
    nt = _canonical_two_step()
    if break_it == "steps=None":
        nt.steps = None  # type: ignore[assignment]
    elif break_it == "steps[0]=None":
        nt.steps[0] = None  # type: ignore[call-overload]
    elif break_it == "trace=None":
        nt.trace = None  # type: ignore[assignment]
    else:
        nt.steps[0] = object()  # type: ignore[call-overload]
    target = tmp_path / "a.json"
    target.write_text("previous", encoding="utf-8")
    with pytest.raises(ValueError):
        getattr(artifact, writer)(nt, target)
    assert target.read_text(encoding="utf-8") == "previous"
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]
