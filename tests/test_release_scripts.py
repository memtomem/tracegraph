"""The release gate and the artifact verifier, exercised without a network or a build.

Both scripts are what stands between a tag and PyPI, and neither runs in the ordinary
test path, so their failure modes are pinned here: each check must refuse the thing it
names, and refusing everything would pass just as well as refusing nothing if only the
happy path were asserted.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import re
import tarfile
import zipfile

import pytest

REPO = Path(__file__).resolve().parents[1]
VERSION = "9.9.9"
STEM = f"agent_tracegraph-{VERSION}"


def _load(name: str):
    script = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"release_{name}", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def artifacts():
    return _load("verify_artifacts")


@pytest.fixture
def gate():
    return _load("require_ci_success")


# --- verify_artifacts -------------------------------------------------------------


def _metadata(name: str = "agent-tracegraph", version: str = VERSION) -> str:
    return f"Metadata-Version: 2.5\nName: {name}\nVersion: {version}\n\nbody text\n"


SDIST_MEMBERS = {
    "PKG-INFO": _metadata(),
    "pyproject.toml": "[project]\n",
    "README.md": "# tracegraph\n",
    "LICENSE": "Apache\n",
    "CHANGELOG.md": "# Changelog\n",
    "SECURITY.md": "# Security policy\n",
    ".gitignore": "dist/\n",
    "src/tracegraph/__init__.py": "__version__ = 'x'\n",
    "contracts/analysis-report.schema.json": "{}\n",
}


def _write_sdist(path: Path, members: dict[str, str], *, root: str = STEM,
                 symlink: str | None = None, duplicate: str | None = None,
                 raw: tuple[str, ...] = ()) -> None:
    """`raw` names are written verbatim, without the root prefix."""
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            payload = content.encode()
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        for name in raw:
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        if duplicate is not None:
            payload = members[duplicate].encode()
            info = tarfile.TarInfo(f"{root}/{duplicate}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if symlink is not None:
            info = tarfile.TarInfo(f"{root}/{symlink}")
            info.type = tarfile.SYMTYPE
            info.linkname = "README.md"
            archive.addfile(info)


def _write_wheel(path: Path, *, metadata: str | None = None, stem: str = STEM,
                 extra: tuple[str, ...] = ()) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tracegraph/__init__.py", "__version__ = 'x'\n")
        archive.writestr(f"{stem}.dist-info/METADATA", metadata or _metadata())
        for name in extra:
            archive.writestr(name, "x\n")


def _build_dist(tmp_path: Path, **sdist_kwargs) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    members = sdist_kwargs.pop("members", SDIST_MEMBERS)
    _write_sdist(dist / f"{STEM}.tar.gz", members, **sdist_kwargs)
    _write_wheel(dist / f"{STEM}-py3-none-any.whl")
    (dist / ".gitignore").write_text("*\n")
    return dist


def _verify(artifacts, dist: Path):
    return artifacts.verify(dist, VERSION, REPO / "pyproject.toml")


def test_a_well_formed_pair_passes(artifacts, tmp_path):
    """The allowlist is read from the real pyproject, so this pins that parse too."""
    result = _verify(artifacts, _build_dist(tmp_path))
    assert result["name"] == "agent-tracegraph"
    assert result["sdist"] == f"{STEM}.tar.gz"
    assert result["sdist_members"] == len(SDIST_MEMBERS)


def test_an_extra_file_in_the_directory_is_refused(artifacts, tmp_path):
    """Everything in the directory is handed to the publisher."""
    dist = _build_dist(tmp_path)
    (dist / "notes.txt").write_text("hello")
    with pytest.raises(artifacts.VerifyFailure, match="unexpected file"):
        _verify(artifacts, dist)


def test_a_symlink_named_like_a_distribution_is_refused(artifacts, tmp_path):
    """`is_file()` follows links, so the symlink check has to come first.

    The link carries an *expected* name, so no other check objects to it: without the
    symlink test this directory verifies clean and the publisher uploads whatever the
    link points at.
    """
    dist = _build_dist(tmp_path)
    real = dist / f"{STEM}.tar.gz"
    elsewhere = tmp_path / "elsewhere.tar.gz"
    real.rename(elsewhere)
    real.symlink_to(elsewhere)
    with pytest.raises(artifacts.VerifyFailure, match="symlink"):
        _verify(artifacts, dist)


def test_a_version_that_does_not_match_the_build_is_refused(artifacts, tmp_path):
    dist = _build_dist(tmp_path)
    with pytest.raises(artifacts.VerifyFailure, match="missing"):
        artifacts.verify(dist, "1.2.3", REPO / "pyproject.toml")


def test_metadata_version_disagreeing_with_the_tag_is_refused(artifacts, tmp_path):
    """The filename is not evidence: hatchling writes both, and only one is checked here."""
    dist = _build_dist(tmp_path)
    _write_wheel(dist / f"{STEM}-py3-none-any.whl", metadata=_metadata(version="0.0.1"))
    with pytest.raises(artifacts.VerifyFailure, match="wheel METADATA Version"):
        _verify(artifacts, dist)


def test_metadata_name_disagreeing_with_pyproject_is_refused(artifacts, tmp_path):
    dist = _build_dist(tmp_path)
    _write_wheel(dist / f"{STEM}-py3-none-any.whl", metadata=_metadata(name="tracegraph"))
    with pytest.raises(artifacts.VerifyFailure, match="wheel METADATA Name"):
        _verify(artifacts, dist)


@pytest.mark.parametrize("field, wrong, message", [
    ("version", {"version": "0.0.1"}, "sdist PKG-INFO Version"),
    ("name", {"name": "tracegraph"}, "sdist PKG-INFO Name"),
])
def test_sdist_metadata_is_checked_as_well_as_the_wheels(artifacts, tmp_path, field,
                                                         wrong, message):
    """Two archives are uploaded, and PyPI reads the name and version out of each."""
    members = dict(SDIST_MEMBERS)
    members["PKG-INFO"] = _metadata(**wrong)
    dist = _build_dist(tmp_path, members=members)
    with pytest.raises(artifacts.VerifyFailure, match=message):
        _verify(artifacts, dist)


@pytest.mark.parametrize("member", ["tests/test_cli.py", "docs/HANDOFF.md",
                                    ".github/workflows/test.yml", "uv.lock"])
def test_repository_material_is_refused_even_though_the_allowlist_is_derived(
    artifacts, tmp_path, member
):
    """The deny list is the independent statement: widening the allowlist must not help."""
    members = dict(SDIST_MEMBERS)
    members[member] = "x\n"
    dist = _build_dist(tmp_path, members=members)
    with pytest.raises(artifacts.VerifyFailure, match="must never ship"):
        _verify(artifacts, dist)


def test_a_path_outside_the_allowlist_is_refused(artifacts, tmp_path):
    members = dict(SDIST_MEMBERS)
    members["Makefile"] = "all:\n"
    dist = _build_dist(tmp_path, members=members)
    with pytest.raises(artifacts.VerifyFailure, match="outside the pyproject allowlist"):
        _verify(artifacts, dist)


def test_a_missing_required_member_is_refused(artifacts, tmp_path):
    members = {name: text for name, text in SDIST_MEMBERS.items() if name != "LICENSE"}
    dist = _build_dist(tmp_path, members=members)
    with pytest.raises(artifacts.VerifyFailure, match="missing LICENSE"):
        _verify(artifacts, dist)


@pytest.mark.parametrize("member, reason", [
    # Starts with an allowlisted prefix and with no denied one, yet lands in `docs/`.
    ("src/tracegraph/../../docs/private.txt", "'..' component"),
    # Leaves the versioned root altogether: an extractor writes it beside the archive.
    ("src/tracegraph/../../../outside.txt", "'..' component"),
    ("../outside.txt", "'..' component"),
    ("src/./tracegraph/x.py", "'.' component"),
    # A Windows extractor reads this as a separator; splitting on "/" sees one component.
    ("src/tracegraph/..\\..\\x.txt", "backslash"),
])
def test_an_sdist_path_that_resolves_elsewhere_is_refused(artifacts, tmp_path, member,
                                                          reason):
    """Neither list means anything on a path that does not go where it reads."""
    members = dict(SDIST_MEMBERS)
    members[member] = "secret\n"
    dist = _build_dist(tmp_path, members=members)
    with pytest.raises(artifacts.VerifyFailure, match=reason):
        _verify(artifacts, dist)


@pytest.mark.parametrize("raw, reason", [
    (f"{STEM}/src/../../evil/", "'..' component"),
    ("/etc/passwd", "absolute path"),
])
def test_traversal_is_refused_in_entries_that_carry_no_content(artifacts, tmp_path, raw,
                                                               reason):
    """Directory entries and absolute names skip the member checks that come later."""
    dist = _build_dist(tmp_path, raw=(raw,))
    with pytest.raises(artifacts.VerifyFailure, match=reason):
        _verify(artifacts, dist)


def test_a_wheel_path_that_resolves_elsewhere_is_refused(artifacts, tmp_path):
    """The wheel is uploaded and extracted too; only its metadata was being read."""
    dist = _build_dist(tmp_path)
    _write_wheel(dist / f"{STEM}-py3-none-any.whl", extra=("tracegraph/../../evil.py",))
    with pytest.raises(artifacts.VerifyFailure, match="wheel member carries"):
        _verify(artifacts, dist)


def test_a_symlink_inside_the_sdist_is_refused(artifacts, tmp_path):
    dist = _build_dist(tmp_path, symlink="LINK.md")
    with pytest.raises(artifacts.VerifyFailure, match="not a plain file"):
        _verify(artifacts, dist)


def test_a_duplicate_sdist_member_is_refused(artifacts, tmp_path):
    """A second record for a path can carry different bytes than the first."""
    dist = _build_dist(tmp_path, duplicate="README.md")
    with pytest.raises(artifacts.VerifyFailure, match="duplicate"):
        _verify(artifacts, dist)


def test_a_member_outside_the_versioned_root_is_refused(artifacts, tmp_path):
    dist = _build_dist(tmp_path, root="agent_tracegraph-0.0.1")
    with pytest.raises(artifacts.VerifyFailure, match="outside the"):
        _verify(artifacts, dist)


@pytest.mark.parametrize("body, message", [
    ('[project]\nname = "agent-tracegraph"\n', "no [tool.hatch.build.targets.sdist]"),
    ('[project]\nname = "agent-tracegraph"\n'
     "[tool.hatch.build.targets.sdist]\ninclude = []\n", "include list is empty"),
    ('[tool.hatch.build.targets.sdist]\ninclude = ["/src/"]\n', "no [project] name"),
])
def test_a_missing_allowlist_is_reported_as_itself(artifacts, tmp_path, body, message):
    """Without the allowlist hatchling ships everything; say that, not `KeyError: include`."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(body)
    # re.escape: the messages name TOML tables, and `[project]` is a character class.
    with pytest.raises(artifacts.VerifyFailure, match=re.escape(message)):
        artifacts.read_project(pyproject)


def test_the_real_pyproject_allowlist_is_present_and_anchored(artifacts):
    """A bare `README.md` matches at every depth; the entries must stay root-anchored."""
    name, patterns = artifacts.read_project(REPO / "pyproject.toml")
    assert name == "agent-tracegraph"
    assert all(pattern.startswith("/") for pattern in patterns), patterns


# --- require_ci_success -----------------------------------------------------------

SHA = "a" * 40


def _fetcher(compare: dict | Exception, runs: dict | Exception):
    def fetch(url: str) -> dict:
        payload = compare if "/compare/" in url else runs
        if isinstance(payload, Exception):
            raise payload
        return payload
    return fetch


def _run(**overrides) -> dict:
    run = {"id": 1, "head_sha": SHA, "event": "push", "status": "completed",
           "conclusion": "success", "head_branch": "tracegraph-mvp"}
    run.update(overrides)
    return run


def _gate(gate, compare, runs):
    return gate.gate("memtomem/tracegraph", "tracegraph-mvp", "test.yml", SHA,
                     _fetcher(compare, runs))


@pytest.mark.parametrize("status", ["identical", "behind"])
def test_a_green_push_run_on_the_default_branch_passes(gate, status):
    result = _gate(gate, {"status": status}, {"workflow_runs": [_run()]})
    assert result == {"compare_status": status, "run_id": 1}


def test_a_tag_push_run_counts(gate):
    """A tag push reports the tag as head_branch; it is still a run of this commit."""
    runs = {"workflow_runs": [_run(id=7, head_branch="v0.2.0")]}
    assert _gate(gate, {"status": "identical"}, runs)["run_id"] == 7


def test_an_earlier_failure_does_not_veto_a_later_success(gate):
    """Re-running a flaky job is the normal fix; the question is whether it ever passed."""
    runs = {"workflow_runs": [_run(id=1, conclusion="failure"), _run(id=2)]}
    assert _gate(gate, {"status": "identical"}, runs)["run_id"] == 2


@pytest.mark.parametrize("status", ["ahead", "diverged"])
def test_a_commit_that_is_not_on_the_default_branch_is_refused(gate, status):
    with pytest.raises(gate.CIGateError, match="is not on tracegraph-mvp"):
        _gate(gate, {"status": status}, {"workflow_runs": [_run()]})


def test_an_unknown_commit_is_refused(gate):
    """A 404 from compare is its own failure, not a missing-run message."""
    error = gate.CIGateError("GitHub API returned 404")
    with pytest.raises(gate.CIGateError, match="404"):
        _gate(gate, error, {"workflow_runs": [_run()]})


def test_no_runs_yet_is_refused_with_the_rerun_instruction(gate):
    with pytest.raises(gate.CIGateError) as caught:
        _gate(gate, {"status": "identical"}, {"workflow_runs": []})
    assert "no push runs" in str(caught.value)
    # The gate does not wait, so the message has to say what to do instead.
    assert "re-run this workflow run" in str(caught.value)


def test_an_in_progress_run_is_not_a_pass(gate):
    runs = {"workflow_runs": [_run(status="in_progress", conclusion=None)]}
    with pytest.raises(gate.CIGateError, match="re-run this workflow run"):
        _gate(gate, {"status": "identical"}, runs)


def test_a_failed_run_is_refused(gate):
    runs = {"workflow_runs": [_run(conclusion="failure")]}
    with pytest.raises(gate.CIGateError, match="completed/failure"):
        _gate(gate, {"status": "identical"}, runs)


def test_a_pull_request_run_is_not_evidence(gate):
    """It was green before the merge commit changed the tree."""
    runs = {"workflow_runs": [_run(event="pull_request")]}
    with pytest.raises(gate.CIGateError, match="no push runs"):
        _gate(gate, {"status": "identical"}, runs)


def test_a_run_for_a_different_commit_is_ignored(gate):
    """The query parameters are a request, not a guarantee; the filter is re-applied."""
    runs = {"workflow_runs": [_run(head_sha="b" * 40)]}
    with pytest.raises(gate.CIGateError, match="no push runs"):
        _gate(gate, {"status": "identical"}, runs)


def test_a_malformed_runs_payload_is_refused(gate):
    with pytest.raises(gate.CIGateError, match="no workflow_runs list"):
        _gate(gate, {"status": "identical"}, {"message": "Not Found"})


def test_an_unread_page_of_runs_is_reported_as_missing_evidence(gate):
    """Saying "it never passed" on a page we did not read would be a false rejection."""
    runs = {"total_count": 80, "workflow_runs": [_run(conclusion="failure")]}
    with pytest.raises(gate.CIGateError, match="only 1 of 80"):
        _gate(gate, {"status": "identical"}, runs)
