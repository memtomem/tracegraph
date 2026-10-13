"""Contract + artifact round-trip."""

import json

import pytest

from tracegraph import artifact
from tracegraph.model import (
    CausalFidelity,
    DecisionEvidence,
    Edge,
    EdgeType,
    RawTrace,
    Step,
    StepKind,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize


def _raw() -> RawTrace:
    return RawTrace(
        trace=Trace(trace_id="t1", source_kind="langgraph", thread_id="A"),
        steps=[
            Step(step_id="a", trace_id="t1", seq=0, kind=StepKind.CHAIN),
            Step(step_id="b", trace_id="t1", seq=1, kind=StepKind.TOOL, status=StepStatus.ERROR),
        ],
        causal_edges=[Edge(type=EdgeType.CAUSED_BY, src="b", dst="a")],
    )


def test_artifact_round_trip_is_stable():
    nt = normalize(_raw())
    text = artifact.dumps(nt)
    again = artifact.loads(text)
    assert again == nt
    # dumps is deterministic (sorted keys) -> re-serializing the parsed trace matches.
    assert artifact.dumps(again) == text
    assert json.loads(text)["schema_version"] == 2


def test_external_decision_evidence_round_trips_without_becoming_an_edge():
    raw = _raw()
    raw.trace.decision_evidence = [
        DecisionEvidence(
            artifact_digest="sha256:" + "b" * 64,
            graph_generation=3,
            verdict="review",
        )
    ]
    nt = normalize(raw)
    loaded = artifact.loads(artifact.dumps(nt))
    assert loaded.trace.decision_evidence == raw.trace.decision_evidence
    assert all(edge.type is not EdgeType.CAUSED_BY or edge.src != "toolgraph" for edge in loaded.edges)


def test_v1_artifact_is_migrated_in_memory_without_rewriting():
    payload = json.loads(artifact.dumps(normalize(_raw())))
    payload["schema_version"] = 1
    payload["trace"]["trace"].pop("causal_fidelity")
    payload["trace"]["trace"].pop("links_preserved")
    for step in payload["trace"]["steps"]:
        step.pop("evidence")
    for edge in payload["trace"]["edges"]:
        edge.pop("origin")
    loaded = artifact.loads(json.dumps(payload))
    assert loaded.trace.causal_fidelity is CausalFidelity.LEGACY_UNKNOWN
    assert loaded.edges_of(EdgeType.CAUSED_BY)[0].origin.value == "legacy_unknown"


def test_save_load(tmp_path):
    nt = normalize(_raw())
    path = tmp_path / "trace.json"
    artifact.save(nt, path)
    assert artifact.load(path) == nt


def test_edge_direction_is_effect_to_cause():
    nt = normalize(_raw())
    (e,) = nt.edges_of(EdgeType.CAUSED_BY)
    # CAUSED_BY points effect -> cause: b (effect) caused by a (cause).
    assert e.src == "b" and e.dst == "a"


def test_unknown_schema_version_rejected():
    with pytest.raises(ValueError):
        artifact.loads('{"schema_version": 999, "trace": {}}')


# --- non-finite floats never reach an artifact --------------------------------------------


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_evidence_floats_must_be_finite(value):
    from tracegraph.model import EvaluationSummary, StepEvidence

    with pytest.raises(ValueError):
        StepEvidence(duration_ms=value)
    with pytest.raises(ValueError):
        EvaluationSummary(name="quality", score=value)


def test_artifact_with_nan_token_is_rejected_on_load():
    # Python's json accepts the non-standard `NaN` token; the artifact contract does not.
    nt = normalize(
        RawTrace(trace=Trace(trace_id="t", source_kind="x"), steps=[Step(step_id="a", trace_id="t", seq=0)])
    )
    payload = json.loads(artifact.dumps(nt))
    payload["trace"]["steps"][0]["evidence"] = {"duration_ms": "__NAN__"}
    text = json.dumps(payload).replace('"__NAN__"', "NaN")
    with pytest.raises(ValueError):
        artifact.loads(text)


def test_dumps_refuses_to_write_nan():
    # A model built without validation (model_construct, attribute assignment) can still
    # carry NaN; the writer must refuse rather than emit a file strict JSON parsers reject.
    from tracegraph.model import StepEvidence

    nt = normalize(
        RawTrace(trace=Trace(trace_id="t", source_kind="x"), steps=[Step(step_id="a", trace_id="t", seq=0)])
    )
    nt.steps[0].evidence = StepEvidence.model_construct(duration_ms=float("nan"))
    with pytest.raises(ValueError):
        artifact.dumps(nt)


def test_cli_save_translates_a_serialization_error_into_a_usage_error(tmp_path):
    import typer

    from tracegraph import cli
    from tracegraph.model import StepEvidence

    nt = normalize(
        RawTrace(trace=Trace(trace_id="t", source_kind="x"), steps=[Step(step_id="a", trace_id="t", seq=0)])
    )
    nt.steps[0].evidence = StepEvidence.model_construct(duration_ms=float("nan"))
    for save in (lambda: cli._save_artifact(nt, tmp_path / "a.json"),
                 lambda: cli._save_ingested(nt, tmp_path / "b.json", explicit=True)):
        with pytest.raises(typer.BadParameter, match="cannot save artifact"):
            save()
    assert list(tmp_path.iterdir()) == []
