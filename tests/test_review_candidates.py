"""T3 versioned review-candidate contract and CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph.analysis import Match, PRESETS, PathPattern, StepPredicate
from tracegraph.cli import app
from tracegraph.model import Edge, EdgeType, RawTrace, Step, StepKind, StepStatus, Trace
from tracegraph.normalize import normalize
from tracegraph.review_candidates import ReviewCandidateReport, build_report, dumps


runner = CliRunner()
ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "contracts" / "review-candidates.schema.json"
GOLDEN = ROOT / "tests" / "fixtures" / "review-candidates" / "v1.json"


def _write_trace(
    path: Path,
    trace_id: str,
    *,
    run_id: str | None,
    tool_key: str = "syncmill::board_stats",
    failing: bool = True,
) -> None:
    status = StepStatus.ERROR if failing else StepStatus.OK
    steps = [
        Step(step_id=f"{trace_id}-0", trace_id=trace_id, seq=0, name="private/prompt.txt"),
        Step(
            step_id=f"{trace_id}-1",
            trace_id=trace_id,
            seq=1,
            name=tool_key,
            kind=StepKind.TOOL,
            status=status,
            error_msg="password=secret stdout and patch body",
        ),
    ]
    edges = [Edge(type=EdgeType.CAUSED_BY, src=steps[1].step_id, dst=steps[0].step_id)]
    nt = normalize(
        RawTrace(
            trace=Trace(trace_id=trace_id, source_kind="test", run_id=run_id),
            steps=steps,
            causal_edges=edges,
        )
    )
    artifact.save(nt, path)


def _export(preset: str, source: Path, target: Path, *extra: str):
    return runner.invoke(
        app,
        ["export-review-candidates", preset, str(source), "--out", str(target), *extra],
    )


def test_contract_schema_and_canonical_fixture():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    fixture = json.loads(GOLDEN.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(fixture)
    assert dumps(ReviewCandidateReport.model_validate(fixture)) == GOLDEN.read_text(encoding="utf-8")
    Draft202012Validator(schema).validate({**fixture, "future_additive_field": True})
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({**fixture, "schema_version": 2})


def test_path_pattern_metadata_is_paired_and_positive():
    step = (StepPredicate(kind=StepKind.TOOL),)
    with pytest.raises(ValueError, match="set together"):
        PathPattern(step, pattern_id="only-id")
    with pytest.raises(ValueError, match="positive"):
        PathPattern(step, pattern_id="x", pattern_version=0)
    assert all(name == pattern.pattern_id for name, pattern in PRESETS.items())
    assert {pattern.pattern_version for pattern in PRESETS.values()} == {1}


def test_cli_exports_exact_digest_minimal_fields_and_redacts(tmp_path):
    source = tmp_path / "trace.json"
    target = tmp_path / "candidates.json"
    _write_trace(source, "trace-a", run_id="run-a")

    result = _export("tool-failure", source, target)
    assert result.exit_code == 0, result.output
    payload = json.loads(target.read_text(encoding="utf-8"))
    expected_digest = f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"
    assert payload == {
        "schema_version": 1,
        "kind": "tracegraph.review-candidates",
        "candidates": [
            {
                "run_id": "run-a",
                "pattern_id": "tool-failure",
                "pattern_version": 1,
                "tool_key": "syncmill::board_stats",
                "artifact_digest": expected_digest,
            }
        ],
    }
    text = target.read_text(encoding="utf-8")
    for forbidden in ("password", "stdout", "patch body", "private/prompt", "trace-a-1"):
        assert forbidden not in text


def test_batch_is_sorted_deduplicated_and_repeatable(tmp_path):
    source_a = tmp_path / "z.json"
    source_b = tmp_path / "a.json"
    target = tmp_path / "report.json"
    _write_trace(source_a, "trace-z", run_id="run-z")
    _write_trace(source_b, "trace-a", run_id="run-a", tool_key="filesystem::write_file")

    first = _export("tool-failure", tmp_path, target)
    assert first.exit_code == 0, first.output
    first_bytes = target.read_bytes()
    keys = [item["tool_key"] for item in json.loads(first_bytes)["candidates"]]
    assert keys == ["filesystem::write_file", "syncmill::board_stats"]

    second = _export("tool-failure", tmp_path, target)
    assert second.exit_code == 0, second.output
    assert target.read_bytes() == first_bytes


def test_builder_deduplicates_identical_candidate_tuples(tmp_path):
    source = tmp_path / "trace.json"
    _write_trace(source, "dupe", run_id="run-dupe")
    nt = artifact.load(source)
    endpoint = next(step for step in nt.steps if step.kind is StepKind.TOOL)
    duplicate = Match(trace_id="dupe", step_ids=[endpoint.step_id], labels=[endpoint.name])
    digest = f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"
    report = build_report(
        PRESETS["tool-failure"],
        [duplicate, duplicate],
        {"dupe": nt},
        {"dupe": digest},
    )
    assert len(report.candidates) == 1


@pytest.mark.cypher
def test_memory_and_kuzu_exports_are_byte_identical(tmp_path):
    pytest.importorskip("kuzu")
    source = tmp_path / "trace.json"
    memory_out = tmp_path / "memory.json"
    kuzu_out = tmp_path / "kuzu.json"
    _write_trace(source, "backend", run_id="run-backend")
    memory = _export("tool-failure", source, memory_out)
    kuzu = _export("tool-failure", source, kuzu_out, "--backend", "kuzu")
    assert memory.exit_code == kuzu.exit_code == 0
    assert memory_out.read_bytes() == kuzu_out.read_bytes()


def test_no_match_writes_empty_report_and_succeeds_without_run_id(tmp_path):
    source = tmp_path / "clean.json"
    target = tmp_path / "report.json"
    _write_trace(source, "clean", run_id=None, failing=False)
    result = _export("tool-failure", source, target)
    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text(encoding="utf-8"))["candidates"] == []


@pytest.mark.parametrize(
    ("preset", "run_id", "tool_key", "message"),
    [
        ("error", "run", "syncmill::board_stats", "not review-exportable"),
        ("tool-failure", None, "syncmill::board_stats", "has no run_id"),
        ("tool-failure", "run", "board_stats", "server-qualified"),
    ],
)
def test_invalid_export_preserves_existing_output(
    tmp_path, preset, run_id, tool_key, message
):
    source = tmp_path / "trace.json"
    target = tmp_path / "report.json"
    _write_trace(source, "bad", run_id=run_id, tool_key=tool_key)
    target.write_text("keep-me\n", encoding="utf-8")
    result = _export(preset, source, target)
    assert result.exit_code != 0
    assert message in result.output
    assert target.read_text(encoding="utf-8") == "keep-me\n"


def test_output_must_not_be_an_explicit_input(tmp_path):
    source = tmp_path / "trace.json"
    _write_trace(source, "same", run_id="run")
    result = _export("tool-failure", source, source)
    assert result.exit_code != 0
    assert "must not overwrite an input artifact" in result.output


def test_presets_and_query_stamp_pattern_version(tmp_path):
    source = tmp_path / "trace.json"
    _write_trace(source, "stamped", run_id="run")
    listed = runner.invoke(app, ["presets"])
    queried = runner.invoke(app, ["query", "tool-failure", str(source)])
    assert listed.exit_code == queried.exit_code == 0
    assert "tool-failure@v1" in listed.output
    assert "review-exportable" in listed.output
    assert "tool-failure@v1" in queried.output
