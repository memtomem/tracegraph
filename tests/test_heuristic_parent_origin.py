"""A graph parent chosen by start order is recorded per edge, not as a declared cause (#142).

``analyze`` treats only declared origins as error ancestry, so a loop parent taken among several
candidates by start order no longer decides whether a failure is primary or propagated. The edge
keeps the tree rank of a declared graph parent, so the derived tree is unchanged, and an artifact
carrying it is stamped schema 8. Artifacts written before the origin existed are read as before.
"""

import json

import pytest
from test_capture_provenance import gap_counts
from test_heuristic_parent_disclosure import _by_timing, _span, _trace

from tracegraph import artifact
from tracegraph.adapters import OTLPSpanAdapter
from tracegraph.analysis.diagnose import analyze
from tracegraph.analysis.memo import build_incident_memo
from tracegraph.model import EdgeOrigin, EdgeType
from tracegraph.normalize import normalize, raw_layer

T0 = 1_700_000_000_000_000_000
LOOP = [("graph.node.id", "loop")]
CHILD = [("graph.node.parent_id", "loop")]
DIGEST = "sha256:" + "0" * 64
TIMED = "otlp.graph_parent_by_timing"


def _origin(nt, effect, cause):
    [origin] = [e.origin for e in nt.edges_of(EdgeType.CAUSED_BY) if (e.src, e.dst) == (effect, cause)]
    return origin


def _tree_parent(nt, sid):
    [parent] = [e.dst for e in nt.edges_of(EdgeType.TREE_PARENT) if e.src == sid]
    return parent


def _split(nt):
    report = analyze(nt)
    return (
        [f.step.step_id for f in report.primary_failures],
        [s.step_id for s in report.propagated_failures],
    )


def _issue_loop(*, b_error, links=()):
    """The #142 reproduction: c names loop as its graph parent, a and b carry it, b is taken."""
    return _trace([
        _span("r", T0, parent=None),
        _span("a", T0 + 100, attrs=LOOP),
        _span("b", T0 + 200, attrs=LOOP, error=b_error),
        _span("c", T0 + 300, attrs=CHILD, error=True, links=list(links)),
    ])


def _legacy(nt):
    """``nt`` as 0.8.0 wrote it: the timed edge says graph_parent, under a v7 envelope."""
    payload = json.loads(artifact.dumps(nt))
    assert payload["schema_version"] == 8
    payload["schema_version"] = 7
    for edge in payload["trace"]["edges"]:
        if edge.get("origin") == "graph_parent_heuristic":
            edge["origin"] = "graph_parent"
    return artifact.from_obj(payload)


# --- adapter ------------------------------------------------------------------------------------


def test_the_timed_choice_is_recorded_on_its_edge_and_still_counted():
    nt = _issue_loop(b_error=False)
    assert _origin(nt, "c", "b") is EdgeOrigin.GRAPH_PARENT_HEURISTIC
    assert gap_counts(nt) == {TIMED: 1}


def test_a_single_candidate_stays_a_declared_graph_parent():
    nt = _trace([
        _span("r", T0, parent=None),
        _span("p", T0 + 100, attrs=[("graph.node.id", "plan")]),
        _span("x", T0 + 200, attrs=[("graph.node.parent_id", "plan")]),
    ])
    assert _origin(nt, "x", "p") is EdgeOrigin.GRAPH_PARENT
    assert gap_counts(nt) == {}


def test_the_tree_is_the_one_a_declared_graph_parent_gives():
    nt = _issue_loop(b_error=False, links=["r"])
    assert _tree_parent(nt, "c") == "b"
    # Read under the old origin, the same raw layer projects the same tree.
    legacy = normalize(raw_layer(_legacy(nt)))
    assert legacy.edges_of(EdgeType.TREE_PARENT) == nt.edges_of(EdgeType.TREE_PARENT)


# --- analyze ------------------------------------------------------------------------------------


def test_a_timed_parent_that_failed_no_longer_makes_its_child_propagated():
    # Before: primary [b], propagated [c] — only because b was the one picked by start time.
    assert _split(_issue_loop(b_error=True)) == (["c", "b"], [])
    assert _split(_issue_loop(b_error=False)) == (["c"], [])


def test_a_declared_chain_beside_a_timed_loop_still_propagates():
    nt = _trace([
        _span("r", T0, parent=None),
        _span("p", T0 + 10, attrs=[("graph.node.id", "plan")]),
        _span("x", T0 + 20, attrs=[("graph.node.id", "exec"), ("graph.node.parent_id", "plan")], error=True),
        _span("y", T0 + 30, attrs=[("graph.node.id", "report"), ("graph.node.parent_id", "exec")], error=True),
        _span("a", T0 + 100, attrs=LOOP),
        _span("b", T0 + 200, attrs=LOOP),
        _span("c", T0 + 300, attrs=CHILD),
    ])
    assert gap_counts(nt) == {TIMED: 1}
    assert _split(nt) == (["x"], ["y"])


def test_the_timed_edge_stays_in_the_finding_with_its_origin():
    report = analyze(_issue_loop(b_error=False))
    [finding] = report.primary_failures
    assert [s.step_id for s in finding.causal_steps] == ["r", "b"]
    assert {(e.effect, e.cause): e.origin for e in finding.causal_edges}[("c", "b")] == "graph_parent_heuristic"


def test_a_legacy_artifact_is_classified_as_before():
    # No per-edge record: reinterpreting every graph parent would demote declared ones too.
    assert _split(_legacy(_issue_loop(b_error=True))) == (["b"], ["c"])


# --- a link that names the chosen parent --------------------------------------------------------


def test_a_link_to_the_chosen_parent_makes_it_declared():
    # The link to r is an earlier second cause: a SPAN_LINK rank for c→b would make r the tree parent.
    nt = _issue_loop(b_error=True, links=["b", "r"])
    assert _origin(nt, "c", "b") is EdgeOrigin.GRAPH_PARENT
    assert _tree_parent(nt, "c") == "b"
    assert TIMED not in gap_counts(nt)
    assert _split(nt) == (["b"], ["c"])
    assert json.loads(artifact.dumps(nt))["schema_version"] == 7


def test_only_the_corroborated_choice_leaves_the_count():
    nt = _trace([
        _span("r", T0, parent=None),
        _span("a", T0 + 100, attrs=LOOP),
        _span("b", T0 + 200, attrs=LOOP),
        _span("c", T0 + 300, attrs=CHILD, links=["b"]),
        _span("d", T0 + 400, attrs=CHILD),
    ])
    assert _origin(nt, "c", "b") is EdgeOrigin.GRAPH_PARENT
    [d_parent] = [e for e in nt.edges_of(EdgeType.CAUSED_BY) if e.src == "d"]
    assert d_parent.origin is EdgeOrigin.GRAPH_PARENT_HEURISTIC
    assert gap_counts(nt) == {TIMED: 1}


def _doc(spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def _with_link(link):
    spans = [
        _span("r", T0, parent=None),
        _span("a", T0 + 100, attrs=LOOP),
        _span("b", T0 + 200, attrs=LOOP),
        _span("c", T0 + 300, attrs=CHILD, error=True),
    ]
    spans[3]["links"] = [link]
    return spans


@pytest.mark.parametrize(
    "link, kwargs",
    [
        ({"traceId": "t", "spanId": "b"}, {"links_as_causes": False}),
        ({"traceId": "other", "spanId": "b"}, {}),
        ({"spanId": "b"}, {}),
        ({"traceId": "t", "spanId": "missing"}, {}),
        ({"traceId": "t", "spanId": "a"}, {}),
    ],
    ids=["links-off", "foreign", "unscoped", "unresolved", "other-candidate"],
)
def test_a_link_that_does_not_name_the_chosen_parent_does_not_corroborate(link, kwargs):
    nt = normalize(OTLPSpanAdapter(_doc(_with_link(link)), **kwargs).ingest("t"))
    assert _origin(nt, "c", "b") is EdgeOrigin.GRAPH_PARENT_HEURISTIC
    assert gap_counts(nt)[TIMED] == 1


def _numbered(links=()):
    """Unreadable times with start-order numbers on the private channel: a ``by_order`` choice."""
    spans = []
    for sid, order, attrs, parent in [
        ("r", 0, [], None), ("a", 1, LOOP, "r"), ("b", 2, LOOP, "r"), ("c", 3, CHILD, "r"),
    ]:
        span = _span(sid, T0, parent=parent, attrs=attrs)
        del span["startTimeUnixNano"]
        span["_order"] = order
        spans.append(span)
    spans[3]["links"] = [{"traceId": "t", "spanId": target} for target in links]
    return normalize(OTLPSpanAdapter(_doc(spans), start_order_channel="_order").ingest("t"))


def test_a_choice_by_start_order_numbers_is_recorded_and_corroborated_alike():
    nt = _numbered()
    assert _origin(nt, "c", "b") is EdgeOrigin.GRAPH_PARENT_HEURISTIC
    assert gap_counts(nt)[TIMED] == 1
    nt = _numbered(links=["b"])
    assert _origin(nt, "c", "b") is EdgeOrigin.GRAPH_PARENT
    assert TIMED not in gap_counts(nt)


# --- artifact -----------------------------------------------------------------------------------


def test_the_heuristic_origin_is_stamped_8_and_round_trips():
    nt = _issue_loop(b_error=True)
    text = artifact.dumps(nt)
    assert json.loads(text)["schema_version"] == 8
    assert artifact.dumps(artifact.loads(text)) == text


def test_an_envelope_that_understates_the_origin_is_refused():
    payload = json.loads(artifact.dumps(_issue_loop(b_error=True)))
    payload["schema_version"] = 7
    with pytest.raises(ValueError, match="content requires 8"):
        artifact.from_obj(payload)


def test_a_trace_without_a_heuristic_edge_keeps_its_version_and_bytes():
    nt = _trace([_span("r", T0, parent=None), _span("c", T0 + 100, error=True)])
    text = artifact.dumps(nt)
    assert json.loads(text)["schema_version"] == 7
    assert "graph_parent_heuristic" not in text
    legacy = artifact.dumps(_legacy(_issue_loop(b_error=True)))
    assert json.loads(legacy)["schema_version"] == 7
    assert artifact.dumps(artifact.loads(legacy)) == legacy


def test_ladybug_round_trips_the_heuristic_origin(tmp_path):
    pytest.importorskip("ladybug")
    from tracegraph.store.ladybug import LadybugStore

    nt = _issue_loop(b_error=True)
    with LadybugStore.from_trace(nt) as store:
        store.export_artifact(tmp_path / "out.json")
    assert (tmp_path / "out.json").read_text() == artifact.dumps(nt)


# --- memo ---------------------------------------------------------------------------------------


def _ancestry(memo):
    section = memo.split("### Causal Ancestry Graph\n", 1)[1].split("\n\n", 1)[0]
    return section.splitlines()


def test_the_memo_qualifies_only_the_timed_edge():
    nt = _trace([
        _span("r", T0, parent=None),
        _span("p", T0 + 10, attrs=[("graph.node.id", "plan")]),
        _span("x", T0 + 20, attrs=[("graph.node.parent_id", "plan")], error=True),
        _span("a", T0 + 100, attrs=LOOP),
        _span("b", T0 + 200, attrs=LOOP),
        _span("c", T0 + 300, attrs=CHILD, error=True),
    ])
    lines = _ancestry(build_incident_memo(nt, DIGEST))
    declared = [line for line in lines if "[origin: graph_parent]" in line]
    timed = [line for line in lines if "[origin: graph_parent_heuristic]" in line]
    assert declared and all(" caused by " in line for line in declared)
    assert timed and all(" preceded by (graph parent, chosen heuristically) " in line for line in timed)
    assert not any("possibly" in line for line in lines)


def test_a_timed_edge_outside_the_failure_still_marks_the_artifact_as_per_edge():
    # Only the declared chain is printed. Had the legacy check scanned just the printed edges, it
    # would find no heuristic origin and qualify the declared graph parent as possibly timed.
    nt = _trace([
        _span("r", T0, parent=None),
        _span("p", T0 + 10, attrs=[("graph.node.id", "plan")]),
        _span("x", T0 + 20, attrs=[("graph.node.parent_id", "plan")], error=True),
        _span("a", T0 + 100, attrs=LOOP),
        _span("b", T0 + 200, attrs=LOOP),
        _span("c", T0 + 300, attrs=CHILD),
    ])
    lines = _ancestry(build_incident_memo(nt, DIGEST))
    assert not any("graph_parent_heuristic" in line for line in lines)
    declared = [line for line in lines if "[origin: graph_parent]" in line]
    assert declared and all(" caused by " in line for line in declared)


def test_the_memo_reads_a_legacy_artifact_as_before():
    lines = _ancestry(build_incident_memo(_legacy(_by_timing()), DIGEST))
    graph = [line for line in lines if "[origin: graph_parent]" in line]
    assert graph and all("preceded by (graph parent, possibly chosen heuristically)" in line for line in graph)
