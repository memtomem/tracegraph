"""OTLP/OpenInference adapter: span export -> raw causal graph -> normalized layers.

The headline case this source exercises that LangGraph cannot: a real multi-parent
fan-in (``synthesize`` caused by both its graph parent and a linked retriever span),
which must be flagged ``projection_lossy`` yet still fully recoverable via ``explain``.
"""

import json

import pytest
from otlp_agent_trace import sample_otlp_document

from tracegraph.adapters import OTLPSpanAdapter
from tracegraph.adapters.otlp_spans import _decimal_attr, _int_attr
from tracegraph.analysis import explain
from tracegraph.model import EdgeType, StepKind, StepStatus
from tracegraph.normalize import normalize
from tracegraph.store import InMemoryStore


def _adapter() -> OTLPSpanAdapter:
    return OTLPSpanAdapter(sample_otlp_document())


def _caused(nt_or_raw) -> set[tuple[str, str]]:
    edges = nt_or_raw.causal_edges if hasattr(nt_or_raw, "causal_edges") else nt_or_raw.edges_of(EdgeType.CAUSED_BY)
    return {(e.src, e.dst) for e in edges}


def test_discover_lists_distinct_traces():
    assert _adapter().discover() == ["agent-trace-1"]


def test_syncmill_run_id_is_preserved_when_consistent():
    def attr(value):
        return {"key": "syncmill.run_id", "value": {"stringValue": value}}

    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "a", "attributes": [attr("run-1")]},
        {"traceId": "t", "spanId": "b", "parentSpanId": "a", "name": "b",
         "attributes": [attr("run-1")]},
    ]}]}]}
    assert OTLPSpanAdapter(doc).ingest("t").trace.run_id == "run-1"


def test_conflicting_syncmill_run_ids_are_rejected():
    def attr(value):
        return {"key": "syncmill.run_id", "value": {"stringValue": value}}

    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "a", "attributes": [attr("run-1")]},
        {"traceId": "t", "spanId": "b", "parentSpanId": "a", "name": "b",
         "attributes": [attr("run-2")]},
    ]}]}]}
    with pytest.raises(ValueError, match="conflicting syncmill.run_id"):
        OTLPSpanAdapter(doc).ingest("t")


def test_whitespace_in_syncmill_run_id_is_rejected_at_ingest():
    attr = {"key": "syncmill.run_id", "value": {"stringValue": "run abc"}}
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "a", "attributes": [attr]},
    ]}]}]}
    with pytest.raises(ValueError, match="whitespace-containing"):
        OTLPSpanAdapter(doc).ingest("t")


def test_toolgraph_preflight_evidence_is_preserved_as_noncausal_metadata():
    attrs = [
        {
            "key": "toolgraph.preflight.artifact_digest",
            "value": {"stringValue": "sha256:" + "a" * 64},
        },
        {"key": "toolgraph.graph_generation", "value": {"intValue": "7"}},
        {"key": "toolgraph.preflight.verdict", "value": {"stringValue": "review"}},
    ]
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "run", "attributes": attrs},
    ]}]}]}
    raw = OTLPSpanAdapter(doc).ingest("t")
    assert raw.trace.decision_evidence[0].artifact_digest == "sha256:" + "a" * 64
    assert raw.trace.decision_evidence[0].graph_generation == 7
    assert raw.trace.decision_evidence[0].verdict == "review"
    assert raw.causal_edges == []


@pytest.mark.parametrize(
    "attrs, message",
    [
        ([{"key": "toolgraph.graph_generation", "value": {"intValue": "1"}}], "together"),
        ([
            {"key": "toolgraph.preflight.artifact_digest", "value": {"stringValue": "bad"}},
            {"key": "toolgraph.graph_generation", "value": {"intValue": "1"}},
            {"key": "toolgraph.preflight.verdict", "value": {"stringValue": "review"}},
        ], "invalid Toolgraph preflight digest"),
    ],
)
def test_partial_or_invalid_toolgraph_evidence_is_rejected(attrs, message):
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "run", "attributes": attrs},
    ]}]}]}
    with pytest.raises(ValueError, match=message):
        OTLPSpanAdapter(doc).ingest("t")


def test_invalid_syncmill_artifact_digest_is_rejected():
    attrs = [
        {"key": "syncmill.artifact_digest", "value": {"stringValue": "/tmp/result.patch"}}
    ]
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "attempt", "attributes": attrs},
    ]}]}]}
    with pytest.raises(ValueError, match="lowercase sha256"):
        OTLPSpanAdapter(doc).ingest("t")


def test_numeric_attribute_aliases_fall_through_invalid_primary_values():
    assert _int_attr(
        {"llm.token_count.prompt": "invalid", "llm.token_count.input": 7},
        "llm.token_count.prompt",
        "llm.token_count.input",
    ) == 7
    assert _decimal_attr(
        {"llm.cost.prompt": "invalid", "llm.cost.input": "0.25"},
        "llm.cost.prompt",
        "llm.cost.input",
    ) == "0.25"


def test_ingest_unknown_trace_raises():
    with pytest.raises(KeyError):
        _adapter().ingest("nope")


def test_openinference_span_kinds_mapped():
    kinds = {s.name: s.kind for s in _adapter().ingest("agent-trace-1").steps}
    assert kinds["agent"] is StepKind.AGENT
    assert kinds["retrieve_docs"] is StepKind.RETRIEVER
    assert kinds["web_search"] is StepKind.TOOL
    assert kinds["synthesize"] is StepKind.LLM


def test_graph_node_parent_overrides_span_nesting():
    # All four leaf spans are span-children of `agent` (s0), but graph.node.parent_id makes
    # their logical parent `plan` (s1). The adapter records the logical causal edge and treats
    # span-nesting as containment-only (NOT a second cause) — by design, so hierarchy doesn't
    # flag every span projection_lossy.
    caused = _caused(_adapter().ingest("agent-trace-1"))
    assert ("s1", "s0") in caused          # plan caused_by agent
    assert ("s2", "s1") in caused          # retrieve caused_by plan, NOT the span parent agent
    assert ("s2", "s0") not in caused      # span-nesting containment is not recorded as a cause


def test_link_creates_fan_in_flagged_lossy():
    nt = normalize(_adapter().ingest("agent-trace-1"))
    caused = _caused(nt)
    # synthesize has TWO real causes: its graph parent (plan) and the linked retriever
    assert ("s4", "s1") in caused and ("s4", "s2") in caused
    assert nt.steps_by_id()["s4"].projection_lossy
    # the derived tree keeps only the earliest cause (plan, seq 1) and drops the link
    tree = {(e.src, e.dst) for e in nt.edges_of(EdgeType.TREE_PARENT)}
    assert ("s4", "s1") in tree and ("s4", "s2") not in tree


def test_error_status_detected_with_message():
    errors = [s for s in _adapter().ingest("agent-trace-1").steps if s.status is StepStatus.ERROR]
    assert [s.name for s in errors] == ["web_search"]
    assert "timeout" in (errors[0].error_msg or "")


def test_explain_recovers_both_causes_and_flags_lossy():
    store = InMemoryStore.from_raw(_adapter().ingest("agent-trace-1"))
    result = explain(store, "s4")
    assert {"s1", "s2", "s0"} <= {s.step_id for s in result.chain}  # both causes + root
    assert result.is_lossy  # because s4 itself was a multi-cause fan-in


def test_explain_clean_chain_is_not_lossy():
    store = InMemoryStore.from_raw(_adapter().ingest("agent-trace-1"))
    # the failing tool has a single-cause chain back to the root: nothing collapsed
    assert not explain(store, "s3").is_lossy


def test_seq_is_monotonic_and_causes_precede_effects():
    raw = _adapter().ingest("agent-trace-1")
    seq = {s.step_id: s.seq for s in raw.steps}
    assert all(seq[e.dst] < seq[e.src] for e in raw.causal_edges)


def test_dangling_parent_span_refuses_to_fabricate_root():
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "parentSpanId": "ghost", "name": "x",
         "startTimeUnixNano": "5", "attributes": []},
    ]}]}]}
    with pytest.raises(ValueError, match="fabricate"):
        OTLPSpanAdapter(doc).ingest("t")


def test_link_without_traceid_is_not_treated_as_local_cause():
    # A malformed link (no traceId) that happens to name an existing in-trace span must NOT
    # become a cause — silently localizing it would fabricate causality.
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "a", "startTimeUnixNano": "10", "attributes": []},
        {"traceId": "t", "spanId": "c", "parentSpanId": "a", "name": "c", "startTimeUnixNano": "15", "attributes": []},
        {"traceId": "t", "spanId": "b", "parentSpanId": "a", "name": "b", "startTimeUnixNano": "20",
         "attributes": [], "links": [{"spanId": "c"}]},
    ]}]}]}
    caused = _caused(OTLPSpanAdapter(doc).ingest("t"))
    assert ("b", "c") not in caused             # the trace-less link is ignored, not localized
    assert caused == {("c", "a"), ("b", "a")}   # only the structural parents remain


def test_unique_graph_parent_kept_even_when_it_starts_later():
    # The one span carrying graph.node.id='leaf' starts AFTER its declared child. A clock gate
    # would silently drop the declared graph parent; we keep it and let topo order fix seq.
    def attr(k, v):
        return {"key": k, "value": {"stringValue": v}}

    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "p", "name": "p", "startTimeUnixNano": "10", "attributes": [attr("graph.node.id", "root")]},
        {"traceId": "t", "spanId": "c", "parentSpanId": "p", "name": "c", "startTimeUnixNano": "20",
         "attributes": [attr("graph.node.id", "c"), attr("graph.node.parent_id", "leaf")]},
        {"traceId": "t", "spanId": "l", "parentSpanId": "p", "name": "l", "startTimeUnixNano": "30", "attributes": [attr("graph.node.id", "leaf")]},
    ]}]}]}
    raw = OTLPSpanAdapter(doc).ingest("t")
    assert ("c", "l") in _caused(raw)           # declared graph parent preserved, not dropped to a root
    seq = {s.step_id: s.seq for s in raw.steps}
    assert seq["l"] < seq["c"]                  # topo order puts the cause first despite the clock
    normalize(raw)


def _attr(k, v):
    return {"key": k, "value": {"stringValue": v}}


def test_unresolvable_graph_parent_without_span_parent_raises():
    # graph.node.parent_id declares a logical parent, but no span carries that graph.node.id
    # and there is no parentSpanId — honoring the contract means raising, not rooting it.
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "c", "name": "c", "startTimeUnixNano": "10",
         "attributes": [_attr("graph.node.parent_id", "missing")]},
    ]}]}]}
    with pytest.raises(ValueError, match="cannot honor"):
        OTLPSpanAdapter(doc).ingest("t")


def test_unresolvable_graph_parent_falls_back_to_span_parent():
    # same unresolvable logical parent, but a real parentSpanId exists: fall back to the
    # structural parent (best-effort) rather than raise or fabricate a root.
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "p", "name": "p", "startTimeUnixNano": "10", "attributes": []},
        {"traceId": "t", "spanId": "c", "parentSpanId": "p", "name": "c", "startTimeUnixNano": "20",
         "attributes": [_attr("graph.node.parent_id", "missing")]},
    ]}]}]}
    assert _caused(OTLPSpanAdapter(doc).ingest("t")) == {("c", "p")}


def test_ambiguous_graph_parent_without_span_parent_raises():
    # node 'leaf' executes twice (l1, l2), both AFTER child c, and c has no parentSpanId:
    # the logical parent is genuinely ambiguous and unrootable -> raise.
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "c", "name": "c", "startTimeUnixNano": "10",
         "attributes": [_attr("graph.node.parent_id", "leaf")]},
        {"traceId": "t", "spanId": "l1", "name": "l1", "startTimeUnixNano": "20", "attributes": [_attr("graph.node.id", "leaf")]},
        {"traceId": "t", "spanId": "l2", "name": "l2", "startTimeUnixNano": "30", "attributes": [_attr("graph.node.id", "leaf")]},
    ]}]}]}
    with pytest.raises(ValueError, match="cannot honor"):
        OTLPSpanAdapter(doc).ingest("t")


def test_link_to_later_span_still_recorded_as_cause():
    # An explicit link to a same-trace span stamped LATER is still a declared cause; dropping
    # it would erase real fan-in and the projection_lossy signal that depends on it.
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "r", "name": "r", "startTimeUnixNano": "10", "attributes": []},
        {"traceId": "t", "spanId": "x", "parentSpanId": "r", "name": "x", "startTimeUnixNano": "20",
         "attributes": [], "links": [{"traceId": "t", "spanId": "y"}]},
        {"traceId": "t", "spanId": "y", "parentSpanId": "r", "name": "y", "startTimeUnixNano": "30", "attributes": []},
    ]}]}]}
    nt = normalize(OTLPSpanAdapter(doc).ingest("t"))
    assert ("x", "y") in _caused(nt)                 # link kept despite y starting later
    assert nt.steps_by_id()["x"].projection_lossy    # x has two causes now (r and y) -> lossy


def test_clock_skew_keeps_declared_parent_not_fabricated_root():
    # child `b` is stamped EARLIER than its parent `a` (async span / coarse clock). The
    # declared parentSpanId edge must survive, and seq must still order cause before effect.
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "parent", "startTimeUnixNano": "100", "attributes": []},
        {"traceId": "t", "spanId": "b", "parentSpanId": "a", "name": "child",
         "startTimeUnixNano": "50", "attributes": []},
    ]}]}]}
    raw = OTLPSpanAdapter(doc).ingest("t")
    assert _caused(raw) == {("b", "a")}  # declared parent NOT dropped despite inverted clock
    seq = {s.step_id: s.seq for s in raw.steps}
    assert seq["a"] < seq["b"]
    normalize(raw)  # and validate_raw accepts it


def test_contradictory_causality_raises_cycle():
    # b is a's structural child (b caused_by a) AND a links back to the earlier-stamped b
    # (a caused_by b) -> a real causal cycle, which must raise rather than silently resolve.
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "a", "startTimeUnixNano": "100", "attributes": [],
         "links": [{"traceId": "t", "spanId": "b"}]},
        {"traceId": "t", "spanId": "b", "parentSpanId": "a", "name": "b",
         "startTimeUnixNano": "50", "attributes": []},
    ]}]}]}
    with pytest.raises(ValueError, match="cycle"):
        OTLPSpanAdapter(doc).ingest("t")


def test_cross_trace_and_missing_links_are_skipped():
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "root", "startTimeUnixNano": "1", "attributes": []},
        {"traceId": "t", "spanId": "b", "parentSpanId": "a", "name": "leaf",
         "startTimeUnixNano": "2", "attributes": [],
         "links": [{"traceId": "other", "spanId": "z"}, {"traceId": "t", "spanId": "ghost"}]},
    ]}]}]}
    # only the structural parent edge survives; the foreign and dangling links are dropped
    assert _caused(OTLPSpanAdapter(doc).ingest("t")) == {("b", "a")}


def test_links_disabled_yields_pure_tree():
    raw = OTLPSpanAdapter(sample_otlp_document(), links_as_causes=False).ingest("agent-trace-1")
    nt = normalize(raw)
    assert ("s4", "s2") not in _caused(raw)
    assert not any(s.projection_lossy for s in nt.steps)  # no fan-in -> nothing lossy


def test_snake_case_keys_are_accepted():
    doc = {"resource_spans": [{"scope_spans": [{"spans": [
        {"trace_id": "t", "span_id": "a", "name": "root", "start_time_unix_nano": "1", "attributes": []},
        {"trace_id": "t", "span_id": "b", "parent_span_id": "a", "name": "child",
         "start_time_unix_nano": "2", "attributes": []},
    ]}]}]}
    adapter = OTLPSpanAdapter(doc)
    assert adapter.discover() == ["t"]
    assert _caused(adapter.ingest("t")) == {("b", "a")}


def test_collector_jsonl_documents_are_merged():
    first = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "a", "name": "root", "attributes": []}
    ]}]}]}
    second = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t", "spanId": "b", "parentSpanId": "a", "name": "child", "attributes": []}
    ]}]}]}
    adapter = OTLPSpanAdapter.from_json(json.dumps(first) + "\n" + json.dumps(second) + "\n")
    assert adapter.discover() == ["t"]
    assert _caused(adapter.ingest("t")) == {("b", "a")}
