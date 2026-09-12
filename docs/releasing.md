# Release runbook

Tracegraph publishes to PyPI through GitHub Actions and PyPI Trusted Publishing. No
long-lived PyPI token belongs in a repository or environment secret.

The distribution is **`agent-tracegraph`**; the import package and the console script are
both `tracegraph`. Version numbers are the same on the tag, in `pyproject.toml`, and in
`CHANGELOG.md`, and the release refuses to build when they disagree.

## What the workflow does

`release.yml` runs three jobs on a `v*` or `test-v*` tag.

- **`build`** checks that the tagged commit is on `tracegraph-mvp` and already has a green
  `tests` run, that the tag matches the packaged version, and that the changelog entry is
  dated. Then it builds, runs `twine check`, verifies the distributions, and records their
  SHA-256 digests. It holds no publishing permission.
- **`verify-wheel`** installs the *downloaded* artifact into fresh core and Cypher
  environments and runs `scripts/verify_wheel.py` from outside the checkout, with
  `--expected-version` bound to the tag. The bytes it exercises are the bytes that will be
  uploaded.
- **`publish`** holds `id-token: write`. It does not check out the repository, installs
  nothing, and runs one coreutils step that compares the downloaded files against the
  digests `build` recorded, before calling the pinned publisher.

Be precise about what the split buys. The publish job still runs code: the download and
the publisher are both actions. The publisher is pinned by commit, and the GitHub-owned
downloader by tag, following the repository's convention that only a third party can move
a tag unilaterally. The claim is narrower — **no checked-out project code and no
installed dependency runs in the job that can publish as this project.** The digest
comparison proves nothing tampered with the files between the jobs; it is not an
independent attestation of the build, because the same job produced both the artifacts
and the digests. A compromised build still publishes a compromised artifact.

`tests/test_release_workflow_shape.py` pins that boundary: publish uses only two
allowlisted actions, has no checkout, holds exactly one shell step with no `${{ }}` in it,
carries no `if:` or `continue-on-error` that could let a failed check through, and is the
only job that can mint a token.

### Why `twine check` is pinned to 7.0.0

The pinned build backend emits `Metadata-Version: 2.5`. **twine 6.2.0 rejects it**
(`'2.5' is not a valid metadata version`) and **twine 7.0.0 accepts it** — measured
against this project's own build on 2026-09-12. The publisher action runs `twine check`
inside its own container with twine 7.0.0 pinned, which is why `verify-metadata` stays at
its default. An unpinned `uvx twine` can resolve to either version, so both the workflow
and the local gate name `twine@7.0.0` explicitly. See `CONTRIBUTING.md`.

## One-time setup (owner)

1. Review the whole repository — history, issues, Actions logs and artifacts, settings —
   before changing visibility. See [the public release checklist](public-release-checklist.md).
2. Make `memtomem/tracegraph` public, then restore the push rulesets the visibility change
   disables. Set branch protection on `tracegraph-mvp` with the `tests` jobs required. The
   CLA check stays **visible but not required**, as in the sibling repositories.
3. Create the `pypi` GitHub environment with the owner as required reviewer, administrator
   bypass disabled, and selected **tag** patterns `v*` and `test-v*` (no deployment
   branches). Read the settings back: a workflow reference alone can create an unprotected
   environment. Required reviewers need a public repository on the current plan.
4. Create a pending Trusted Publisher on **TestPyPI**: repository `memtomem/tracegraph`,
   workflow `release.yml`, environment `pypi`, project `agent-tracegraph`.
5. Create the same pending publisher on **PyPI**. The two are separate services and both
   must be configured. A pending publisher does not reserve the name.

## Cutting a release

### 1. The release-prep commit

Bump `version` in `pyproject.toml`, run `uv lock` (the lockfile records the project
version, so `uv lock --check` fails without it), and move the changelog's `## Unreleased`
section under a dated `## <VERSION> - YYYY-MM-DD` heading.

PyPI refuses a version that already exists, so a released number is spent even if the
upload later fails verification. `pyproject.toml` embeds `README.md` as package metadata:
the release notes are frozen at tag time.

### 2. The local gate

Build into a fresh directory. A stale `dist/` from an earlier version is exactly what the
verifier refuses, and refusing it is the point.

```bash
cd /path/to/tracegraph
VERSION="$(uv run --no-sync python -c "import tomllib,pathlib; \
  print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")"
echo "releasing $VERSION"

uv lock --check
uv sync --locked --extra cypher
uvx ruff@0.14.2 check src tests scripts examples
uv run --no-sync pytest -q
rm -rf dist && uv build
uvx twine@7.0.0 check dist/*
uv run --no-sync python scripts/verify_artifacts.py dist "$VERSION"
```

This is a gate, not the release build: the artifacts that get published are built by the
workflow from the tagged commit.

### 3. Merge, then rehearse

Merge the release-prep PR and wait for `tests` to pass on `tracegraph-mvp`. The release
gate reads that result; it does not wait for a run in flight. If you tag before CI
finishes, the build job fails with an instruction to re-run it from the Actions UI once
`tests` is green — re-running re-evaluates the same commit, so nothing loosens.

**Tag the merged commit, not the branch you were on.** A squash or merge commit has a
different SHA than anything in your feature branch, and the gate refuses a commit that is
not an ancestor of the default branch:

```bash
git fetch origin
git checkout tracegraph-mvp && git pull --ff-only
RELEASE_SHA="$(git rev-parse HEAD)"
git tag "test-v$VERSION" "$RELEASE_SHA"
git push origin "test-v$VERSION"
```

The rehearsal publishes to TestPyPI with `skip-existing`, so it can be repeated.

**The rehearsal waits for the same approval as production.** The publish job names the
`pypi` environment whichever tag triggered it, so the run pauses until the environment's
required reviewer approves it. Approve the `test-v` run when the build and wheel jobs are
green; nothing has been uploaded at that point.

### 4. Verify what TestPyPI actually published

Not the workflow badge, and not a fresh `skip-existing` build — the published files.

Fetch them from the index's own record rather than with `pip download`. Verified
2026-09-12: `pip download --no-binary=:all:` *builds* the source distribution to read its
metadata, which installs build dependencies from whichever index was named — so against
TestPyPI it fails on a package that is not there. The JSON API also reports the digests
the index recorded, which is the comparison this step is for.

The comparison baseline is the **`test-v` build job's own `sha256sum dist/*` output**,
read from its log. The `dist/` from step 2 was built before the merge, so on a squash
merge it need not correspond to the released commit; rebuild locally from `$RELEASE_SHA`
if you want a second opinion.

Run from the repository checkout:

```bash
REPO="$PWD"
DL="$(mktemp -d)"
curl -sS "https://test.pypi.org/pypi/agent-tracegraph/$VERSION/json" \
  | python3 -c "
import json, sys, urllib.request, pathlib, hashlib
data = json.load(sys.stdin)
out = pathlib.Path(sys.argv[1])
for entry in data['urls']:
    blob = urllib.request.urlopen(entry['url']).read()
    digest = hashlib.sha256(blob).hexdigest()
    assert digest == entry['digests']['sha256'], entry['filename']
    (out / entry['filename']).write_bytes(blob)
    print(digest, entry['filename'])
" "$DL"

```

Compare those two lines with the `Record artifact digests` step in the `test-v` run's
build job log. They must match, filename for filename.

Then install into two fresh Python 3.12 environments **outside** the checkout, taking
dependencies from PyPI. Do not mix project selection across indexes
with `--extra-index-url`. These installs are **unlocked**, which is what a user gets and
what neither CI job exercises:

`uv venv --python 3.12` rather than `python3 -m venv`: the shell's `python3` is whatever
the machine has, and an older or newer interpreter tests a different runtime than the one
the classifiers claim.

```bash
WHEEL="$DL/agent_tracegraph-$VERSION-py3-none-any.whl"

# (a) core
uv venv --python 3.12 "$DL/core"
uv pip install --python "$DL/core/bin/python" --index-url https://pypi.org/simple/ "$WHEEL"
uv pip check --python "$DL/core/bin/python"
"$DL/core/bin/tracegraph" --version
"$DL/core/bin/python" "$REPO/scripts/verify_wheel.py" --extra core --expected-version "$VERSION"

# (b) cypher
uv venv --python 3.12 "$DL/cypher"
# Braced: in zsh, "$WHEEL[cypher]" is a subscript and expands to nothing.
uv pip install --python "$DL/cypher/bin/python" --index-url https://pypi.org/simple/ "${WHEEL}[cypher]"
uv pip check --python "$DL/cypher/bin/python"
"$DL/cypher/bin/python" "$REPO/scripts/verify_wheel.py" --extra cypher --expected-version "$VERSION"
```

`verify_wheel.py` runs on the fresh environment's interpreter but reads its fixtures from
the repository, which is why it is invoked by absolute path out of the checkout of the
tagged commit. Finally read the project page back — the rendered README, the project
links, and the publish attestations on both files.

### 5. Production

```bash
git tag "v$VERSION" "$RELEASE_SHA"
git push origin "v$VERSION"
```

That is the **same commit** as the rehearsal tag. While the publish job waits for
environment approval:

1. `git rev-parse "test-v$VERSION^{commit}" "v$VERSION^{commit}"` — the two must match.
2. Compare the `v<VERSION>` build job's printed `sha256sum dist/*` with the digests of the
   files downloaded from TestPyPI above. The same source commit does not by itself mean
   the same bytes, which is why the toolchain is pinned in `release.yml` and in
   `[build-system]`. Note that a re-run of the rehearsal prints digests for a freshly
   built file that `skip-existing` may not have uploaded: compare against the run that
   actually uploaded, or against the index, which is what the command above reads.

A mismatch, a missing file, or an unavailable index blocks approval. The production digest
does not exist until the production run is under way, so this comparison **cannot block
publication by itself** — the required reviewer is what turns it into a gate.

After the upload, re-run the download-and-compare block against
`https://pypi.org/pypi/agent-tracegraph/$VERSION/json`, repeat the clean installs, and
confirm the attestations. Then create the GitHub Release for `v<VERSION>`
with the prerelease flag on; the tag and the package version are the same number.

## When something goes wrong

- **Re-running a failed run is not reusing a tag.** If the run failed *before* anything
  was uploaded — the CI gate, the version or changelog check, the artifact verification,
  the wheel smoke — re-run that workflow run from the Actions UI once the cause is fixed
  outside the repository, or push a new commit and a new tag if the fix is a code change.
  The tag still points at the same commit and nothing has been published.
- **Do not move, delete or recreate a tag**, and do not reuse a version once anything
  reached an index. Fix forward with a new patch or prerelease version. PyPI refuses a
  re-upload, so the number is spent even if what landed was wrong.
- **A defective published release is yanked**, not deleted, and replaced by a new version.
- **An ambiguous upload outcome is unknown state.** A network error after the request was
  sent is not evidence that nothing was published: read the index back before deciding how
  to recover.
- A successful local build is not a publication, and a green workflow badge is not
  evidence about published bytes.
