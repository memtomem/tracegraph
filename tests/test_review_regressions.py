"""Reproductions for the September implementation review, F01–F09."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from jsonschema import Draft202012Validator

from tracegraph import artifact, input_validation
from tracegraph.adapters import OTLPSpanAdapter, PhoenixExportAdapter
from tracegraph.analysis.ahu import canonical, diff, is_isomorphic
from tracegraph.analysis.diagnose import analyze, dumps, _logical_keys
from tracegraph.cli import app
from tracegraph.model import Edge, EdgeOrigin, EdgeType, RawTrace, Step, StepStatus, Trace, StepEvidence, EvaluationSummary
from tracegraph.normalize import normalize
from tracegraph.sqlite_snapshot import ingest_snapshot
from tracegraph.store.in_memory import InMemoryStore


def trace(names=("parent", "child"), origin=EdgeOrigin.SPAN_PARENT_FALLBACK, errors=True):
    return normalize(RawTrace(
        trace=Trace(trace_id="t", source_kind="test"),
        steps=[Step(step_id=str(i), trace_id="t", seq=i, name=name,
                    status=StepStatus.ERROR if errors else StepStatus.OK)
               for i, name in enumerate(names)],
        causal_edges=[Edge(type=EdgeType.CAUSED_BY, src=str(i), dst=str(i-1), origin=origin)
                      for i in range(1, len(names))],
    ))


def otlp(**fields):
    span = {"traceId": "t", "spanId": "s", "name": "tool", **fields}
    return {"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}


@pytest.mark.parametrize("identifier", ["../victim", "/tmp/victim", "a\\b", "CON", "nul.txt", "x"*500, "한글", ".", "a."])
def test_default_filename_cannot_escape(identifier):
    path = artifact.default_path(identifier)
    assert path.parent == Path('.')
    assert path.name == "trace-" + hashlib.sha256(identifier.encode()).hexdigest() + ".json"


def test_ingest_default_create_only_and_explicit_replace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source.json"
    source.write_text(json.dumps(otlp(traceId="../victim")))
    victim = tmp_path.parent / "victim.json"
    victim.write_text("sentinel")
    runner = CliRunner()
    args = ["ingest-otlp", "--file", str(source)]
    assert runner.invoke(app, args).exit_code == 0
    target = artifact.default_path("../victim")
    original = target.read_bytes()
    assert runner.invoke(app, args).exit_code == 2
    assert target.read_bytes() == original
    assert victim.read_text() == "sentinel"
    assert runner.invoke(app, args + ["--out", str(target)]).exit_code == 0
    assert runner.invoke(app, args + ["--out", str(source)]).exit_code == 2


def test_atomic_create_race_and_symlink(tmp_path):
    nt = trace()
    target = tmp_path / "out.json"
    def save(_):
        try:
            artifact.save_atomic(nt, target, replace=False)
            return True
        except FileExistsError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(save, range(4))) == 1
    assert artifact.load(target) == nt
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        artifact.save_atomic(nt, link, replace=False)
    assert not list(tmp_path.glob(".*.json.*"))


def test_report_outputs_cannot_alias_sources_or_each_other(tmp_path):
    source = tmp_path / "source.json"
    artifact.save(trace(), source)
    original = source.read_bytes()
    alias = tmp_path / "alias.json"
    alias.hardlink_to(source)
    runner = CliRunner()
    for args in (["analyze", str(source), "--json-out", str(alias)],
                 ["analyze", str(source), "--baseline", str(alias), "--json-out", str(alias)],
                 ["phoenix", "diagnose", "--save-artifact", str(source), "--json-out", str(alias)]):
        assert runner.invoke(app, args).exit_code == 2
        assert source.read_bytes() == original


@pytest.mark.parametrize("wal", [False, True])
def test_sqlite_failed_ingestion_preserves_source(tmp_path, wal):
    path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=" + ("WAL" if wal else "DELETE"))
    conn.execute("CREATE TABLE unrelated(value)")
    conn.execute("INSERT INTO unrelated VALUES (42)")
    conn.commit()
    before = path.read_bytes()
    with pytest.raises((KeyError, ValueError)):
        ingest_snapshot(path, "missing", error_channel="error")
    assert path.read_bytes() == before
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("unrelated",)]
    assert conn.execute("SELECT value FROM unrelated").fetchall() == [(42,)]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == ("wal" if wal else "delete")
    with pytest.raises(ValueError, match="deadline"):
        ingest_snapshot(path, "missing", error_channel="error", timeout=0)
    assert path.read_bytes() == before
    conn.close()


def test_readonly_live_wal_checkpoint_snapshot(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver
    from tiny_agent import run
    path = tmp_path / "db.sqlite"
    with SqliteSaver.from_conn_string(str(path)) as saver:
        run(saver, "thread", "hello")
        expected = saver.conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
        before = path.read_bytes()
        path.chmod(0o444)
        try:
            raw = ingest_snapshot(path, "thread", error_channel="error")
            assert len(raw.steps) == expected
            assert path.read_bytes() == before
        finally:
            path.chmod(0o644)


@pytest.mark.parametrize("fields", [{"attributes":[1]}, {"attributes":[None]}, {"attributes":0},
    {"attributes":[{"key":[]}]}, {"status":"ERROR"}, {"status":{"code":[]}},
    {"events":[1]}, {"events":{}}, {"events":[{"name":"exception","attributes":[1]}]}, {"links":[1]},
    {"spanId":["x"]}, {"spanId":""}, {"traceId":["t"]}, {"traceId":""}, {"traceId":5},
    {"parentSpanId":["x"]}, {"parentSpanId":5}, {"parentSpanId":0}, {"parentSpanId":[]},
    {"parentSpanId":False}, {"status":{"code":True}}, {"status":{"code":False}}])
def test_malformed_otlp_is_value_error(fields):
    with pytest.raises(ValueError):
        OTLPSpanAdapter(otlp(**fields)).ingest("t")


@pytest.mark.parametrize("context", ["bad", [], 1, {"trace_id":[]}])
def test_malformed_phoenix_context(context):
    with pytest.raises(ValueError):
        PhoenixExportAdapter({"traceId":"t", "spans":[{"context":context}]})


ROOT = Path(__file__).parents[1]


def test_deep_json_all_boundaries(tmp_path):
    text = '[' * 20000 + ']' * 20000
    for parse in (artifact.loads, OTLPSpanAdapter.from_json, PhoenixExportAdapter.from_json):
        with pytest.raises(ValueError, match="nesting"):
            parse(text)
    source = tmp_path / "deep.json"
    source.write_text(text)
    assert CliRunner().invoke(app, ["analyze", str(source)]).exit_code == 2


def test_deep_value_in_an_ignored_key_is_refused():
    """Refused on every interpreter, though 3.14's json would parse it and drop the key."""
    source = ROOT / "tests" / "fixtures" / "review-candidates" / "source.json"
    deep = '{"a":' * 20000 + "1" + "}" * 20000
    text = json.dumps(json.loads(source.read_text(encoding="utf-8")))[:-1] + ', "ignored": ' + deep + "}"
    with pytest.raises(ValueError, match="JSON nesting too deep"):
        artifact.loads(text)


def test_recursion_in_json_is_reported_as_nesting(monkeypatch):
    def recurse(*args, **kwargs):
        raise RecursionError

    monkeypatch.setattr(input_validation, "json", SimpleNamespace(loads=recurse))
    for strict in (False, True):
        with pytest.raises(ValueError, match="^JSON nesting too deep$"):
            input_validation.loads("[]", strict=strict)


@pytest.mark.parametrize("code,expected", [(1,StepStatus.OK),(2,StepStatus.ERROR),(0,StepStatus.ERROR),(None,StepStatus.ERROR)])
def test_explicit_ok_beats_handled_exception(code, expected):
    span = otlp(status={"code":code}, events=[{"name":"exception", "attributes":{"exception.message":"handled"}}])
    raw = OTLPSpanAdapter(span).ingest("t")
    assert raw.steps[0].status is expected
    if expected is StepStatus.OK:
        assert raw.steps[0].error_msg is None


@pytest.mark.parametrize("origin", [EdgeOrigin.SPAN_PARENT_FALLBACK, EdgeOrigin.LEGACY_UNKNOWN, None])
def test_containment_does_not_hide_child_failure(origin):
    report = analyze(trace(origin=origin))
    assert [f.step.name for f in report.primary_failures] == ["child", "parent"]
    assert report.propagated_failures == []
    nt = trace(origin=origin)
    nt.steps[-1].status = StepStatus.OK
    assert [f.step.name for f in analyze(nt).primary_failures] == ["parent"]


@pytest.mark.parametrize("origin", [EdgeOrigin.GRAPH_PARENT, EdgeOrigin.SPAN_LINK, EdgeOrigin.CHECKPOINT_PARENT])
def test_explicit_ancestry_keeps_context(origin):
    report = analyze(trace(origin=origin))
    assert [f.step.name for f in report.primary_failures] == ["parent"]
    assert [s.name for s in report.propagated_failures] == ["child"]


def test_privacy_display_fields_and_raw_digest():
    secret = "PRIVATE BODY account=123"
    nt = trace((secret, "safe"))
    nt.trace.source_kind = secret
    nt.steps[0].evidence = StepEvidence(total_cost="1", cost_currency=secret, evaluations=[EvaluationSummary(name=secret, label=secret)])
    baseline = trace(("baseline PRIVATE BODY", "safe"), errors=False)
    original = artifact.dumps(nt)
    report = analyze(nt, baseline=baseline)
    encoded = dumps(report)
    assert secret not in encoded and "baseline PRIVATE BODY" not in encoded
    assert "redacted:" in encoded
    assert report.artifact_digest == "sha256:" + hashlib.sha256(original.encode()).hexdigest()
    assert artifact.dumps(nt) == original
    assert not report.comparison.topology_identical
    assert any("Display text" in warning for warning in report.warnings)


def test_logical_key_delimiters_and_missing_names_do_not_collide():
    nt = trace(("a", "b"), errors=False)
    nt.steps.append(Step(step_id="2", trace_id="t", seq=2, name="a#0/CHAIN:b", status=StepStatus.OK))
    nt = normalize(RawTrace(trace=nt.trace, steps=nt.steps, causal_edges=nt.edges_of(EdgeType.CAUSED_BY)))
    baseline = nt.model_copy(deep=True)
    nt.steps[-1].status = StepStatus.ERROR
    assert len(_logical_keys(nt)) == 3
    changes = analyze(nt, baseline=baseline).comparison.behavior_changes
    assert len(changes) == 1 and changes[0].name == "a#0/CHAIN:b"
    roots = normalize(RawTrace(trace=Trace(trace_id="t",source_kind="x"), steps=[
        Step(step_id=str(i),trace_id="t",seq=i,name=name) for i,name in enumerate((None,"","-","same","same"))]))
    assert len(_logical_keys(roots)) == 5


@pytest.mark.parametrize("n", [8000, 16000])
def test_independent_deep_trees(n):
    names = ["node"] * n
    a, b = trace(names, errors=False), trace(names, errors=False)
    assert is_isomorphic(a,b) and diff(a,b).identical
    b.steps[-1].name = "changed"
    result = diff(a,b)
    assert not result.identical and any("changed" in c for c in result.changes)
    # Keep the public nested-tuple format, building it without recursive comparisons.
    assert isinstance(canonical(a), tuple)
    assert not diff(a, trace(("unrelated",), errors=False)).identical


@pytest.mark.parametrize("backend", ["memory", "ladybug"])
def test_store_owns_inputs_and_outputs(backend):
    cls = InMemoryStore
    if backend == "ladybug":
        pytest.importorskip("ladybug")
        from tracegraph.store.ladybug import LadybugStore
        cls = LadybugStore
    nt = trace()
    nt.steps[0].evidence = StepEvidence(evaluations=[EvaluationSummary(name="safe")])
    store = cls.from_trace(nt)
    try:
        nt.steps[0].name = "changed input"
        returned = store.trace()
        returned.trace.source_kind = "changed output"
        returned.steps[0].evidence.evaluations[0].name = "changed nested"
        returned.edges[0].dst = "bad"
        ancestors = store.ancestors("1")
        ancestors[0].name = "changed ancestor"
        again = store.trace()
        assert again.steps[0].name == "parent"
        assert again.steps[0].evidence.evaluations[0].name == "safe"
        assert again.trace.source_kind == "test"
        assert all(e.dst != "bad" for e in again.edges)
    finally:
        if hasattr(store,"close"):
            store.close()


def test_comparison_schema_rejects_malformed_members():
    schema = json.loads(Path("contracts/analysis-report.schema.json").read_text())
    validator = Draft202012Validator(schema)
    obj = json.loads(dumps(analyze(trace(), baseline=trace(errors=False))))
    validator.validate(obj)
    obj["comparison"]["topology_identical"] = "yes"
    assert list(validator.iter_errors(obj))


def test_e2e_report_assertions_reject_wrong_trace_baseline_and_body(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_phoenix_syncmill_e2e.py"
    spec = importlib.util.spec_from_file_location("phoenix_e2e_verifier", script)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    _verify_analysis, E2EFailure = verifier._verify_analysis, verifier.E2EFailure
    from tracegraph.model import StepKind
    nt = trace(("tool", "retry:tool", "tool"))
    nt.steps[0].kind = nt.steps[2].kind = StepKind.TOOL
    baseline = nt.model_copy(deep=True)
    for step in baseline.steps:
        step.status = StepStatus.OK
    obj = json.loads(dumps(analyze(nt, baseline=baseline)))
    path = tmp_path / "report.json"
    path.write_text(json.dumps(obj))
    digest = obj["comparison"]["baseline_digest"]
    _verify_analysis(path, "t", baseline=True, baseline_digest=digest)
    with pytest.raises(E2EFailure):
        _verify_analysis(path, "wrong", baseline=True)
    with pytest.raises(E2EFailure):
        _verify_analysis(path, "t", baseline=True, baseline_digest="sha256:" + "0"*64)
    obj["primary_failures"][0]["step"]["name"] = "PRIVATE BODY"
    path.write_text(json.dumps(obj))
    with pytest.raises(E2EFailure):
        _verify_analysis(path, "t", baseline=True)


def test_redacted_alias_does_not_become_matching_identity():
    secret = "PRIVATE BODY"
    alias = "redacted:" + hashlib.sha256(secret.encode()).hexdigest()
    before = trace((secret,), errors=False)
    after = trace((alias,))
    comparison = analyze(after, baseline=before).comparison
    assert not comparison.topology_identical
    assert len(comparison.behavior_changes) == 2
    assert len({c.logical_step_key for c in comparison.behavior_changes}) == 2


@pytest.mark.parametrize("adapter", ["otlp", "phoenix", "langgraph", "langgraph-native"])
def test_report_privacy_after_real_adapter_ingestion(adapter):
    secret = "PRIVATE ACCOUNT BODY"
    if adapter == "otlp":
        raw = OTLPSpanAdapter(otlp(name=secret, status={"code":2,"message":secret})).ingest("t")
    elif adapter == "phoenix":
        raw = PhoenixExportAdapter({"traceId":"t", "spans":[{
            "context":{"trace_id":"t","span_id":"s"}, "name":secret,
            "status_code":"ERROR", "annotations":[{"name":secret,"result":{"label":secret}}]
        }]}).ingest("t")
    else:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import StateGraph, START, END
        from tracegraph.adapters import LangGraphCheckpointAdapter
        saver = InMemorySaver()
        from typing import TypedDict
        class State(TypedDict):
            error: str
        graph = StateGraph(State)
        if adapter == "langgraph-native":
            # A raised exception carries the secret in its *message*, which reaches the trace
            # through pending writes rather than through a state channel — a second way in,
            # so the redaction guarantee has to hold on that path too.
            def node(state):
                raise RuntimeError(secret)
        else:
            def node(state):
                return {"error": secret}
        graph.add_node(secret, node)
        graph.add_edge(START, secret)
        graph.add_edge(secret, END)
        app = graph.compile(checkpointer=saver)
        try:
            app.invoke({"error":""}, {"configurable":{"thread_id":"t"}})
        except RuntimeError:
            assert adapter == "langgraph-native"
        raw = LangGraphCheckpointAdapter(saver).ingest("t")
    nt = normalize(raw)
    report = analyze(nt)
    assert report.error_count > 0
    assert secret not in dumps(report)
    assert report.artifact_digest == "sha256:" + hashlib.sha256(artifact.dumps(nt).encode()).hexdigest()


@pytest.mark.parametrize("events", [False, 0, {}, "invalid"])
def test_phoenix_rejects_non_array_events(events):
    with pytest.raises(ValueError, match="events"):
        PhoenixExportAdapter({"traceId":"t", "spans":[{
            "context":{"trace_id":"t","span_id":"s"}, "events":events
        }]}).ingest("t")


# --- ingest boundary: numbers that cannot be converted, ids, private keys -----------------


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 1e30, 10**400,
                                   "9" * 400, "yesterday"])
def test_unconvertible_timestamp_is_unavailable_and_disclosed(value):
    # Same rule for every value that cannot become a timestamp: no traceback, no guessed
    # time, and a disclosure on the read. A non-finite or out-of-range number used to escape
    # as OverflowError; a garbage string was silently treated as missing.
    raw = OTLPSpanAdapter(otlp(startTimeUnixNano=value, endTimeUnixNano=value)).ingest("t")
    step = raw.steps[0]
    assert step.ts is None
    assert step.evidence is None or (step.evidence.end_ts is None and step.evidence.duration_ms is None)
    assert any("timestamp" in w for w in raw.ingest_warnings)


def test_convertible_timestamps_raise_no_warning():
    raw = OTLPSpanAdapter(otlp(startTimeUnixNano="1700000000000000000",
                               endTimeUnixNano=1700000000500000000)).ingest("t")
    assert raw.steps[0].ts is not None
    assert raw.ingest_warnings == []


def test_empty_parent_span_id_still_means_root():
    raw = OTLPSpanAdapter(otlp(parentSpanId="")).ingest("t")
    assert raw.causal_edges == []


@pytest.mark.parametrize("count", [{"doubleValue": float("inf")}, {"intValue": float("inf")},
                                   {"intValue": float("nan")}, True, {"boolValue": True},
                                   {"intValue": True}])
def test_unusable_token_count_is_unavailable(count):
    # The metric is unavailable, not 1, and the read discloses it (test_unreadable_telemetry).
    raw = OTLPSpanAdapter(otlp(attributes=[{"key": "llm.token_count.total", "value": count}])
                          if isinstance(count, dict) else otlp(attributes={"llm.token_count.total": count})
                          ).ingest("t")
    assert raw.steps[0].evidence is None or raw.steps[0].evidence.total_tokens is None


def test_raw_otlp_cannot_inject_evaluations_through_the_phoenix_channel():
    span = otlp()
    span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["_tracegraph_evaluations"] = [
        {"name": "quality", "label": "x" * 500}
    ]
    assert OTLPSpanAdapter(span).ingest("t").steps[0].evidence is None


def _phoenix_span(**fields):
    return {"traceId": "t", "spans": [{"context": {"trace_id": "t", "span_id": "s"}, **fields}]}


@pytest.mark.parametrize("fields", [{"parent_id": ["x"]}, {"parent_id": 5}, {"annotations": 5},
                                    {"annotations": {"name": "q"}}])
def test_malformed_phoenix_span(fields):
    with pytest.raises(ValueError):
        PhoenixExportAdapter(_phoenix_span(**fields)).ingest("t")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "yesterday"])
def test_phoenix_unconvertible_timestamp_is_disclosed(value):
    raw = PhoenixExportAdapter(_phoenix_span(start_time=value, end_time=value)).ingest("t")
    assert raw.steps[0].ts is None
    assert any("timestamp" in w for w in raw.ingest_warnings)


_PHOENIX_NS = 1_783_987_200_100_000_000  # 2026-07-14T00:00:00.1Z


def _phoenix_times(start, end):
    raw = PhoenixExportAdapter(_phoenix_span(start_time=start, end_time=end)).ingest("t")
    gaps = {gap.code: gap.count for gap in raw.trace.capture.gaps}
    evidence = raw.steps[0].evidence
    return raw.steps[0].ts, evidence.duration_ms if evidence else None, gaps.get("otlp.unreadable_timestamp")


@pytest.mark.parametrize("value", [
    _PHOENIX_NS / 1e9, _PHOENIX_NS // 10**6, _PHOENIX_NS // 10**3, 10**17 - 1, True, "20260714",
    "+1783987200100", " 1783987200100 ", "-1", "1_783_987_200_100",
])
def test_phoenix_number_too_small_for_epoch_nanoseconds_is_disclosed(value):
    # Seconds, milliseconds or microseconds read as nanoseconds would land near 1970 (#124).
    assert _phoenix_times(value, value) == (None, None, 1)


@pytest.mark.parametrize("start", [_PHOENIX_NS, str(_PHOENIX_NS), f"+{_PHOENIX_NS}", float(_PHOENIX_NS),
                                   "2026-07-14T00:00:00.1Z"])
def test_phoenix_epoch_nanoseconds_and_iso_are_still_read(start):
    ts, duration, gap = _phoenix_times(start, "2026-07-14T00:00:00.6Z")
    assert ts == "2026-07-14T00:00:00.100000+00:00" and duration == 500.0 and gap is None


@pytest.mark.parametrize(("value", "ts"), [(10**17, "1973-03-03T09:46:40+00:00"),
                                          ("20260714_123456", "2026-07-14T12:34:56+00:00"),
                                          ("20260714_1234", "2026-07-14T12:34:00+00:00")])
def test_phoenix_floor_and_iso_strings_that_int_also_reads(value, ts):
    # ISO comes before the integer fallback, so a compact ISO time `int()` accepts stays a time.
    assert _phoenix_times(value, None)[0] == ts


@pytest.mark.parametrize("value", ["²", "9" * 5000])
def test_phoenix_digit_text_int_cannot_read_is_still_disclosed_once(value):
    assert _phoenix_times(value, value) == (None, None, 1)


@pytest.mark.parametrize(("start", "end"), [(1783987200, "yesterday"), ("yesterday", 1783987200)])
def test_phoenix_span_with_two_bad_times_is_counted_once(start, end):
    assert _phoenix_times(start, end) == (None, None, 1)


class _ReprRaisesInt(int):
    def __repr__(self):
        raise RuntimeError("producer __repr__ ran")


class _ReprRaisesStr(str):
    def __repr__(self):
        raise ValueError("producer __repr__ ran")


class _StrAndIntDisagree:
    def __str__(self):
        return "-1"

    def __int__(self):
        return _PHOENIX_NS


@pytest.mark.parametrize("value", [b"1783987200100", bytearray(b"1783987200100"),
                                   _ReprRaisesInt(1), _ReprRaisesStr("+1783987200100")],
                         ids=["bytes", "bytearray", "int-repr-raises", "str-repr-raises"])
def test_phoenix_small_number_floor_follows_int_value_and_runs_no_repr(value):
    # The floor tests what the span builder's int(value) reads, and the unreadable marker is
    # fixed text, so a producer's __repr__ never runs.
    assert _phoenix_times(value, value) == (None, None, 1)


class _IntRaisesOSError:
    def __str__(self):
        return "unreadable"

    def __int__(self):
        raise OSError("producer __int__")


def test_phoenix_value_the_builder_cannot_read_stays_disclosed_whatever_int_raises():
    # The fallback asks the span builder's own parser, so it rejects exactly what the builder
    # rejects, including an __int__ that raises OSError.
    assert _phoenix_times(_IntRaisesOSError(), None) == (None, None, 1)


class _DigitTextWithInt(str):
    def __int__(self):
        return 1783987200100


def test_phoenix_digit_text_int_cannot_read_is_handed_on_as_text():
    # As before #124, the text goes on, so the builder never consults the producer's __int__.
    assert _phoenix_times(_DigitTextWithInt("²"), None) == (None, None, 1)


def test_phoenix_value_whose_str_is_not_its_int_is_read_by_its_int():
    ts, _, gap = _phoenix_times(_StrAndIntDisagree(), None)
    assert ts == "2026-07-14T00:00:00.100000+00:00" and gap is None


def test_cli_prints_read_disclosures_for_otlp(tmp_path):
    source = tmp_path / "inf.json"
    source.write_text(json.dumps(otlp(startTimeUnixNano=1)).replace('"startTimeUnixNano": 1', '"startTimeUnixNano": Infinity'))
    result = CliRunner().invoke(app, ["ingest-otlp", "--file", str(source), "--out", str(tmp_path / "o.json")])
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "timestamp" in result.stderr


def test_int64_string_token_count_is_still_read():
    raw = OTLPSpanAdapter(otlp(attributes=[{"key": "llm.token_count.total", "value": {"intValue": "42"}}])).ingest("t")
    assert raw.steps[0].evidence.total_tokens == 42


def test_phoenix_unrepresentable_score_is_unavailable():
    raw = PhoenixExportAdapter(_phoenix_span(annotations=[{"name": "q", "score": 10**400}])).ingest("t")
    assert [e.score for e in raw.steps[0].evidence.evaluations] == [None]
