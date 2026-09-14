"""Tests for memtomem incident memo exporter with strict output allowlist."""

import hashlib

import pytest
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph.analysis.memo import (
    INCIDENT_FIDELITY_WARNING,
    LANGGRAPH_FIDELITY_WARNING,
    LINKS_PRESERVED_FIDELITY_WARNING,
    PARENT_ONLY_FIDELITY_WARNING,
    build_incident_memo,
    compute_run_digest,
    save_incident_memo,
)
from tracegraph.cli import app
from tracegraph.model import (
    CausalFidelity,
    Edge,
    EdgeOrigin,
    EdgeType,
    EvaluationSummary,
    RawTrace,
    Step,
    StepEvidence,
    StepKind,
    StepSource,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize

runner = CliRunner()


def _sample_trace_with_secrets(
    *,
    secret_run_id: str = "sk-ant-api03-SECRET-RUN-TOKEN",
    secret_step_name: str = "ghp_SECRET_GITHUB_TOKEN",
    secret_error_msg: str = "DB connection failed password=SuperSecret123",
    secret_currency: str = "SECRET_CURRENCY",
    secret_source_kind: str = "SECRET_SOURCE_KIND",
) -> RawTrace:
    return RawTrace(
        trace=Trace(
            trace_id="trace-secret-1",
            run_id=secret_run_id,
            source_kind=secret_source_kind,
        ),
        steps=[
            Step(
                step_id="step-uuid-1",
                trace_id="trace-secret-1",
                seq=0,
                source=StepSource.INPUT,
                kind=StepKind.CHAIN,
                name="entrypoint",
            ),
            Step(
                step_id="step-uuid-2",
                trace_id="trace-secret-1",
                seq=1,
                kind=StepKind.TOOL,
                name=secret_step_name,
                status=StepStatus.ERROR,
                error_msg=secret_error_msg,
                evidence=StepEvidence(
                    duration_ms=120.0,
                    cost_currency=secret_currency,
                    evaluations=[EvaluationSummary(name="safety_eval", label="UNSAFE_LEAK", score=0.0)],
                ),
            ),
        ],
        causal_edges=[
            Edge(
                type=EdgeType.CAUSED_BY,
                src="step-uuid-2",
                dst="step-uuid-1",
                origin=EdgeOrigin.GRAPH_PARENT,
            ),
        ],
    )


def test_build_incident_memo_strict_allowlist_and_secrets_exclusion():
    raw = _sample_trace_with_secrets()
    nt = normalize(raw)
    artifact_bytes = artifact.dumps(nt).encode("utf-8")
    artifact_digest = f"sha256:{hashlib.sha256(artifact_bytes).hexdigest()}"

    memo = build_incident_memo(nt, artifact_digest)

    # Allowlisted fields must be present
    assert f"artifact_digest: {artifact_digest}" in memo
    expected_run_digest = compute_run_digest("sk-ant-api03-SECRET-RUN-TOKEN")
    assert expected_run_digest is not None
    assert f"run_digest: {expected_run_digest}" in memo
    assert INCIDENT_FIDELITY_WARNING in memo
    assert "type: incident" in memo
    assert "step_1_chain" in memo
    assert "step_2_tool" in memo
    assert "`step_2_tool` caused by `step_1_chain` [origin: graph_parent]" in memo

    # Strictly forbidden secrets must NEVER appear in the memo
    forbidden = [
        "sk-ant-api03-SECRET-RUN-TOKEN",
        "ghp_SECRET_GITHUB_TOKEN",
        "SuperSecret123",
        "password=",
        "SECRET_CURRENCY",
        "SECRET_SOURCE_KIND",
        "UNSAFE_LEAK",
        "step-uuid-1",
        "step-uuid-2",
        "trace-secret-1",
    ]
    for secret in forbidden:
        assert secret not in memo, f"Secret/identifier leaked in memo: {secret!r}"


def test_build_incident_memo_fan_in_preserves_actual_edge_pairs():
    # Fan-in: a, b -> c (c has two real parents). Must NOT assert a -> b!
    raw = RawTrace(
        trace=Trace(trace_id="fan-in-trace", source_kind="syncmill"),
        steps=[
            Step(step_id="a", trace_id="fan-in-trace", seq=0, kind=StepKind.CHAIN),
            Step(step_id="b", trace_id="fan-in-trace", seq=1, kind=StepKind.TOOL),
            Step(step_id="c", trace_id="fan-in-trace", seq=2, kind=StepKind.TOOL, status=StepStatus.ERROR),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="c", dst="a", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="c", dst="b", origin=EdgeOrigin.SPAN_LINK),
        ],
    )
    nt = normalize(raw)
    memo = build_incident_memo(nt, "sha256:" + "a" * 64)

    # Must list both real causes
    assert "`step_3_tool` caused by `step_1_chain` [origin: graph_parent]" in memo
    assert "`step_3_tool` caused by `step_2_tool` [origin: span_link]" in memo
    # Must NOT fabricate a linear arrow between parents
    assert "step_1_chain -> step_2_tool" not in memo
    assert "step_2_tool -> step_1_chain" not in memo


def test_build_incident_memo_parent_only_disclosure():
    raw = RawTrace(
        trace=Trace(
            trace_id="phoenix-trace",
            source_kind="phoenix",
            causal_fidelity=CausalFidelity.PARENT_ONLY,
            links_preserved=False,
        ),
        steps=[
            Step(step_id="err", trace_id="phoenix-trace", seq=0, kind=StepKind.TOOL, status=StepStatus.ERROR),
        ],
        causal_edges=[],
    )
    nt = normalize(raw)
    memo = build_incident_memo(nt, "sha256:" + "b" * 64)

    # Disclosures and metadata
    assert "causal_fidelity: parent_only" in memo
    assert "links_preserved: false" in memo
    assert PARENT_ONLY_FIDELITY_WARNING in memo
    # No predecessors must NOT assert "root cause"
    assert "(no recorded predecessors)" in memo
    assert "root cause" not in memo


def test_build_incident_memo_clean_trace_no_errors():
    raw = RawTrace(
        trace=Trace(trace_id="clean-trace", source_kind="langgraph"),
        steps=[
            Step(step_id="s1", trace_id="clean-trace", seq=0, kind=StepKind.CHAIN),
            Step(step_id="s2", trace_id="clean-trace", seq=1, kind=StepKind.TOOL),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="s2", dst="s1"),
        ],
    )
    nt = normalize(raw)
    digest = "sha256:" + "0" * 64
    memo = build_incident_memo(nt, digest)

    assert "**Trace Status**: `ok`" in memo
    assert "No step-level errors detected." in memo
    assert "run_digest" not in memo


def test_save_incident_memo_cleans_up_on_failure(tmp_path):
    target_dir = tmp_path / "existing_directory"
    target_dir.mkdir()

    # Replacing an existing directory with a file will raise an OSError (EISDIR on Unix)
    with pytest.raises(OSError):
        save_incident_memo("# test memo", target_dir)

    # Verify no temporary files were left behind in tmp_path
    remaining_temps = list(tmp_path.glob(".*"))
    assert not remaining_temps, f"Temporary file leaked on error: {remaining_temps}"


def test_cli_export_incident_memo_success(tmp_path):
    raw = _sample_trace_with_secrets()
    nt = normalize(raw)
    artifact_path = tmp_path / "trace_artifact.json"
    memo_path = tmp_path / "incident_memo.md"
    artifact.save(nt, artifact_path)

    result = runner.invoke(app, ["export-incident-memo", str(artifact_path), "-o", str(memo_path)])
    assert result.exit_code == 0, result.output
    assert "exported incident memo" in result.output
    assert memo_path.exists()

    memo_content = memo_path.read_text(encoding="utf-8")
    expected_digest = f"sha256:{hashlib.sha256(artifact_path.read_bytes()).hexdigest()}"
    assert expected_digest in memo_content
    assert "sk-ant-api03-SECRET-RUN-TOKEN" not in memo_content
    assert "SuperSecret123" not in memo_content


def test_cli_export_incident_memo_load_errors_handled_cleanly(tmp_path):
    # 1. Non-existent artifact file
    missing = tmp_path / "non_existent.json"
    out = tmp_path / "memo.md"
    result = runner.invoke(app, ["export-incident-memo", str(missing), "-o", str(out)])
    assert result.exit_code == 2
    assert "cannot load artifact" in result.output

    # 2. Corrupt / non-JSON artifact file
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not a valid json", encoding="utf-8")
    result = runner.invoke(app, ["export-incident-memo", str(corrupt), "-o", str(out)])
    assert result.exit_code == 2
    assert "cannot load artifact" in result.output


def test_cli_export_incident_memo_overwrite_input_fails(tmp_path):
    raw = _sample_trace_with_secrets()
    nt = normalize(raw)
    artifact_path = tmp_path / "trace.json"
    artifact.save(nt, artifact_path)

    result = runner.invoke(app, ["export-incident-memo", str(artifact_path), "-o", str(artifact_path)])
    assert result.exit_code == 2
    assert "output must not overwrite an input" in result.output


def test_cli_export_incident_memo_unwritable_path_fails(tmp_path):
    raw = _sample_trace_with_secrets()
    nt = normalize(raw)
    artifact_path = tmp_path / "trace.json"
    artifact.save(nt, artifact_path)
    bad_target = tmp_path / "trace.json" / "memo.md"

    result = runner.invoke(app, ["export-incident-memo", str(artifact_path), "-o", str(bad_target)])
    assert result.exit_code == 2
    assert "cannot save incident memo" in result.output


def test_save_incident_memo_cleans_up_on_write_or_sync_failure(tmp_path, monkeypatch):
    import os

    target_file = tmp_path / "memo.md"

    # Simulate an error during os.fsync (e.g. disk write failure / EIO)
    def _failing_fsync(_fd):
        raise OSError("Simulated disk error during fsync")

    monkeypatch.setattr(os, "fsync", _failing_fsync)

    with pytest.raises(OSError, match="Simulated disk error"):
        save_incident_memo("# failed memo", target_file)

    # Verify no temporary files were left behind in tmp_path
    remaining_temps = list(tmp_path.glob(".*"))
    assert not remaining_temps, f"Temporary file leaked on write/sync error: {remaining_temps}"


def test_build_incident_memo_long_error_chain_linear_scaling():
    # Long error chain: 1,000 steps where every step is an error and each caused by predecessor
    n_steps = 1000
    steps = [
        Step(
            step_id=f"step-{i:04d}",
            trace_id="long-chain-trace",
            seq=i,
            kind=StepKind.TOOL,
            status=StepStatus.ERROR,
        )
        for i in range(n_steps)
    ]
    edges = [
        Edge(
            type=EdgeType.CAUSED_BY,
            src=f"step-{i:04d}",
            dst=f"step-{i-1:04d}",
            origin=EdgeOrigin.GRAPH_PARENT,
        )
        for i in range(1, n_steps)
    ]
    raw = RawTrace(
        trace=Trace(trace_id="long-chain-trace", source_kind="langgraph"),
        steps=steps,
        causal_edges=edges,
    )
    nt = normalize(raw)

    memo = build_incident_memo(nt, "sha256:" + "c" * 64)

    # In a quadratic implementation, 1,000 steps would emit ~500,000 edge lines.
    # In this linear implementation, each of the 999 edges appears exactly once in the ancestry graph
    # and once as a direct predecessor.
    assert memo.count("caused by") == n_steps - 1
    assert memo.count("[origin: graph_parent]") == 2 * (n_steps - 1)
    # Total lines scale linearly (failure aliases, direct causes, detected patterns, and ancestry graph)
    total_lines = len(memo.splitlines())
    assert total_lines < 6 * n_steps, f"Output unexpectedly large ({total_lines} lines), possible quadratic explosion"


def test_build_incident_memo_distinguishes_containment_and_unknown_from_causation():
    raw = RawTrace(
        trace=Trace(trace_id="containment-trace", source_kind="otlp"),
        steps=[
            Step(step_id="s1", trace_id="containment-trace", seq=0, kind=StepKind.CHAIN),
            Step(step_id="s2", trace_id="containment-trace", seq=1, kind=StepKind.TOOL, status=StepStatus.ERROR),
            Step(step_id="s3", trace_id="containment-trace", seq=2, kind=StepKind.TOOL, status=StepStatus.ERROR),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="s2", dst="s1", origin=EdgeOrigin.SPAN_PARENT_FALLBACK),
            Edge(type=EdgeType.CAUSED_BY, src="s3", dst="s2", origin=EdgeOrigin.LEGACY_UNKNOWN),
        ],
    )
    nt = normalize(raw)
    memo = build_incident_memo(nt, "sha256:" + "d" * 64)

    # Must accurately qualify containment and unknown evidence
    assert "`step_1_chain` [origin: span_parent_fallback (containment only)]" in memo
    assert "`step_2_tool` [origin: legacy_unknown (unknown evidence)]" in memo
    assert "`step_2_tool` preceded by (containment only) `step_1_chain` [origin: span_parent_fallback (containment only)]" in memo
    assert "`step_3_tool` preceded by (unknown evidence) `step_2_tool` [origin: legacy_unknown (unknown evidence)]" in memo

    # Must NOT falsely assert causation for containment or unverified edges
    assert "step_2_tool` caused by" not in memo
    assert "step_3_tool` caused by" not in memo


def test_build_incident_memo_containment_long_error_chain_linear_scaling():
    # Long error chain with containment origins:
    # If analyze(nt) was called, _failures() would treat every node as primary and
    # materialize full ancestor records quadratically. build_incident_memo must bypass this.
    n_steps = 1000
    steps = [
        Step(
            step_id=f"step-{i:04d}",
            trace_id="long-containment-trace",
            seq=i,
            kind=StepKind.TOOL,
            status=StepStatus.ERROR,
        )
        for i in range(n_steps)
    ]
    edges = [
        Edge(
            type=EdgeType.CAUSED_BY,
            src=f"step-{i:04d}",
            dst=f"step-{i-1:04d}",
            origin=EdgeOrigin.SPAN_PARENT_FALLBACK,
        )
        for i in range(1, n_steps)
    ]
    raw = RawTrace(
        trace=Trace(trace_id="long-containment-trace", source_kind="otlp"),
        steps=steps,
        causal_edges=edges,
    )
    nt = normalize(raw)

    memo = build_incident_memo(nt, "sha256:" + "e" * 64)

    # Verify containment edges are preserved and scaled linearly
    assert memo.count("[origin: span_parent_fallback (containment only)]") == 2 * (n_steps - 1)
    total_lines = len(memo.splitlines())
    assert total_lines < 6 * n_steps, f"Containment chain exploded ({total_lines} lines)"


def test_build_incident_memo_rejects_invalid_artifact_digest():
    raw = _sample_trace_with_secrets()
    nt = normalize(raw)

    # Missing sha256 prefix
    with pytest.raises(ValueError, match="Invalid artifact_digest"):
        build_incident_memo(nt, "a" * 64)

    # Uppercase hex digits
    with pytest.raises(ValueError, match="Invalid artifact_digest"):
        build_incident_memo(nt, "sha256:" + "A" * 64)

    # Wrong length
    with pytest.raises(ValueError, match="Invalid artifact_digest"):
        build_incident_memo(nt, "sha256:abcd")

    # Newline injection attempt
    with pytest.raises(ValueError, match="Invalid artifact_digest"):
        build_incident_memo(nt, f"sha256:{'a'*64}\ninjected_field: true")


def test_build_incident_memo_disclosures_for_langgraph_and_links_preserved():
    # Clean LangGraph trace with preserved links and NO error steps
    raw = RawTrace(
        trace=Trace(
            trace_id="lg-clean-trace",
            source_kind="langgraph",
            links_preserved=True,
        ),
        steps=[
            Step(step_id="s1", trace_id="lg-clean-trace", seq=0, kind=StepKind.CHAIN),
            Step(step_id="s2", trace_id="lg-clean-trace", seq=1, kind=StepKind.TOOL),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="s2", dst="s1", origin=EdgeOrigin.GRAPH_PARENT),
        ],
    )
    nt = normalize(raw)
    memo = build_incident_memo(nt, "sha256:" + "f" * 64)

    # Disclosures must inform reader that absence of errors in LangGraph does not prove success,
    # and links cover valid in-trace links only.
    assert LANGGRAPH_FIDELITY_WARNING in memo
    assert LINKS_PRESERVED_FIDELITY_WARNING in memo
    # No error steps, so incident candidate warning is omitted
    assert INCIDENT_FIDELITY_WARNING not in memo
    assert PARENT_ONLY_FIDELITY_WARNING not in memo
