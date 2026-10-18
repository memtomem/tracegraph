"""Telemetry a producer sent but tracegraph cannot read is disclosed, never silently dropped.

A dropped cost used to make the run's total look smaller than it was, and a fractional token
count was truncated to a plausible integer before validation could reject it.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from test_capture_provenance import gap_counts
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph import cli as cli_mod
from tracegraph.adapters import OTLPSpanAdapter, PhoenixExportAdapter
from tracegraph.analysis.diagnose import analyze
from tracegraph.cli import app
from tracegraph.normalize import normalize

runner = CliRunner()

GOOD = {"llm.cost.total": {"stringValue": "0.5"}, "llm.token_count.prompt": {"intValue": "10"}}


def _span(sid, parent, attrs):
    return {
        "traceId": "t",
        "spanId": sid,
        "parentSpanId": parent,
        "name": sid,
        "startTimeUnixNano": "1000",
        "endTimeUnixNano": "2000",
        "status": {"code": 1},
        "attributes": [{"key": k, "value": v} for k, v in attrs.items()],
    }


def _doc(bad: dict, good: dict | None = None) -> dict:
    kind = {"openinference.span.kind": {"stringValue": "LLM"}}
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            _span("a", None, {**kind, **(GOOD if good is None else good)}),
                            _span("b", "a", {**kind, **bad}),
                        ]
                    }
                ]
            }
        ]
    }


def _read(bad: dict, good: dict | None = None):
    raw = OTLPSpanAdapter(_doc(bad, good)).ingest("t")
    return raw, analyze(normalize(raw))


@pytest.mark.parametrize(
    "value",
    [{"stringValue": "abc"}, {"doubleValue": -1}, {"stringValue": "NaN"}, {"boolValue": True},
     {"arrayValue": {"values": []}}],
)
def test_unreadable_total_cost_withholds_the_total(value):
    raw, report = _read({"llm.cost.total": value})
    assert raw.steps[1].evidence.total_cost == "unreadable"
    assert report.metrics.total_cost is None
    assert any("not a finite non-negative decimal" in w for w in report.warnings), report.warnings
    assert any("cost" in w for w in raw.ingest_warnings), raw.ingest_warnings
    assert "abc" not in json.dumps(raw.ingest_warnings)
    assert gap_counts(raw) == {"otlp.unreadable_total_cost": 1}


@pytest.mark.parametrize(
    "value",
    [{"boolValue": True}, {"intValue": "-5"}, {"intValue": 1.5}, {"intValue": -0.5},
     {"stringValue": "abc"}, {"doubleValue": 2.5}],
)
def test_unreadable_token_count_is_unavailable_and_disclosed(value):
    raw, report = _read({"llm.token_count.prompt": value})
    assert raw.steps[1].evidence is None or raw.steps[1].evidence.prompt_tokens is None
    assert any("token count" in w for w in raw.ingest_warnings), raw.ingest_warnings
    assert gap_counts(raw) == {"otlp.unreadable_token_count": 1}
    # Token totals are already disclosed as possibly partial; the readable count still counts.
    assert report.metrics.prompt_tokens == 10


def test_disclosure_names_the_attribute_and_counts_spans():
    doc = _doc({"llm.token_count.completion": {"stringValue": "x"},
                "llm.token_count.total": {"boolValue": True}},
               good={**GOOD, "llm.token_count.completion": {"intValue": "-1"}})
    raw = OTLPSpanAdapter(doc).ingest("t")
    assert gap_counts(raw) == {"otlp.unreadable_token_count": 2}
    warnings = raw.ingest_warnings
    assert warnings == [
        "2 span(s) carry a token count that cannot be read (llm.token_count.completion, "
        "llm.token_count.total); that count is reported as unavailable."
    ]


def test_a_readable_fallback_name_is_not_a_drop():
    raw, _ = _read({"llm.token_count.prompt": {"stringValue": "abc"},
                    "llm.token_count.input": {"intValue": "7"}})
    assert raw.steps[1].evidence.prompt_tokens == 7
    assert raw.ingest_warnings == []


@pytest.mark.parametrize("bad", [{}, {"llm.cost.total": None}, {"llm.token_count.prompt": None}])
def test_absent_or_null_telemetry_is_not_a_drop(bad):
    raw, report = _read(bad)
    assert raw.ingest_warnings == []
    assert report.metrics.total_cost == "0.5"


@pytest.mark.parametrize("name", ["llm.cost.prompt", "llm.cost.completion"])
def test_unreadable_component_cost_is_disclosed(name):
    raw, report = _read({name: {"stringValue": "abc"}})
    assert raw.steps[1].evidence is None or getattr(
        raw.steps[1].evidence, name.replace("llm.cost.", "") + "_cost"
    ) is None
    assert any("cost" in w for w in raw.ingest_warnings), raw.ingest_warnings
    assert gap_counts(raw) == {"otlp.unreadable_component_cost": 1}
    # Component costs are never summed, so the total is unaffected.
    assert report.metrics.total_cost == "0.5"


@pytest.mark.parametrize("value", [{"intValue": "840"}, {"stringValue": "US$"}, {"boolValue": True}])
def test_unreadable_currency_is_disclosed(value):
    raw, _ = _read({"llm.cost.total": {"stringValue": "0.1"}, "llm.cost.currency": value})
    assert raw.steps[1].evidence.cost_currency is None
    assert any("currency" in w for w in raw.ingest_warnings), raw.ingest_warnings
    assert gap_counts(raw) == {"otlp.unreadable_cost_currency": 1}


def test_unwrapping_no_longer_truncates_a_fractional_int_value():
    raw, _ = _read({"llm.cost.total": {"intValue": 1.5}})
    assert raw.steps[1].evidence.total_cost == "1.5"


@pytest.mark.parametrize("value,expected", [({"intValue": 2.0}, 2), ({"intValue": "7"}, 7),
                                            ({"intValue": 7}, 7)])
def test_integral_int_values_still_read(value, expected):
    raw, _ = _read({"llm.token_count.prompt": value})
    assert raw.steps[1].evidence.prompt_tokens == expected
    assert raw.ingest_warnings == []


def _phoenix_tokens(attributes) -> dict:
    return {"traceId": "t", "spans": [{"context": {"trace_id": "t", "span_id": "s"},
                                       "span_kind": "LLM", "attributes": attributes}]}


@pytest.mark.parametrize(
    "attributes,expected,warned",
    [
        ({"llm.token_count.prompt": 1.5}, None, True),
        ({"llm.token_count.prompt": -0.5}, None, True),
        ({"llm.token_count.prompt": 2.0}, 2, False),
        ({"llm.token_count.prompt": "7"}, 7, False),
        ([{"key": "llm.token_count.prompt", "value": {"intValue": 1.5}}], None, True),
        ([{"key": "llm.token_count.prompt", "value": {"intValue": -0.5}}], None, True),
        ([{"key": "llm.token_count.prompt", "value": {"intValue": 2.0}}], 2, False),
        ([{"key": "llm.token_count.prompt", "value": {"intValue": "7"}}], 7, False),
    ],
)
def test_phoenix_token_counts_are_validated_as_sent(attributes, expected, warned):
    raw = PhoenixExportAdapter(_phoenix_tokens(attributes)).ingest("t")
    evidence = raw.steps[0].evidence
    assert (evidence.prompt_tokens if evidence else None) == expected
    assert any("token count" in w for w in raw.ingest_warnings) is warned, raw.ingest_warnings


def test_fractional_toolgraph_generation_is_rejected_not_truncated():
    attrs = {
        "toolgraph.preflight.artifact_digest": {"stringValue": "sha256:" + "a" * 64},
        "toolgraph.graph_generation": {"intValue": 1.5},
        "toolgraph.preflight.verdict": {"stringValue": "allow"},
    }
    with pytest.raises(ValueError, match="graph generation"):
        OTLPSpanAdapter(_doc(attrs)).ingest("t")


# --- the marker downstream ------------------------------------------------------------------


def _marked(tool_error: bool = False):
    bad = {"llm.cost.total": {"stringValue": "abc"}}
    if tool_error:
        bad["openinference.span.kind"] = {"stringValue": "TOOL"}
        bad["syncmill.run_id"] = {"stringValue": "run-1"}
    doc = _doc(bad)
    span = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
    if tool_error:
        span["name"] = "srv::lookup"
        span["status"] = {"code": 2, "message": "boom"}
    nt = normalize(OTLPSpanAdapter(doc).ingest("t"))
    assert nt.steps_by_id()["b"].evidence.total_cost == "unreadable"
    return nt


def _withheld(report) -> bool:
    return report.metrics.total_cost is None and any(
        "total_cost unavailable" in w and "not a finite non-negative decimal" in w
        for w in report.warnings
    )


def test_marker_survives_the_artifact_and_validates(tmp_path):
    nt = _marked()
    path = tmp_path / "a.json"
    artifact.save(nt, path)
    loaded = artifact.load(path)
    assert loaded.steps_by_id()["b"].evidence.total_cost == "unreadable"
    assert runner.invoke(app, ["validate", str(path)]).exit_code == 0
    assert "abc" not in path.read_text(encoding="utf-8")
    assert _withheld(analyze(loaded))


def test_marker_withholds_the_total_on_either_side_of_a_comparison():
    clean = normalize(OTLPSpanAdapter(_doc({"llm.cost.total": {"stringValue": "0.25"}})).ingest("t"))
    assert analyze(clean).metrics.total_cost == "0.75"
    for current, baseline in [(_marked(), clean), (clean, _marked()), (_marked(), _marked())]:
        report = analyze(current, baseline=baseline)
        assert report.comparison.metric_deltas["total_cost"] is None
        if current is not clean:
            assert _withheld(report)
        else:
            assert report.metrics.total_cost == "0.75"
            assert any("total_cost" in w and "not a finite non-negative decimal" in w
                       for w in report.warnings), report.warnings


def test_marker_survives_the_ladybug_store():
    pytest.importorskip("ladybug")
    from tracegraph.store import LadybugStore

    nt = _marked()
    back = LadybugStore.from_trace(nt).trace()
    assert back == nt
    assert back.steps_by_id()["b"].evidence.total_cost == "unreadable"


def test_marker_artifact_exports_a_memo_and_candidates(tmp_path):
    nt = _marked(tool_error=True)
    path = tmp_path / "a.json"
    artifact.save(nt, path)
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    outputs = []
    for attempt in range(2):
        memo, cands = tmp_path / f"m{attempt}.md", tmp_path / f"c{attempt}.json"
        assert runner.invoke(app, ["export-incident-memo", str(path), "-o", str(memo)]).exit_code == 0
        result = runner.invoke(app, ["export-review-candidates", "tool-failure", str(path), "-o", str(cands)])
        assert result.exit_code == 0, result.output
        outputs.append((memo.read_bytes(), cands.read_bytes()))
    assert outputs[0] == outputs[1]  # deterministic

    memo_text, cands_text = (b.decode("utf-8") for b in outputs[0])
    assert digest in memo_text
    assert "unreadable" not in memo_text and "abc" not in memo_text
    candidates = json.loads(cands_text)["candidates"]
    assert [(c["tool_key"], c["artifact_digest"]) for c in candidates] == [("srv::lookup", digest)]
    assert "abc" not in cands_text


# --- Phoenix scores -------------------------------------------------------------------------


def _phoenix(annotations):
    return {"traceId": "t", "spans": [{"context": {"trace_id": "t", "span_id": "s"},
                                       "annotations": annotations}]}


@pytest.mark.parametrize("score", ["abc", 10**400, "nan", float("inf"), [1], {}])
def test_phoenix_unreadable_score_is_null_and_disclosed(score):
    raw = PhoenixExportAdapter(_phoenix([{"name": "quality", "score": score}])).ingest("t")
    assert [e.score for e in raw.steps[0].evidence.evaluations] == [None]
    assert any("score" in w for w in raw.ingest_warnings), raw.ingest_warnings
    assert gap_counts(raw)["phoenix.unreadable_evaluation_score"] == 1


@pytest.mark.parametrize("score,expected", [(True, 1.0), (False, 0.0), ("0.25", 0.25)])
def test_phoenix_scores_that_read_as_numbers_are_unchanged(score, expected):
    raw = PhoenixExportAdapter(_phoenix([{"name": "quality", "score": score}])).ingest("t")
    assert [e.score for e in raw.steps[0].evidence.evaluations] == [expected]
    assert raw.ingest_warnings == []


def test_phoenix_counts_evaluations_not_spans():
    raw = PhoenixExportAdapter(_phoenix([{"name": "q1", "score": "abc"},
                                         {"name": "q2", "score": "nan"}])).ingest("t")
    assert any(w.startswith("2 evaluation score(s)") for w in raw.ingest_warnings), raw.ingest_warnings
    assert gap_counts(raw)["phoenix.unreadable_evaluation_score"] == 2


def test_phoenix_readable_or_dropped_evaluations_raise_no_score_warning():
    raw = PhoenixExportAdapter(_phoenix([
        {"name": "quality", "score": 0.5},
        {"name": "quality2", "result": {"score": None}},
        {"name": "free text name", "score": "abc"},  # dropped by the identifier filter
    ])).ingest("t")
    assert not any("score" in w for w in raw.ingest_warnings), raw.ingest_warnings


# --- the CLI prints the disclosures on stderr ----------------------------------------------

SECRET = "SECRET-COST-VALUE"


def _secret_otlp(tmp_path):
    path = tmp_path / "otlp.json"
    path.write_text(json.dumps(_doc({"llm.cost.total": {"stringValue": SECRET}})), encoding="utf-8")
    return path


def _secret_phoenix() -> dict:
    return {
        "traceId": "phx",
        "status": "OK",
        "spans": [{
            "context": {"trace_id": "phx", "span_id": "s"},
            "name": "llm",
            "span_kind": "LLM",
            "status_code": "OK",
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:00:01Z",
            "attributes": {"llm.cost.total": SECRET},
        }],
    }


def _assert_disclosed(result, *paths):
    assert result.exit_code == 0, result.output
    assert "warning:" in result.stderr and "total cost that cannot be read" in result.stderr
    assert "cannot be read" not in result.stdout and "warning:" not in result.stdout
    assert SECRET not in result.stdout and SECRET not in result.stderr
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert SECRET not in text and "cannot be read" not in text


def test_cli_ingest_otlp_discloses_on_stderr(tmp_path):
    out = tmp_path / "a.json"
    result = runner.invoke(app, ["ingest-otlp", "-f", str(_secret_otlp(tmp_path)), "-o", str(out)])
    _assert_disclosed(result, out)


@pytest.mark.parametrize("stdin", [False, True])
def test_cli_ingest_phoenix_discloses_on_stderr(tmp_path, stdin):
    source = tmp_path / "phx.json"
    source.write_text(json.dumps(_secret_phoenix()), encoding="utf-8")
    out = tmp_path / "a.json"
    if stdin:
        result = runner.invoke(app, ["ingest-phoenix", "-f", "-", "-o", str(out)],
                               input=source.read_text(encoding="utf-8"))
    else:
        result = runner.invoke(app, ["ingest-phoenix", "-f", str(source), "-o", str(out)])
    _assert_disclosed(result, out)


@pytest.mark.parametrize("as_baseline", [False, True])
def test_cli_analyze_phoenix_export_discloses_on_stderr(tmp_path, as_baseline):
    source = tmp_path / "phx.json"
    source.write_text(json.dumps(_secret_phoenix()), encoding="utf-8")
    report = tmp_path / "r.json"
    if as_baseline:
        clean = tmp_path / "clean.json"
        payload = _secret_phoenix()
        payload["spans"][0]["attributes"] = {"llm.cost.total": "0.1"}
        clean.write_text(json.dumps(payload), encoding="utf-8")
        args = ["analyze", str(clean), "--baseline", str(source), "--json-out", str(report)]
    else:
        args = ["analyze", str(source), "--json-out", str(report)]
    result = runner.invoke(app, args)
    _assert_disclosed(result, report)


@pytest.mark.parametrize("explicit", [True, False])
def test_cli_phoenix_diagnose_discloses_on_stderr(tmp_path, monkeypatch, explicit):
    payload = _secret_phoenix()

    def fake_run(command, **kwargs):
        if command == ["px", "--version"]:
            return SimpleNamespace(stdout="1.18.8\n")
        if command[1:3] == ["trace", "get"]:
            return SimpleNamespace(stdout=json.dumps(payload))
        if command[1:3] == ["trace", "list"]:
            return SimpleNamespace(stdout=json.dumps([payload]))
        raise AssertionError(f"unexpected px command: {command}")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    saved = tmp_path / "a.json"
    args = ["phoenix", "diagnose", *(["phx"] if explicit else []), "--save-artifact", str(saved)]
    result = runner.invoke(app, args)
    _assert_disclosed(result, saved)
