# Phoenix + SyncMill operational E2E

The `Phoenix + SyncMill E2E` workflow is intentionally separate from pull-request CI. It runs
every Monday and on manual dispatch against the Phoenix Docker digest and `px` version pinned in
`contracts/phoenix-runtime.json`.

The scheduled run resolves SyncMill `main` and records the exact commit SHA. A manual run may
select a branch, tag, or commit with the `syncmill_ref` input. This allows a coordinated SyncMill
producer change to be tested before its default branch is updated.

The workflow proves the following real boundaries:

1. A controlled SyncMill `AgentRunner` produces a successful baseline and a failed explicit
   retry through the normal Supervisor and trace exporter.
2. The exporter sends the body-free spans to a pinned, loopback-only Phoenix server.
3. The pinned `px` CLI can read both traces, and zero-id diagnosis selects the failed trace and
   reports `tool-retry-failure@v2`.
4. Explicit baseline diagnosis and local full-fidelity `diff` report the expected change.
5. The local artifact exports one version-2 review candidate. SyncMill imports it once, skips it
   on repetition, and keeps the item human-required.

Only the body-free summary, normalized artifacts, analysis reports, and candidate report are
retained for 14 days. The temporary Phoenix database, raw API responses, local OTLP files,
workspace snapshots, and board database are destroyed with the runner.

For a local run, install Tracegraph, install SyncMill with its `phoenix` extra, install the pinned
`px`, start the exact Docker image on a random loopback port, and invoke:

```bash
python scripts/verify_phoenix_syncmill_e2e.py \
  --endpoint http://127.0.0.1:PORT \
  --syncmill-dir ../syncmill \
  --syncmill-sha "$(git -C ../syncmill rev-parse HEAD)" \
  --px-version "$(jq -r .phoenix_cli contracts/phoenix-runtime.json)" \
  --work-dir /tmp/tracegraph-phoenix-e2e
```

Use a fresh `HOME`/XDG directory for local contract testing so `px profile create --activate`
cannot alter an operator's normal Phoenix profile.
