"""Contract + artifact round-trip."""

import pytest

from tracegraph import artifact
from tracegraph.model import (
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
