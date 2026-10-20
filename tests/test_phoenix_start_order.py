"""Loop parents ordered by Phoenix start times whose unit is unknown (#136).

A start time below the epoch-nanosecond floor is no time, but within one export it still orders
spans. It decides a loop parent only when the span and every candidate carry one, and the choice
is disclosed under ``otlp.graph_parent_by_timing`` like any other heuristic parent.
"""

import json
import random
from types import SimpleNamespace

import pytest
from test_capture_provenance import gap_counts
from test_cli import _patch_px
from typer.testing import CliRunner

from tracegraph.adapters import OTLPSpanAdapter, PhoenixExportAdapter
from tracegraph.adapters.phoenix_export import _start_order
from tracegraph.cli import app
from tracegraph.model import EdgeOrigin

runner = CliRunner()
ORDER_WARNING = "start-time numbers whose unit is unknown"


def _span(sid, start, attrs=None, parent=None):
    return {"id": sid, "context": {"trace_id": "tr", "span_id": sid}, "name": sid,
            "span_kind": "CHAIN", "parent_id": parent, "start_time": start, "end_time": None,
            "status_code": "OK", "attributes": attrs or {}}


def _loop(starts, child_start, *, child_parent=None):
    """Spans a, b, ... carrying ``graph.node.id`` loop, and c naming loop as its graph parent."""
    spans = [_span(chr(ord("a") + i), t, {"graph.node.id": "loop"}) for i, t in enumerate(starts)]
    spans.append(_span("c", child_start, {"graph.node.parent_id": "loop"}, child_parent))
    return {"traceId": "tr", "spans": spans}


def _ingest(export):
    return PhoenixExportAdapter(export).ingest("tr")


def _parent(raw, sid="c"):
    [(cause, origin)] = [(e.dst, e.origin) for e in raw.causal_edges if e.src == sid]
    return cause, origin


def test_the_issue_reproduction_is_accepted_with_the_parent_numbers_order():
    # 0.6.1 read these as nanoseconds and took b; 0.7.0 refused the export.
    raw = _ingest(_loop([1783987200, 1783987201], 1783987202))
    assert _parent(raw) == ("b", EdgeOrigin.GRAPH_PARENT)
    assert gap_counts(raw) == {"otlp.graph_parent_by_timing": 1, "otlp.unreadable_timestamp": 3}
    assert sum(ORDER_WARNING in w for w in raw.ingest_warnings) == 1
    assert all(s.ts is None for s in raw.steps), "the numbers are still no time"


def test_the_order_wins_over_a_parent_id_fallback():
    raw = _ingest(_loop([1783987200, 1783987201], 1783987202, child_parent="a"))
    assert _parent(raw) == ("b", EdgeOrigin.GRAPH_PARENT)
    assert "otlp.graph_parent_fallback" not in gap_counts(raw)


def test_a_fallback_that_would_close_a_cycle_is_no_longer_needed():
    # c's parent_id is p, and p names c's node as its own graph parent. 0.7.0 fell back to p
    # and refused the cycle; with the order, c takes b, as 0.6.1 did.
    export = _loop([1783987200, 1783987201], 1783987203, child_parent="p")
    export["spans"][-1]["attributes"]["graph.node.id"] = "cn"
    export["spans"].append(_span("p", 1783987202, {"graph.node.id": "pn", "graph.node.parent_id": "cn"}))
    raw = _ingest(export)
    assert _parent(raw) == ("b", EdgeOrigin.GRAPH_PARENT)
    assert _parent(raw, "p") == ("c", EdgeOrigin.GRAPH_PARENT)


def test_a_changed_parent_can_change_the_step_order(monkeypatch):
    # b sits two levels down (x -> y -> b) and c one (z -> c), so prerank puts c before b. Falling
    # back to z leaves c ahead of b in seq; taking b by the order puts c after it.
    export = _loop([1783987200, 1783987201], 1783987202, child_parent="z")
    export["spans"][1]["parent_id"] = "y"
    export["spans"] += [_span("x", None), _span("y", None, parent="x"), _span("z", None)]

    def seq_of():
        raw = _ingest(export)
        return _parent(raw)[0], {s.step_id: s.seq for s in raw.steps}

    parent, seq = seq_of()
    assert parent == "b" and seq["c"] > seq["b"]
    monkeypatch.setattr("tracegraph.adapters.phoenix_export._start_order", lambda value: None)
    parent, seq = seq_of()
    assert parent == "z" and seq["c"] < seq["b"]


@pytest.mark.parametrize("starts, child, expected", [
    ([1783987200.25, 1783987200.75], 1783987200.8, "b"),  # a float keeps its fraction
    ([1783987200.75, 1783987200.25], 1783987200.5, "b"),  # b is the nearer preceding one
    (["1783987200", "1783987201"], "1783987202", "b"),  # digit strings
    (["+1783987200", " 1783987201 "], "1_783_987_202", "b"),  # what int() reads
    ([0, 5], 3, "a"),  # relative offsets, zero included
    ([-10, -5], 0, "b"),  # negative offsets
    ([1783987200000, 1783987201000], 1783987202000, "b"),  # milliseconds
])
def test_unit_unknown_numbers_order_the_candidates(starts, child, expected):
    raw = _ingest(_loop(starts, child))
    assert _parent(raw) == (expected, EdgeOrigin.GRAPH_PARENT)


def test_equal_numbers_tie_by_depth_then_span_id_as_prerank_does():
    raw = _ingest(_loop([1783987200, 1783987200], 1783987201))
    assert _parent(raw) == ("b", EdgeOrigin.GRAPH_PARENT)  # same depth: the later id
    # a is one level deeper than b, so depth decides before the id does.
    export = _loop([1783987200, 1783987200], 1783987201)
    export["spans"][0]["parent_id"] = "r"
    export["spans"].append(_span("r", None))
    assert _parent(_ingest(export)) == ("a", EdgeOrigin.GRAPH_PARENT)


def test_the_export_order_does_not_matter():
    export = _loop([1783987200, 1783987201], 1783987202)
    export["spans"].append(_span("d", 1783987203, {"graph.node.id": "loop"}))  # a later run
    expected = _parent(_ingest(export))
    for seed in range(5):
        shuffled = dict(export, spans=list(export["spans"]))
        random.Random(seed).shuffle(shuffled["spans"])
        assert _parent(_ingest(shuffled)) == expected == ("b", EdgeOrigin.GRAPH_PARENT)


@pytest.mark.parametrize("starts, child", [
    (["2026-07-14T00:00:00Z", 1783987201], 1783987202),  # a candidate with a readable time
    ([1783987200, 1783987201], "2026-07-14T00:00:02Z"),  # the span itself has one
    ([1783987200, None], 1783987202),  # a candidate with no start
    ([1783987200, True], 1783987202),  # a bool is not a number here
    ([1783987200, "20260714_123456"], 1783987202),  # compact ISO is a time, not a number
    ([1783987203, 1783987204], 1783987202),  # nothing precedes
])
def test_without_a_number_on_every_one_the_choice_is_unchanged(starts, child):
    # Each of these has no order to go by, so with no parent_id the export is refused as in 0.7.0.
    with pytest.raises(ValueError, match="multiple ambiguous spans"):
        _ingest(_loop(starts, child))


def test_timed_and_numbered_choices_are_counted_once_each():
    timed = _loop(["2026-07-14T00:00:00Z", "2026-07-14T00:00:01Z"], "2026-07-14T00:00:02Z")
    numbered = _loop([1783987200, 1783987201], 1783987202)
    for span in numbered["spans"]:
        span["id"] = span["context"]["span_id"] = "n" + span["id"]
        if "graph.node.id" in span["attributes"]:
            span["attributes"]["graph.node.id"] = "nloop"
        else:
            span["attributes"]["graph.node.parent_id"] = "nloop"
    raw = _ingest({"traceId": "tr", "spans": timed["spans"] + numbered["spans"]})
    assert _parent(raw) == ("b", EdgeOrigin.GRAPH_PARENT)
    assert _parent(raw, "nc") == ("nb", EdgeOrigin.GRAPH_PARENT)
    assert gap_counts(raw)["otlp.graph_parent_by_timing"] == 2
    assert sum(ORDER_WARNING in w for w in raw.ingest_warnings) == 1
    assert sum("by start time was taken" in w for w in raw.ingest_warnings) == 1


def test_raw_otlp_never_reads_the_channel():
    spans = [
        {"traceId": "t", "spanId": sid, "name": sid, "_tracegraph_start_order": n,
         "attributes": [{"key": key, "value": {"stringValue": "loop"}}]}
        for sid, n, key in (("a", 1, "graph.node.id"), ("b", 2, "graph.node.id"),
                            ("c", 3, "graph.node.parent_id"))
    ]
    with pytest.raises(ValueError, match="multiple ambiguous spans"):
        OTLPSpanAdapter({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}).ingest("t")


@pytest.mark.parametrize("value, expected", [
    (1783987200, 1783987200), (1783987200.5, 1783987200.5), ("1783987200", 1783987200),
    ("-5", -5), (10**17, None), (str(10**17), None), (float("nan"), None), (float("inf"), None),
    (True, None), (b"1783987200", None), (None, None), ("2026-07-14T00:00:00Z", None),
    ("20260714_123456", None), ("not a time", None),
])
def test_start_order_takes_only_unit_unknown_numbers(value, expected):
    assert _start_order(value) == expected


def test_the_cli_accepts_the_reproduction(tmp_path, monkeypatch):
    export = _loop([1783987200, 1783987201], 1783987202)
    source = tmp_path / "export.json"
    source.write_text(json.dumps(export))
    for args in (["ingest-phoenix", "--file", str(source), "--out", str(tmp_path / "a.json")],
                 ["analyze", str(source)]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output

    def fake_run(command, **kwargs):
        if command == ["px", "--version"]:
            return SimpleNamespace(stdout="1.8.1\n")
        if command[1:3] == ["trace", "get"]:
            return SimpleNamespace(stdout=json.dumps(export))
        raise AssertionError(f"unexpected px command: {command}")

    _patch_px(monkeypatch, fake_run)
    result = runner.invoke(app, ["phoenix", "diagnose", "tr"])
    assert result.exit_code == 0, result.output
