# Contributing to Tracegraph

Thank you for your interest in contributing to Tracegraph!

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). It applies to issues, pull
requests, discussions and code review alike.

Tracegraph is a **read-path analysis layer**: it reads LangGraph checkpoints and span
exports and explains what happened. It is not a checkpointer, not a tracer, and not in any
request path. Changes that would have it write to a checkpointer, instrument a running
graph, or sit in the execution path are out of scope.

## Development setup

```bash
git clone https://github.com/memtomem/tracegraph.git
cd tracegraph

# Requires Python 3.12+ and uv
uv sync --locked

# Tests. `--no-sync` keeps you on exactly the locked environment.
uv run --no-sync pytest -m "not perf"     # the default gate
uv run --no-sync pytest                   # includes wall-clock perf guards

# Lint — the same pinned version CI runs
uvx ruff@0.14.2 check src tests scripts examples

# Optional Cypher accelerator (LadybugDB)
uv sync --locked --extra cypher
uv run --no-sync --extra cypher pytest -m "not perf"
```

`uv sync --locked` plus `uv run --no-sync` is deliberate: it fails on lockfile drift instead
of silently resolving something different from CI.

**Pin `twine` to 7.0.0 when you run it.** The pinned backend emits
`Metadata-Version: 2.5`. twine 6.2.0 rejects that outright:

```
InvalidDistribution: Invalid distribution metadata: '2.5' is not a valid metadata version
```

twine **7.0.0 accepts it** — measured against this project's own build. So the rejection was
the linter trailing the spec, and it is fixed upstream; `uvx twine@7.0.0 check dist/*` is a
real gate again and both CI and the release workflow run it. Do not drop the version pin:
an unpinned `uvx twine` can still resolve to 6.x. The publishing action runs twine 7.0.0
inside its own container for the same reason.

Releases are tag-driven and documented in the [release runbook](docs/releasing.md).

Lint is **not** a style gate. The rule set is narrow (`F,E4,E7,E9,B`) and aimed at real
defects — undefined names, unused imports, shadowed builtins. `UP` is excluded because
`UP042` would rewrite the `str`+`Enum` models. Please do not widen it as a drive-by change.

## Pull requests

1. Branch from `tracegraph-mvp` (this repository's default branch).
2. Keep changes focused — one feature or fix per PR.
3. Add tests. See the testing notes below for what kind.
4. Ensure lint and `uv run --no-sync pytest -m "not perf"` pass.
5. Write a commit message explaining the **why**, not the diff.
6. Sign the CLA on your first pull request (see below).

## What this project treats as a defect

The product is a claim about causality, so the bar is narrower than "the tests pass":

- **Never assert something the data cannot support.** A metric derived from partial input is
  withheld with a stated reason, not published with a caveat. "No observed error" is never
  reported as "the run succeeded".
- **Never fabricate a causal edge.** Every `CAUSED_BY` edge must come from something the
  source system declared — a parent pointer, a task id, a span link — never from timing,
  ordering, or name similarity.
- **Never blame by proximity.** If the evidence does not identify which node did something,
  the field stays empty. An unnamed finding is correct; a confidently wrong name is not.
- **Degrade to slower, never to wrong.** The Cypher accelerator must produce results
  identical to the pure-Python matcher, and falls back when it cannot.

A change that improves coverage by weakening one of these is not an improvement.

## Testing notes

- **Drive the real producer.** Adapter behavior is pinned against real LangGraph runs and
  real savers, not hand-built fixtures. A synthetic fixture cannot show that a reconstruction
  is correct, because getting it wrong is exactly what produces a plausible-looking fixture.
  Stub checkpoints are for shapes a real saver cannot be asked for on demand — a legacy
  checkpoint version, a colliding id.
- **`contracts/` is consumed by other repositories** (SyncMill, Toolgraph). A test that only
  reads a fixture does not catch producer drift; drive the producer. The
  review-candidate golden fixture is compared **byte for byte**, and the artifact envelope is
  inside the digest consumers bind to, so changing either is a contract change.
- **Assert negative outcomes too** — a query that must *not* match, a command that must exit
  non-zero. Several regressions have been happy-path-only tests.

## Contributor License Agreement (CLA)

Before your first pull request can be merged you need to sign the
[Contributor License Agreement](CLA.md). A workflow comments on your PR with instructions;
you sign by replying with:

> I have read the CLA Document and I hereby sign the CLA

Your signature is stored in `signatures/v1/cla.json` on this repository's `cla-signatures`
branch. Signing is one-time per GitHub account **per repository** — Tracegraph, Toolgraph and
memtomem keep independent signature stores.

The CLA is the Apache Software Foundation Individual CLA with one added section covering
future licensing rights, which preserves DAPADA Inc.'s ability to adopt different terms later
— including copyleft, source-available, or proprietary terms — without re-collecting consent.
It does not change the current license, which is Apache-2.0.

### How the check actually behaves

Worth stating precisely, because the mechanics are counter-intuitive:

- The workflow triggers on `pull_request_target`, which runs the **default branch's** copy of
  the workflow and checks out the default branch — never your PR's code. That is deliberate:
  the job holds a token, so it must not execute code from a fork. The resulting `CLAAssistant`
  check is nonetheless associated with your pull request, and it is **red** while a signature
  is missing.
- Signing is a *comment* event, which GitHub records against the default branch rather than
  your PR. So signing does **not** by itself clear the red check on your PR. Verified against
  this project's sibling repositories: a signed contributor's check was still red at merge.

**Comments are evidence, not a verdict.** The checker never retracts an earlier
"All contributors have signed" comment, so if a later commit adds an unsigned author, that
stale success comment sits alongside the new request. Do not read the newest comment, or the
presence of a success comment, as current approval.

What is authoritative is a **fresh evaluation against the current head**: re-run the
`CLA Assistant` workflow for the PR (or push a commit, which triggers it) and read that run's
result. It re-reads the signature store and re-derives the contributor set every time.

The CLA is deliberately **not** a required status check here, matching the sibling
repositories. It informs the merge decision rather than blocking it mechanically — which is
exactly why the maintainer checks above are not optional.

### What the check does not cover

Even a fresh green evaluation is not proof every contributor signed. The shared checker skips
commits whose author email GitHub cannot resolve to an account, never inspects
`Co-authored-by:` trailers, and matches signatures by **login** even though it stores account
ids — so a signature does not follow a contributor who renames their account, and a login
later reused by a different account would be treated as already signed. These are upstream
gaps in a script three repositories share byte-identically, documented here rather than
forked.

Before merging, a maintainer confirms that every commit author on the PR resolves to a GitHub
login and that any co-author named in a trailer has signed. If your commits' author email does
not resolve, add it to your GitHub account so the check can see you.

For questions about the CLA, contact contact@dapada.co.kr.

## Reporting issues

Open an issue at https://github.com/memtomem/tracegraph/issues with steps to reproduce,
expected versus actual behavior, and your environment (OS, Python version, tracegraph
version). For security issues see [SECURITY.md](SECURITY.md) — please do not open a public
issue.
