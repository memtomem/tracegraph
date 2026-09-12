"""Pin the boundary that keeps publishing authority away from build code.

`release.yml` is the only workflow that can upload to PyPI as this project, and the one
job holding the OIDC token is deliberately small: no checkout, no dependency install, one
coreutils shell step. Nothing in CI notices when that shrinks back -- adding a checkout
step to the publish job looks like a convenience and silently returns every build script
and transitive build dependency to the job that can publish.

This asserts the shape, not the behaviour. It is deliberately short: the sibling project's
equivalent grew to several hundred lines of command allowlisting, and the checks that
actually matter are the few below.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

import pytest
import yaml  # a declared dev dependency: skipping here would hide every check below

REPO = Path(__file__).resolve().parents[1]
PUBLISH_ALLOWED_ACTIONS = {"actions/download-artifact", "pypa/gh-action-pypi-publish"}
SHA_PIN = re.compile(r"^[0-9a-f]{40}$")
TAG_PIN = re.compile(r"^v\d+(\.\d+)*$")


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())


def _permissions(block: dict) -> dict:
    """Normalize `permissions`, which may be a mapping or the `write-all` scalar.

    As a scalar it grants everything, so a plain `"id-token" in permissions` membership
    test reads clean on the one value that is worst.
    """
    permissions = block.get("permissions", {})
    if isinstance(permissions, str):
        return {"__scalar__": permissions}
    return permissions or {}


def _steps(job: dict) -> list[dict]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def test_only_the_publish_job_can_mint_a_token(workflow):
    assert "id-token" not in _permissions(workflow)
    assert _permissions(workflow).get("__scalar__") != "write-all"
    holders = {name for name, job in workflow["jobs"].items()
               if "id-token" in _permissions(job)
               or _permissions(job).get("__scalar__") == "write-all"}
    assert holders == {"publish"}


def _live_commands(job: dict) -> str:
    """Every command the job would actually execute.

    Comment lines are dropped -- a commented-out invocation satisfies a substring test
    while running nothing -- and a step that cannot fail the job is not evidence either.
    """
    lines = []
    for step in _steps(job):
        if "run" not in step:
            continue
        assert "continue-on-error" not in step, step.get("name")
        assert "if" not in step, step.get("name")
        lines += [line for line in step["run"].splitlines()
                  if not line.lstrip().startswith("#")]
    return "\n".join(lines)


def test_the_build_job_runs_the_checks_it_is_there_for(workflow):
    """Shape assertions do not notice a step that quietly stops being invoked."""
    commands = _live_commands(workflow["jobs"]["build"])
    assert "scripts/require_ci_success.py" in commands
    assert "scripts/verify_artifacts.py" in commands
    assert "twine@7.0.0 check" in commands
    assert "sha256sum dist/*" in commands


def test_the_wheel_smoke_binds_the_installed_version_to_the_tag(workflow):
    """Without this the distribution-name lookup can fail silently as 0.0.0.dev0."""
    commands = _live_commands(workflow["jobs"]["verify-wheel"])
    assert "scripts/verify_wheel.py" in commands
    assert "--expected-version" in commands


def test_the_publish_job_runs_no_repository_code(workflow):
    publish = workflow["jobs"]["publish"]
    uses = [step["uses"].split("@")[0] for step in _steps(publish) if "uses" in step]
    assert "actions/checkout" not in uses
    assert set(uses) <= PUBLISH_ALLOWED_ACTIONS, uses
    runs = [step["run"] for step in _steps(publish) if "run" in step]
    assert len(runs) == 1, "the publish job should hold exactly one shell step"
    # `${{ }}` in a run: block is textual substitution, so a build-produced value with a
    # quote in it would become shell code -- inside the job that holds the token.
    assert "${{" not in runs[0]
    # And that step must still be the digest check: counting steps does not notice one
    # whose body was replaced.
    assert "sha256sum --check --strict" in runs[0]
    # Each check in that block is a bare `test`, so without this the step would run past
    # a failure and exit on its last command's status. Not inherited from the runner
    # default on purpose: `tests/test_release_workflow_shape.py` executes this block with
    # a plain shell, and it must fail closed there too.
    assert "set -euo pipefail" in runs[0]


def test_the_publish_job_waits_for_the_build_and_the_wheel_smoke(workflow):
    publish = workflow["jobs"]["publish"]
    needs = publish["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert set(needs) == {"build", "verify-wheel"}
    assert publish["environment"]["name"] == "pypi"
    # A job- or step-level `if:` can let a failed verification through silently.
    assert "if" not in publish
    assert "continue-on-error" not in publish
    for step in _steps(publish):
        assert "continue-on-error" not in step
        if "if" in step:
            assert step["uses"].startswith("pypa/gh-action-pypi-publish@")


def test_publisher_steps_are_sha_pinned_and_first_party_actions_are_tag_pinned(workflow):
    """The repo convention: a third party can move a tag unilaterally, GitHub cannot."""
    for job in workflow["jobs"].values():
        for step in _steps(job):
            if "uses" not in step:
                continue
            action, _, ref = step["uses"].partition("@")
            if action.startswith("actions/"):
                assert TAG_PIN.match(ref), step["uses"]
            else:
                assert SHA_PIN.match(ref), step["uses"]


def test_production_does_not_skip_an_existing_version(workflow):
    """A version already on PyPI means something went wrong; a green check would hide it."""
    publishers = [step for step in _steps(workflow["jobs"]["publish"])
                  if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")]
    assert len(publishers) == 2
    production = [step for step in publishers if "repository-url" not in step.get("with", {})]
    rehearsal = [step for step in publishers if "repository-url" in step.get("with", {})]
    assert len(production) == 1 and len(rehearsal) == 1
    assert "skip-existing" not in production[0]["with"]
    assert rehearsal[0]["with"]["skip-existing"] is True
    assert rehearsal[0]["with"]["repository-url"] == "https://test.pypi.org/legacy/"
    for step in publishers:
        # The action's own twine is 7.0.0, which accepts Metadata-Version 2.5, so its
        # metadata check and its attestations both stay on.
        assert step["with"].get("verify-metadata", True) is True
        assert step["with"].get("attestations", True) is True

    # Routing. Without both conditions one publisher runs unconditionally, so a rehearsal
    # tag would reach production -- and PyPI refuses a re-upload, so that is unrecoverable
    # for that version.
    assert "!startsWith(github.ref_name, 'test-')" in production[0]["if"]
    rehearsal_if = rehearsal[0]["if"]
    assert "startsWith(github.ref_name, 'test-')" in rehearsal_if
    assert "!startsWith" not in rehearsal_if


def test_the_release_toolchain_is_pinned(workflow):
    """Two builds on two days must not come from different toolchains."""
    versions = [step.get("with", {}).get("version")
                for job in workflow["jobs"].values() for step in _steps(job)
                if step.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert versions and all(v and re.fullmatch(r"\d+\.\d+\.\d+", str(v)) for v in versions)

    requires = tomllib.loads((REPO / "pyproject.toml").read_text())["build-system"]["requires"]
    assert all("==" in entry for entry in requires), requires


def test_the_build_job_can_read_this_commits_ci_result(workflow):
    """The release gate calls the Actions API; without this scope it cannot."""
    assert _permissions(workflow["jobs"]["build"]).get("actions") == "read"


# --- the one shell step in the publish job, actually executed --------------------------
#
# Everything else in that job is a pinned action. This block is the only code the release
# runs with the token in reach, and it is what binds the uploaded files to the digests the
# build recorded -- so it is executed here rather than pattern-matched.

VERSION = "9.9.9"
WHEEL = f"agent_tracegraph-{VERSION}-py3-none-any.whl"
SDIST = f"agent_tracegraph-{VERSION}.tar.gz"


def _manifest(files: dict[str, bytes]) -> str:
    """A manifest shaped like the job output: GitHub drops the final newline."""
    return "\n".join(f"{hashlib.sha256(payload).hexdigest()}  dist/{name}"
                      for name, payload in sorted(files.items()))


def _digest_step(workflow: dict) -> str:
    runs = [step["run"] for step in _steps(workflow["jobs"]["publish"]) if "run" in step]
    assert len(runs) == 1
    return runs[0]


def _run_digest_step(script: str, tmp_path: Path, files: dict[str, bytes],
                     sums: str | None = None, ref: str = f"v{VERSION}"):
    dist = tmp_path / "dist"
    dist.mkdir()
    for name, payload in files.items():
        (dist / name).write_bytes(payload)
    if sums is None:
        sums = _manifest(files)
    # A plain shell, deliberately: the block must fail closed on its own rather than
    # relying on the runner invoking it as `bash -e`.
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        cwd=tmp_path, capture_output=True, text=True, check=False,
        env={"PATH": os.environ["PATH"], "SUMS": sums, "GITHUB_REF_NAME": ref,
             "RUNNER_TEMP": str(tmp_path / "runner-temp")},
    )


@pytest.fixture(scope="module")
def digest_step(workflow) -> str:
    if shutil.which("sha256sum") is None:
        pytest.skip("needs GNU sha256sum, as on the ubuntu runner")
    return _digest_step(workflow)


@pytest.fixture
def runner_temp(tmp_path) -> Path:
    (tmp_path / "runner-temp").mkdir()
    return tmp_path


def test_the_expected_pair_passes(digest_step, runner_temp):
    result = _run_digest_step(digest_step, runner_temp,
                              {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"})
    assert result.returncode == 0, result.stderr


def test_a_changed_byte_is_caught(digest_step, runner_temp):
    """The whole point: what the build measured must be what the publisher uploads."""
    files = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"}
    sums = _manifest(files)
    files[WHEEL] = b"wheel bytez"
    result = _run_digest_step(digest_step, runner_temp, files, sums=sums)
    assert result.returncode != 0


def test_an_extra_file_is_caught(digest_step, runner_temp):
    """dist/ is handed to the publisher whole, so a third file would be uploaded too.

    The manifest stays the expected two lines, so the name comparison is satisfied and
    only the file count can catch this.
    """
    expected = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"}
    result = _run_digest_step(digest_step, runner_temp,
                              {**expected, "extra.whl": b"surprise"},
                              sums=_manifest(expected))
    assert result.returncode != 0


def test_an_extra_file_listed_in_the_manifest_is_also_caught(digest_step, runner_temp):
    """And the manifest may not simply grow to match what is in the directory."""
    files = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes", "extra.whl": b"surprise"}
    result = _run_digest_step(digest_step, runner_temp, files)
    assert result.returncode != 0


def test_a_missing_file_is_caught(digest_step, runner_temp):
    result = _run_digest_step(digest_step, runner_temp, {WHEEL: b"wheel bytes"})
    assert result.returncode != 0


def test_a_manifest_naming_something_outside_dist_is_caught(digest_step, runner_temp):
    """Two valid records for the wrong paths satisfy a count and a --check on their own."""
    files = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"}
    (runner_temp / "elsewhere.whl").write_bytes(b"wheel bytes")
    sums = (f"{hashlib.sha256(b'wheel bytes').hexdigest()}  elsewhere.whl\n"
            f"{hashlib.sha256(b'sdist bytes').hexdigest()}  dist/{SDIST}")
    result = _run_digest_step(digest_step, runner_temp, files, sums=sums)
    assert result.returncode != 0


def test_a_malformed_manifest_fails_closed(digest_step, runner_temp):
    """An unreadable manifest must stop the upload, not be skipped as unparseable."""
    files = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"}
    result = _run_digest_step(digest_step, runner_temp, files,
                              sums=_manifest(files) + "\n\nnot a checksum line")
    assert result.returncode != 0


def test_a_rehearsal_tag_expects_the_same_filenames(digest_step, runner_temp):
    """`test-v9.9.9` and `v9.9.9` package the same version; only the index differs."""
    result = _run_digest_step(digest_step, runner_temp,
                              {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"},
                              ref=f"test-v{VERSION}")
    assert result.returncode == 0, result.stderr
