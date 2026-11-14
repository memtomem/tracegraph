"""A directory artifact that is the ``--out`` file under another name (#204).

``summarize`` and ``export-review-candidates`` leave such a file out of the run. When it is a
hardlink in another directory than ``--out`` they say so on stderr, and ``--strict`` refuses
it. The ``--out`` file's own entry, however it is spelled or reached, stays silently left out,
and so does a hardlink in ``--out``'s own directory (directories are compared, never names).
"""

import json
import os
import re

import pytest
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph.cli import app
from tracegraph.model import Edge, EdgeOrigin, EdgeType, RawTrace, Step, StepKind, StepStatus, Trace
from tracegraph.normalize import normalize

runner = CliRunner()
WARNING = "that are the --out file under another name (a hardlink)"
STRICT = "--strict: 1 discovered file(s) are the --out file under another name"


def _retry(trace_id):
    """A trace that matches ``tool-retry-failure`` (summarize groups it)."""
    specs = [("search", StepKind.TOOL, StepStatus.OK), ("retry:search", StepKind.CHAIN, StepStatus.OK),
             ("search", StepKind.TOOL, StepStatus.ERROR)]
    steps = [Step(step_id=f"{trace_id}-{i}", trace_id=trace_id, seq=i, name=name, kind=kind,
                  status=status) for i, (name, kind, status) in enumerate(specs)]
    edges = [Edge(type=EdgeType.CAUSED_BY, src=f"{trace_id}-{i}", dst=f"{trace_id}-{i - 1}",
                  origin=EdgeOrigin.GRAPH_PARENT) for i in (1, 2)]
    return normalize(RawTrace(trace=Trace(trace_id=trace_id, source_kind="x"), steps=steps,
                              causal_edges=edges))


def _plain(trace_id):
    """A trace no review-exportable preset matches, so export succeeds with no candidates."""
    step = Step(step_id=f"{trace_id}-0", trace_id=trace_id, seq=0, name="plan",
                kind=StepKind.CHAIN, status=StepStatus.OK)
    return normalize(RawTrace(trace=Trace(trace_id=trace_id, source_kind="x"), steps=[step],
                              causal_edges=[]))


COMMANDS = {
    "summarize": (["summarize", "tool-retry-failure"], _retry),
    "export": (["export-review-candidates", "tool-retry-failure"], _plain),
}


def _cohort(path, make, *ids):
    path.mkdir(parents=True, exist_ok=True)
    for tid in ids:
        artifact.save(make(tid), path / f"{tid}.json")
    return path


def _text(res):
    """stderr with typer's panel borders and Rich's wrapping collapsed to single spaces."""
    return re.sub(r"[\s│╭╮╰╯─]+", " ", res.stderr)


def _invoke(command, *args):
    head, _ = COMMANDS[command]
    return runner.invoke(app, [*head, *map(str, args)])


def _inode(path):
    return os.stat(path).st_ino


@pytest.mark.parametrize("command", COMMANDS)
def test_a_hardlink_of_out_in_a_scanned_directory_is_named(tmp_path, command):
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    kept = (cohort / "A.json").read_bytes()
    out = tmp_path / "out.json"
    out.hardlink_to(cohort / "A.json")

    res = _invoke(command, cohort, "--out", out)
    assert res.exit_code == 0, res.output
    assert f"left out 1 file(s) {WARNING}: A.json" in _text(res)
    assert (cohort / "A.json").read_bytes() == kept
    assert _inode(out) != _inode(cohort / "A.json")  # the report replaced the link, not A
    if command == "summarize":
        assert json.loads(out.read_text())["coverage"]["artifacts_discovered"] == 1


@pytest.mark.parametrize("command", COMMANDS)
def test_strict_refuses_a_hardlink_of_out_and_writes_nothing(tmp_path, command):
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    kept = (cohort / "A.json").read_bytes()
    out = tmp_path / "out.json"
    out.hardlink_to(cohort / "A.json")

    res = _invoke(command, "--strict", cohort, "--out", out)
    assert res.exit_code == 2, res.output
    err = _text(res)
    assert err.index(WARNING) < err.index(STRICT)
    assert _inode(out) == _inode(cohort / "A.json")
    assert (cohort / "A.json").read_bytes() == kept


@pytest.mark.parametrize("command", COMMANDS)
def test_the_warning_precedes_a_later_explicit_input_refusal(tmp_path, command):
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    out = tmp_path / "out.json"
    out.hardlink_to(cohort / "A.json")

    res = _invoke(command, cohort, out, "--out", out)
    assert res.exit_code == 2, res.output
    err = _text(res)
    assert err.index(f"{WARNING}: A.json") < err.index("--out must not overwrite an input artifact")


@pytest.mark.parametrize("command", COMMANDS)
def test_the_warning_precedes_no_artifact_files_found(tmp_path, command):
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A")
    out = tmp_path / "out.json"
    out.hardlink_to(cohort / "A.json")

    res = _invoke(command, cohort, "--out", out)
    assert res.exit_code == 2, res.output
    err = _text(res)
    assert err.index(f"{WARNING}: A.json") < err.index("no artifact files found")


@pytest.mark.parametrize("command", COMMANDS)
def test_strict_with_a_skipped_file_and_a_hardlink_keeps_the_skipped_refusal(tmp_path, command):
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    (cohort / "bad.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"
    out.hardlink_to(cohort / "A.json")

    res = _invoke(command, "--strict", cohort, "--out", out)
    assert res.exit_code == 2, res.output
    err = _text(res)
    assert err.index(WARNING) < err.index("skipped 1 non-artifact file(s): bad.json")
    assert "--strict: 1 discovered file(s) could not be loaded" in err
    assert STRICT not in err


@pytest.mark.parametrize("command", COMMANDS)
def test_symlinked_directories_still_compare_real_directories(tmp_path, command):
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    scan_link = tmp_path / "scan-link"
    scan_link.symlink_to(cohort, target_is_directory=True)
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    out_link = tmp_path / "out-link"
    out_link.symlink_to(outdir, target_is_directory=True)

    for scanned, out in ((scan_link, tmp_path / "out.json"), (cohort, out_link / "out.json")):
        out.unlink(missing_ok=True)
        out.hardlink_to(cohort / "A.json")
        res = _invoke(command, "--strict", scanned, "--out", out)
        assert res.exit_code == 2, (scanned, out, res.output)
        assert STRICT in _text(res)

    # The scanned directory reached through a link is still --out's own directory.
    report = cohort / "report.json"
    for _ in range(2):
        res = _invoke(command, "--strict", scan_link, "--out", report)
        assert res.exit_code == 0, res.output
        assert WARNING not in _text(res)


@pytest.mark.parametrize("command", COMMANDS)
def test_the_out_entry_itself_and_a_link_to_it_stay_silent(tmp_path, command):
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    report = cohort / "report.json"
    for _ in range(2):  # the second run finds the first run's report in the directory
        res = _invoke(command, "--strict", cohort, "--out", report)
        assert res.exit_code == 0, res.output
        assert WARNING not in _text(res)
    (cohort / "alias.json").symlink_to(report)
    res = _invoke(command, "--strict", cohort, "--out", report)
    assert res.exit_code == 0, res.output
    assert WARNING not in _text(res)


@pytest.mark.parametrize("command", COMMANDS)
def test_a_hardlink_in_outs_own_directory_stays_silent(tmp_path, command):
    """The accepted limit of comparing directories only: as in 0.10.0, no warning."""
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    out = cohort / "out.json"
    out.hardlink_to(cohort / "A.json")

    res = _invoke(command, "--strict", cohort, "--out", out)
    assert res.exit_code == 0, res.output
    assert WARNING not in _text(res)
    if command == "summarize":
        assert json.loads(out.read_text())["coverage"]["artifacts_discovered"] == 1


@pytest.mark.parametrize("command", COMMANDS)
def test_an_out_that_does_not_exist_yet_leaves_nothing_out(tmp_path, command):
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    out = tmp_path / "fresh.json"
    res = _invoke(command, "--strict", cohort, "--out", out)
    assert res.exit_code == 0, res.output
    assert WARNING not in _text(res)
    if command == "summarize":
        assert json.loads(out.read_text())["coverage"]["artifacts_discovered"] == 2


def _aliases(directory, written, spelled):
    """Whether this filesystem opens ``written`` when asked for ``spelled``, as one entry."""
    (directory / written).write_text("probe", encoding="utf-8")
    try:
        return (directory / spelled).exists() and os.listdir(directory) == [written]
    finally:
        (directory / written).unlink()


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    ("entry", "spelled"),
    [("Report.json", "report.json"), ("Ᲊ.json", "ᲊ.json"),
     ("é.json", "\xe9.json")],
    ids=["case", "georgian-mtavruli", "nfd-nfc"],
)
def test_a_spelling_alias_of_out_stays_silent(tmp_path, command, entry, spelled):
    probe = tmp_path / "probe"
    probe.mkdir()
    if not _aliases(probe, entry, spelled):
        pytest.skip(f"this filesystem does not alias {entry!a} and {spelled!a}")
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    (cohort / entry).write_text("{}", encoding="utf-8")  # an earlier report, as found on disk

    res = _invoke(command, "--strict", cohort, "--out", cohort / spelled)
    assert res.exit_code == 0, res.output
    assert WARNING not in _text(res)


@pytest.mark.parametrize("command", COMMANDS)
def test_a_case_alias_of_outs_directory_stays_silent(tmp_path, command):
    probe = tmp_path / "probe"
    probe.mkdir()
    if not _aliases(probe, "Corpus", "corpus"):
        pytest.skip("this filesystem is case-sensitive")
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "Corpus", make, "A", "B")
    (cohort / "Report.json").write_text("{}", encoding="utf-8")

    res = _invoke(command, "--strict", cohort, "--out", tmp_path / "corpus" / "Report.json")
    assert res.exit_code == 0, res.output
    assert WARNING not in _text(res)


@pytest.mark.parametrize("command", COMMANDS)
def test_a_symlink_is_judged_by_the_directory_it_leads_to(tmp_path, command):
    """In ``--out``'s own directory, a symlink to a hardlink of ``--out`` elsewhere is named."""
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    report = cohort / "report.json"
    report.write_text("{}", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "copy.json").hardlink_to(report)
    (cohort / "link.json").symlink_to(elsewhere / "copy.json")

    res = _invoke(command, "--strict", cohort, "--out", report)
    assert res.exit_code == 2, res.output
    assert f"{WARNING}: link.json" in _text(res)
    assert STRICT in _text(res)


def test_directories_that_cannot_be_compared_mean_silence(tmp_path, monkeypatch):
    from tracegraph import cli

    out = tmp_path / "out.json"
    out.write_text("{}", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.json").hardlink_to(out)
    assert cli._beside_out(other / "a.json", out.resolve()) is False

    def refuse(self, *args, **kwargs):
        raise RuntimeError("Symlink loop")

    monkeypatch.setattr(type(out), "resolve", refuse)
    assert cli._beside_out(other / "a.json", out) is True


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("side", ["scanned", "out"])
def test_a_descriptor_alias_of_a_single_name_file_stays_silent(tmp_path, command, side):
    """A ``/dev/fd/N`` alias, N open on the ``--out`` file, on either side: one name, so silent.

    On macOS the alias resolves to ``/dev/fd/N`` itself, outside both directories; on Linux it
    resolves to the file. Either way the file has a single name, so nothing is reported.
    """
    make = COMMANDS[command][1]
    cohort = _cohort(tmp_path / "cohort", make, "A", "B")
    report = cohort / "report.json"
    report.write_text("{}", encoding="utf-8")
    with report.open("rb") as held:
        alias = (cohort if side == "scanned" else tmp_path) / "alias.json"
        alias.symlink_to(f"/dev/fd/{held.fileno()}")
        out = report if side == "scanned" else alias
        res = _invoke(command, "--strict", cohort, "--out", out)
    assert res.exit_code == 0, res.output
    assert WARNING not in _text(res)


def test_only_a_file_with_another_name_is_reported(tmp_path, monkeypatch):
    """The link count gates the directory comparison: one name means it is ``--out`` itself."""
    from tracegraph import cli

    out = tmp_path / "out.json"
    out.write_text("{}", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.json").hardlink_to(out)
    assert cli._beside_out(other / "a.json", out.resolve()) is False

    real_stat = os.stat

    def one_name(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if os.fspath(path).endswith("a.json"):
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, 1, *tuple(st)[4:10]))
        return st

    monkeypatch.setattr(cli.os, "stat", one_name)
    assert cli._beside_out(other / "a.json", out.resolve()) is True
