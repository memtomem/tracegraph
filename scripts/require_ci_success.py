"""Refuse to build a release unless the tagged commit already passed CI on the default branch.

    GITHUB_REPOSITORY=owner/name GITHUB_SHA=<sha> GITHUB_TOKEN=<token> \
        python scripts/require_ci_success.py --workflow test.yml --branch tracegraph-mvp

Two questions, one answer each, no polling:

  1. Is this commit on the default branch? The compare API reports `identical` for the tip
     and `behind` for an ancestor; anything else means the tag names a commit that was
     never merged, which is the release mistake worth refusing.
  2. Did the test workflow already pass *for this exact commit*? A push run is required.
     A pull-request run of the same tree was green before the merge changed it.

Runs are matched on `head_sha` and `event`, never on `head_branch`: a tag push produces a
run whose `head_branch` is the tag name, alongside the branch push run for the same commit
(observed in this repository: the `v0.1.0` tag run and the `tracegraph-mvp` run share a
head SHA). A stale failed run next to a later success is fine -- re-running a flaky job is
the normal fix, and the question is whether this commit has ever passed, not whether it
ever failed.

If the answer is not yet yes, this fails and says so. The fix is to wait for the test
workflow and re-run this workflow run from the Actions UI; it re-evaluates the same
GITHUB_SHA, so nothing about the contract loosens. Deliberately not a poller: waiting
needs a timeout, an interval, a grace period for runs that have not registered yet, and a
transport-retry policy, all to save one click on the rare release where the branch and
the tag are pushed together.

Standard library only: this runs before anything is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TIMEOUT_SECONDS = 20.0

# The commit is on the default branch iff comparing branch...sha gives one of these.
# `identical` is the tip; `behind` means sha is an ancestor. `ahead` and `diverged` mean
# it is not.
ON_DEFAULT_BRANCH = ("identical", "behind")


class CIGateError(RuntimeError):
    pass


def fetch(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tracegraph-release-gate",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CIGateError(f"GitHub API returned {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise CIGateError(f"GitHub API request failed for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CIGateError(f"GitHub API returned a non-object payload for {url}")
    return payload


def check_on_default_branch(repository: str, branch: str, sha: str, fetcher) -> str:
    base = urllib.parse.quote(branch, safe="")
    head = urllib.parse.quote(sha, safe="")
    payload = fetcher(f"{API}/repos/{repository}/compare/{base}...{head}")
    status = payload.get("status")
    if status not in ON_DEFAULT_BRANCH:
        raise CIGateError(
            f"{sha} is not on {branch}: compare reports {status!r}. "
            f"Release only from a commit merged into {branch}."
        )
    return status


def check_workflow_succeeded(repository: str, workflow: str, sha: str, fetcher) -> int:
    query = urllib.parse.urlencode({"head_sha": sha, "event": "push", "per_page": 100})
    payload = fetcher(
        f"{API}/repos/{repository}/actions/workflows/"
        f"{urllib.parse.quote(workflow, safe='')}/runs?{query}"
    )
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise CIGateError("GitHub workflow-runs response has no workflow_runs list")
    # Re-filter client side: the query parameters are a request, not a guarantee.
    matching = [
        run for run in runs
        if isinstance(run, dict) and run.get("head_sha") == sha and run.get("event") == "push"
    ]
    for run in matching:
        if run.get("status") == "completed" and run.get("conclusion") == "success":
            return int(run.get("id", 0))
    total = payload.get("total_count")
    if isinstance(total, int) and total > len(runs):
        # Never claim "it has not passed" about runs that were not read. A commit with
        # more runs than one page is not something this repository produces, but the
        # difference between "no success" and "no evidence" is the whole point of a gate.
        raise CIGateError(
            f"read only {len(runs)} of {total} runs for {sha}; "
            f"cannot conclude whether {workflow} passed."
        )
    if not matching:
        found = "no push runs of that workflow exist for this commit yet"
    else:
        found = "found " + ", ".join(
            f"run {run.get('id')} ({run.get('status')}/{run.get('conclusion')})"
            for run in matching
        )
    raise CIGateError(
        f"{workflow} has not passed for {sha}: {found}. "
        f"Wait for it to pass on this commit, then re-run this workflow run "
        f"from the Actions UI."
    )


def gate(repository: str, branch: str, workflow: str, sha: str, fetcher) -> dict:
    status = check_on_default_branch(repository, branch, sha, fetcher)
    run_id = check_workflow_succeeded(repository, workflow, sha, fetcher)
    return {"compare_status": status, "run_id": run_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workflow", default="test.yml",
                        help="workflow file name whose run must have succeeded")
    parser.add_argument("--branch", default="tracegraph-mvp",
                        help="branch the tagged commit must belong to")
    args = parser.parse_args(argv)

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    for name, value in (("GITHUB_REPOSITORY", repository), ("GITHUB_SHA", sha),
                        ("GITHUB_TOKEN", token)):
        if not value:
            print(f"{name} is required", file=sys.stderr)
            return 1

    try:
        result = gate(repository, args.branch, args.workflow, sha,
                      lambda url: fetch(url, token))
    except CIGateError as exc:
        print(f"Release gate failed: {exc}", file=sys.stderr)
        return 1
    print(f"{sha} is {result['compare_status']} relative to {args.branch}; "
          f"{args.workflow} run {result['run_id']} succeeded for it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
