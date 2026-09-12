# Changelog

All notable changes to Tracegraph are documented here.

The **artifact** schema version and the **report**/**review-candidate** JSON schemas are
separate contracts from this version number; changes to any of them are called out
explicitly.

## Unreleased

### Fixed

- The CLA workflow refused every pull request from the maintainer's own commit-author
  account, which is not the account that owns the repository. Both are allowlisted now.
- The release documentation asked for a personal access token with *read* access to pull
  requests. Posting the CLA comment needs *write*: the call is to `/issues/{n}/comments`,
  and GitHub enforces the Pull requests permission when that number is a pull request.
  It also did not say that a Trusted Publisher's "Environment name" is the GitHub
  environment rather than the index, which failed the first 0.2.0 rehearsal.

## 0.2.0 - 2026-09-12

First published release. `0.1.0` was tagged but never uploaded.

### Fixed

- **A LangGraph node that raised an exception was reported as a successful run**
  (`status=ok`, `error_count=0`). LangGraph does not route such a failure through a state
  channel: it records it in the checkpoint's pending writes and writes no further checkpoint,
  so reading channel values alone could not see it and no `--error-channel` value recovered
  it. Each failed task now becomes its own step, hanging off the checkpoint that scheduled it
  rather than marking that checkpoint — which is named after the node that *produced* it, so
  marking it reported the wrong node.
- A task cancelled because a *sibling* failed is no longer counted as a second failure.
  LangGraph records sibling cancellation through the same `__error__` channel as a genuine
  fault; those tasks are reported as `unset` with no message, so one fault stays one failure
  and a healthy node is never a primary candidate.

### Security

- **Reading a checkpoint database no longer executes what it contains.** LangGraph's
  deserializer revives stored objects by importing a module and calling a name, both taken
  from the payload, and by default an unrecognized target is logged and then called — so a
  crafted checkpoint naming `os.system` or `subprocess.run` ran on the machine doing the
  analysis (both verified against the pinned dependency). `tracegraph ingest` now passes a
  serializer with an empty allowlist.

  **Behaviour change:** objects outside the framework's registered types no longer revive
  through `ingest`; they come back as their raw arguments. If you were relying on a custom
  class being reconstructed from checkpoint state, it will now surface as plain data.
  Constructing `LangGraphCheckpointAdapter` yourself is unaffected — the serializer is
  whichever one your saver carries.

### Added

- The failing node's name, recovered by recomputing LangGraph's task id from checkpoint data.
  The match is self-verifying, so a task nothing identifies keeps no name rather than
  borrowing a neighbour's.
- `StepSource.TASK`, marking a step derived from pending writes rather than from a checkpoint.
- `RawTrace.ingest_warnings`, printed by `ingest` to stderr — disclosures about the *read*
  (for example `Send` packets that could not be decoded), which are not part of the run and so
  never enter the artifact.
- First public release metadata: Apache-2.0 `LICENSE`, `CLA.md`, `CODE_OF_CONDUCT.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, and PyPI classifiers, keywords, authors and project URLs.
- `tracegraph --version` (`-V`), which prints the bare version and nothing else. There was no
  way to ask an installed build what it was.
- An sdist allowlist. Previously the source distribution shipped everything git tracked —
  tests, docs, CI workflows, the lockfile and the checkpoint fixtures.
- A tag-triggered release workflow publishing through PyPI Trusted Publishing: `test-v*`
  rehearses against TestPyPI, `v*` publishes to PyPI. Only the publishing job holds an OIDC
  token, and it checks out nothing, installs nothing, and uploads only files whose digests
  match what the build job recorded. Before any of that, the build refuses a tag whose
  commit is not on the default branch, has no green `tests` run of its own, disagrees with
  the packaged version, or has no dated changelog entry.
- `scripts/verify_artifacts.py`, which checks that the built distributions are exactly the
  two files that will be uploaded and that the source distribution carries only what the
  packaging allowlist intends. It also refuses tests, docs, CI configuration and the
  lockfile outright, so widening that allowlist cannot quietly start shipping them, and
  refuses any archive member whose path does not go where it reads — a `..` segment is
  what would let a member satisfy both lists and still land somewhere else, or outside
  the archive entirely, when someone extracts it.
- `scripts/require_ci_success.py`, the release gate described above.
- A [release runbook](docs/releasing.md) and a
  [public release checklist](docs/public-release-checklist.md).
- The installed-wheel CI jobs now pin the version they expect and run the artifact
  verifier, so the release checks are exercised on every pull request rather than first at
  tag time.

### Changed

- **The distribution is now `agent-tracegraph`.** The plain `tracegraph` name on PyPI belongs
  to an unrelated project. The import package, the console script, the schema `kind`
  constants and the `TRACEGRAPH_*` environment variables are **unchanged** — only the name
  you install differs. Installing from source or from a checkout is unaffected.
- The build backend version is pinned (`hatchling==1.32.0`), so a release rehearsal and the
  production tag cannot run *different backend versions* against the same commit. This is
  drift control, not reproducibility: hatchling's own dependencies still float. The uv
  version used to build is pinned in the same way.
- `twine check` is a gate again, pinned to `twine@7.0.0`. It was disabled because twine 6
  rejected the `Metadata-Version: 2.5` this backend emits; twine 7 accepts it.
- **Artifact schema version 3**, stamped *by content*: an artifact carrying derived task steps
  declares 3 so an older reader refuses it by name instead of failing on an enum, while
  artifacts without them still serialize as version 2 byte for byte. The envelope is inside
  the digest `export-review-candidates` binds to, so a blanket bump would have invalidated
  digests already published for unchanged artifacts.
- The LangGraph capture disclosure in every report now names both the error channel and task
  pending writes. It remains unconditional: an artifact ingested by an older build, or one
  using an unrecognized checkpoint layout, carries no derived step and is indistinguishable
  from a genuinely clean run.
- `xxhash` is now a direct runtime dependency (LangGraph hashes task ids with it, but
  `langgraph` itself is only a development dependency here).
- `langgraph-checkpoint>=4.1` is now declared directly rather than left transitive, because
  ingestion calls its serializer API. `allowed_msgpack_modules` does not exist before 4.1
  (4.0.0 raises `TypeError`), so the floor is what makes the hardening above guaranteed
  rather than incidental.

### Known limitations

- Tasks that were *scheduled but never committed* — a run paused by `interrupt()`, stopped by
  the recursion limit, or killed — are not reported. Trace `status` still means "did a step
  error", so such a run reports `ok`.
- Only the latest persisted error survives per task, so repeated attempts are not
  reconstructed.
- `Send` packets can only be named where `langgraph.types` is importable; an ordinary install
  has only the `langgraph.checkpoint` half of that namespace, and the gap is disclosed.

## 0.1.0 - 2026-05-30

Tagged but never published. Initial read-path causal-graph analysis of LangGraph agent traces.
