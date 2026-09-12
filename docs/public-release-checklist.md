# Public release checklist

Readiness for making `memtomem/tracegraph` public and publishing `agent-tracegraph`.
Use [the release runbook](releasing.md) for the commands; this file is what to check
before those commands can be run at all.

## Two facts that govern everything below

1. **Deleting a file does not remove it.** Unless history is rewritten, any blob still
   reachable — from a branch, tag, PR ref, or unchanged history — stays readable with
   `git show <commit>:<path>` after the repository is public. A new commit that removes
   something only changes the branch tip and the next source distribution.
2. **Open branches carry their own copies.** A file removed on the default branch is still
   at the tip of every other open branch until those are merged or deleted.

So any claim that something was "made private" by deleting or moving it is false. Decide
what stays, and rewrite history or do not publish, rather than deleting and hoping.

## Before visibility changes

1. Record the reviewed SHA and the remote refs. Inspect reachable Git objects, issue and
   PR text and their edit histories, attachments, Actions logs and artifacts, and
   repository settings. Record what was covered and what could not be (expired artifacts
   are *unavailable*, not *clean*). A pattern scan cannot establish that no secret exists.
2. Classify before deleting. Synthetic redaction fixtures and illustrative paths are
   evidence and should stay. Workstation paths in `docs/reviews/*`, `docs/HANDOFF.md` and
   the review logs are the ones to scan for — they name a developer's home directory, not
   a secret, but they are noise in a public repository.
3. A newly discovered credential is resolved before the flip, never published and rotated
   afterwards.
4. Check collaborators and teams, deploy keys, webhooks, installed Apps, default workflow
   permissions, environments and secrets metadata, and whether Wiki/Pages/Discussions are
   enabled.
5. **The SyncMill end-to-end workflow checks out a private repository** with
   `SYNCMILL_REPO_TOKEN`. Confirm it stays `workflow_dispatch` and `schedule` only, so a
   fork pull request cannot reach that token, and check the fork-PR workflow approval
   setting.
6. Check both package indexes again. A 404 from the JSON or simple API means no published
   project was found; it does not reserve a name, and neither does a pending Trusted
   Publisher.
7. Confirm the owner has authorized the transition. Read the new visibility back, restore
   any push rulesets the flip disabled, and configure branch protection on
   `tracegraph-mvp` with the `tests` jobs required. Reuse the existing CI success; a
   visibility change does not require a dummy commit.
8. Enable secret scanning and push protection, dependency alerts, and private
   vulnerability reporting. Verify the reporting form resolves — the email in
   `SECURITY.md` is the documented fallback.

## Before the first tag

- Create and read back the `pypi` environment, its required reviewer, and its release-tag
  restrictions. A workflow reference alone can create an *unprotected* environment, which
  is not evidence of enforcement.
- Configure independent pending Trusted Publishers on **TestPyPI and PyPI** for
  `memtomem/tracegraph`, workflow `release.yml`, environment `pypi`, project
  `agent-tracegraph`.
- Add the `PERSONAL_ACCESS_TOKEN` secret the CLA workflow needs (fine-grained, this
  repository only: Contents read/write for the signature branch, Issues read/write for PR
  comments, Pull requests read for the contributor listing).
- Seed the signature store: `cla-check.py` writes to a `cla-signatures` branch and has no
  branch-creation path, so create that orphan branch first, containing only
  `signatures/v1/cla.json` = `{"signedContributors": []}`.
- Inspect the actual wheel and source distribution once more. The sdist deliberately
  excludes tests, docs, CI configuration and the lockfile: it is a build input for the
  wheel, not a source checkout for downstream test execution. Count the entries in the
  current artifact rather than reusing an earlier count.
- Then follow the runbook: rehearse on TestPyPI, compare published hashes against the
  production candidate before approving the environment, publish, and create the GitHub
  Release.

## Completion record

Record the source and tag SHAs, the workflow run IDs, the index metadata, the distribution
digests from both indexes, the clean-install results for core and Cypher, and the final
visibility, security and environment settings.

If any required check is unavailable or fails, report **HOLD** with that reason rather
than reporting release success. Do not rewrite tags or reuse a published version to
correct package metadata.
