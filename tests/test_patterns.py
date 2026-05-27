"""Cross-trace pattern matching over the raw causal graph."""

from tracegraph.analysis import PRESETS, PathPattern, StepPredicate, find_matches, search
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


def _trace(trace_id: str, *specs):
    """Build a linear trace from (name, kind, status) specs: specs[0] is the root."""
    steps, edges = [], []
    for i, (name, kind, status) in enumerate(specs):
        steps.append(Step(step_id=f"{trace_id}{i}", trace_id=trace_id, seq=i,
                          name=name, kind=kind, status=status))
        if i:
            edges.append(Edge(type=EdgeType.CAUSED_BY, src=f"{trace_id}{i}", dst=f"{trace_id}{i-1}"))
    return normalize(RawTrace(trace=Trace(trace_id=trace_id, source_kind="x"),
                              steps=steps, causal_edges=edges))


OK = StepStatus.OK
ERR = StepStatus.ERROR


def _erroring_tool_trace(tid="A"):
    return _trace(tid,
                  ("input", StepKind.CHAIN, OK),
                  ("plan", StepKind.CHAIN, OK),
                  ("call_tool", StepKind.TOOL, ERR),
                  ("respond", StepKind.CHAIN, OK))


def _clean_trace(tid="B"):
    return _trace(tid,
                  ("input", StepKind.CHAIN, OK),
                  ("plan", StepKind.CHAIN, OK),
                  ("call_tool", StepKind.TOOL, OK),
                  ("respond", StepKind.CHAIN, OK))


def test_single_predicate_match():
    nt = _erroring_tool_trace()
    matches = find_matches(nt, PathPattern((StepPredicate(kind=StepKind.TOOL, status=ERR),)))
    assert len(matches) == 1
    assert nt.steps_by_id()[matches[0][0]].name == "call_tool"


def test_multi_step_causal_path_match():
    nt = _erroring_tool_trace()
    pattern = PathPattern((StepPredicate(name="plan"),
                           StepPredicate(kind=StepKind.TOOL, status=ERR)))
    matches = find_matches(nt, pattern)
    assert len(matches) == 1
    assert [nt.steps_by_id()[s].name for s in matches[0]] == ["plan", "call_tool"]


def test_no_false_match_when_status_differs():
    # clean trace has a TOOL step but it didn't error -> tool-failure must not match
    assert find_matches(_clean_trace(), PRESETS["tool-failure"]) == []


def test_non_contiguous_sequence_does_not_match():
    # plan and respond are not adjacent (call_tool is between) -> no match
    nt = _erroring_tool_trace()
    pattern = PathPattern((StepPredicate(name="plan"), StepPredicate(name="respond")))
    assert find_matches(nt, pattern) == []


def test_cross_trace_search_filters():
    traces = [_erroring_tool_trace("A"), _clean_trace("B")]
    matches = search(traces, PRESETS["tool-failure"])
    assert {m.trace_id for m in matches} == {"A"}  # only the erroring trace


def test_preset_labels_are_readable():
    matches = search([_erroring_tool_trace("A")], PRESETS["plan-then-tool-failure"])
    assert len(matches) == 1
    assert matches[0].labels == ["plan", "call_tool"]
