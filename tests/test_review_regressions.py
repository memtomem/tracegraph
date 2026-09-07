"""Reproductions for the September implementation review, F01–F09."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from typer.testing import CliRunner
from jsonschema import Draft202012Validator

from tracegraph import artifact
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
    from examples.tiny_agent import run
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
    {"events":[1]}, {"events":{}}, {"events":[{"name":"exception","attributes":[1]}]}, {"links":[1]}])
def test_malformed_otlp_is_value_error(fields):
    with pytest.raises(ValueError):
        OTLPSpanAdapter(otlp(**fields)).ingest("t")


@pytest.mark.parametrize("context", ["bad", [], 1, {"trace_id":[]}])
def test_malformed_phoenix_context(context):
    with pytest.raises(ValueError):
        PhoenixExportAdapter({"traceId":"t", "spans":[{"context":context}]})


def test_deep_json_all_boundaries(tmp_path):
    text = '[' * 20000 + ']' * 20000
    for parse in (artifact.loads, OTLPSpanAdapter.from_json, PhoenixExportAdapter.from_json):
        with pytest.raises(ValueError, match="nesting"):
            parse(text)
    source = tmp_path / "deep.json"
    source.write_text(text)
    assert CliRunner().invoke(app, ["analyze", str(source)]).exit_code == 2


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
    from scripts.verify_phoenix_syncmill_e2e import _verify_analysis, E2EFailure
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


@pytest.mark.parametrize("adapter", ["otlp", "phoenix", "langgraph"])
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
        graph.add_node(secret, lambda state: {"error": secret})
        graph.add_edge(START, secret)
        graph.add_edge(secret, END)
        graph.compile(checkpointer=saver).invoke({"error":""}, {"configurable":{"thread_id":"t"}})
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
