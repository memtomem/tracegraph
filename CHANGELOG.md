# Changelog

All notable changes to Tracegraph are documented here.

The **artifact** schema version and the **report**/**review-candidate** JSON schemas are
separate contracts from this version number; changes to any of them are called out
explicitly.

## Unreleased

### Added

- **Guided investigation after `analyze` and `phoenix diagnose`** (#58): displayed failure
  candidates include full step IDs, with POSIX-quoted `explain` commands for existing artifacts
  or explicitly labelled save/replay templates for exports, stdin, and unsaved Phoenix traces.
  Evidence IDs, paths and commands preserve literal emoji-like tokens and are not forcibly
  line-wrapped; consumed pipe inputs and process-bound `/dev/` or `/proc/` paths require
  replay, including regular-file stdin redirects. Nonprinting arguments are shown as
  escaped data instead of copyable commands. Hidden
  candidates show the required `--limit`; report paths are acknowledged only after successful
  writes, without duplicate report-path notices. Existing edge-origin/fidelity warnings and
  missing-telemetry disclosures continue to distinguish evidence from causal claims; this
  change adds navigation, not new causal inference. Analysis JSON schemas, exit codes, and
  artifact privacy boundaries are unchanged.
- **`ingest --graph` reports LangGraph tasks that were scheduled but recorded nothing.**
  Given the compiled graph (`path/to/file.py:attr` or `pkg.module:attr`), LangGraph's
  `get_state` lists the tasks it schedules at the thread's newest root checkpoint, and each one
  with no write there becomes an `unset` step with the new source value `scheduled`, linked by
  `CAUSED_BY` to that checkpoint: the recursion limit, a killed process, a static breakpoint,
  one step per `Send` packet. Finished, failed and paused tasks are still read from the
  checkpoint's writes, so these steps never duplicate `task` or `interrupt` steps. The result
  is LangGraph's answer for the supplied graph; the trace header records its structural
  fingerprint as `schedule_graph`, also when nothing is scheduled. An unapplied
  `Command(goto=...)` input and `Send` packets to a node the graph lacks are disclosed at
  ingest. `--graph` imports the module and runs the graph's state callbacks while reading,
  but no node. `inspect` marks such a step `◌ scheduled` and adds `· N scheduled` to the
  summary.
- **Artifact schema version 6**, stamped by content: an artifact read with `--graph` (it has
  `schedule_graph`, or a `scheduled` step) declares 6, and 0.4.x refuses it by name. Every
  artifact ingested without `--graph` keeps its version and its bytes; the header field is
  omitted, not written as `null`.
- **A hands-on getting-started notebook in Korean**, `examples/getting_started_ko.ipynb`, for
  first-time users working from a repository checkout. It runs the example agent and every
  core command offline, interprets each output, and works through two applied scenarios:
  checking a change against a baseline, and aggregating failures across many runs from
  `analyze --json-out` reports. Its outputs are committed, and `tests/test_notebooks.py`
  executes it so its commands and exit codes cannot drift from the CLI. `nbclient` and
  `ipykernel` join the development dependencies. Nothing ships in the wheel or sdist, and no
  artifact or report contract changes.

### Changed

- `inspect` marks a step paused by `interrupt()` as `⏸ interrupted`, in yellow rather than
  the green used for finished steps, and the summary line gains `· N interrupted` when there
  is one. The output for every trace without interrupt steps is unchanged.

## 0.4.0 - 2026-10-14

### Added

- **LangGraph tasks paused by `interrupt()` are reported.** A task whose only write on the
  thread's newest root checkpoint is `__interrupt__` — it paused and the history holds no
  resume and no result — becomes an `unset` step with the new source value `interrupt`,
  linked by `CAUSED_BY` to that checkpoint and named by the same task-id reconstruction as
  failed tasks. The step describes the recorded history, not whether LangGraph would resume
  the task now. An interrupt recorded beside a resume or an error and no result is ambiguous
  in the checkpoint (under LangGraph 1.2.2 a resumed task that wrote nothing looks exactly
  like one paused twice; 1.2.15 marks it finished), so it is disclosed at ingest instead. Trace `status` is unchanged: a pause is not an error.
- **Artifact schema version 5**, stamped by content: only an artifact carrying an
  `interrupt` step declares it, so every other artifact serializes byte for byte as before.
  0.3.1 and earlier refuse a version 5 artifact by name; this build reads versions 1 to 5,
  and still rejects an envelope that declares less than its content needs.

### Known limitations

- Tasks scheduled but never started — the recursion limit, a killed process, an
  `interrupt_before`/`interrupt_after` breakpoint — leave no write and are still not
  reported. Telling them apart from finished work needs the compiled graph.
- An interrupt inside a subgraph is reported as the subgraph's node at the root, not as the
  node inside the subgraph that paused.

## 0.3.1 - 2026-10-13

### Fixed

- **An unreadable cost no longer understates the run's total.** The importers dropped a
  `llm.cost.total` that was not a finite non-negative decimal (`"abc"`, `-1`, a boolean), so
  `analyze` summed the remaining costs and printed a total that was knowably too low, with no
  warning. The documented rule that a single unparseable cost withholds the whole total only
  applied to artifacts built by hand. An unreadable total cost is now stored as the fixed
  marker `"unreadable"`, never the producer's value, and the report withholds `total_cost`
  and its delta with the existing note.
- Token counts, prompt and completion costs, cost currencies and Phoenix evaluation scores
  that cannot be read were dropped without a trace. They are still reported as unavailable,
  and `ingest-otlp`, `ingest-phoenix`, `analyze` and `phoenix diagnose` now say so on stderr,
  naming the kind of value and how many spans (for scores, evaluations) carried it, never
  the value itself. A boolean score still reads as `1.0` or `0.0`, as before.
- An OTLP `intValue` sent as a fractional JSON number was truncated before validation:
  `1.5` became `1` and `-0.5` became `0`, so an invalid token count looked valid. It is now
  validated as sent.
- **A checkpoint history LangGraph could not have written is a usage error, not a
  traceback or a different trace.** About half of 64 one-field mutations of a real history
  (a non-object `metadata` or `channel_values`, a numeric `checkpoint_ns`, a boolean `v`, an
  unhashable parent checkpoint id, ...) crashed `ingest` with a traceback, and several were
  read silently as something else: a malformed parent config or `metadata.parents` dropped
  the declared parent and made the checkpoint a root, and `metadata.step` of `true` or `1.5`
  was read as step 1. Each checkpoint is now checked against the types LangGraph declares
  for the fields tracegraph reads, strictly, and rejected with a message naming the field.
  A config `checkpoint_id` that disagrees with the checkpoint's own id is rejected too.
- A checkpoint row the saver cannot decode (an unknown serialization type, metadata nested
  past the recursion limit, a non-string metadata column) crashed `ingest` and `discover`
  with a traceback, except that `discover` reported an unknown serialization type as "does
  not support listing all threads". Both now report the history as undecodable.
- Pending writes that are not a `(task id, channel, value)` triple were skipped without a
  word, and any 3-character string was unpacked as one (`"abc"` became task `a`). They are
  still ignored, but `ingest` now says how many on stderr, since one could have carried a
  task failure.

### Compatibility notes for imports

- An artifact imported from a span with an unreadable total cost now carries
  `"total_cost": "unreadable"` instead of no cost, and its reports withhold the run's
  total cost. No schema change: 0.3.0 reads such an artifact and withholds the same way.
- Because `intValue` is no longer truncated, `llm.cost.total = {"intValue": 1.5}` is stored as
  `"1.5"` instead of `"1"`, and a `toolgraph.graph_generation` of `{"intValue": 1.5}` is
  rejected like any other invalid generation instead of being read as `1`.
- The committed Phoenix fixture carries an unreadable `llm.cost.prompt`; reading it now prints
  one stderr disclosure. Every committed fixture's artifact, report, memo, candidate file and
  CLI stdout is unchanged.
- LangGraph histories that `ingest` used to accept with a malformed field listed above are
  now refused. Histories written by LangGraph's own savers are unaffected: the checks cover
  only fields tracegraph reads (`counters_since_delta_snapshot`, which savers return as
  lists although it is declared as tuples, is not checked), a missing `updated_channels`
  and a legacy `pending_sends` are accepted, and an unknown `metadata.source` string still
  reads as a loop. The committed LangGraph fixture is byte-identical.

## 0.3.0 - 2026-10-13

### Fixed

- An artifact whose content was not canonical (for example a stored trace status that
  disagreed with its steps) was stamped `schema_version` 4 even when it carried no edge
  origins. Version 4 is now stamped only when the file's TREE_PARENT edges are the
  origin-priority projection of its causal edges and that differs from the temporal one; the
  rest of the trace does not affect the stamp. **Digest impact:** canonical artifacts are
  unchanged, but re-serializing a non-canonical trace (one `validate` rejects) whose tree is
  not the origin-priority one, such as a trace with its derived edges missing, now writes
  version 2 or 3 instead of 4. Any digest computed from that re-serialization changes,
  including the `artifact_digest` that `analyze` reports for such a trace.
- Loading now rejects an envelope that understates its content: a `schema_version` 2 file
  holding an origin-priority projection or derived task steps. Every reader that trusts the
  envelope would have read such a file under the wrong rules, and a re-save rewrote it as a
  different file. An envelope may still overstate what its content needs.
- `schema_version` must be an integer. `true`, `1.0`, `2.0` and `4.0` were accepted because
  they compare equal, and `true` was migrated as a version 1 file.
- Evidence floats (`duration_ms`, evaluation `score`) must be finite, and the writer refuses
  to emit `NaN`/`Infinity`, which are not JSON.
- `--backend ladybug` crashed with a raw traceback on a step `seq` outside the INT64 range
  the backend stores. It now exits with a usage error naming the limit.
- `InMemoryStore.upsert_edges` accepted a `CAUSED_BY`/`TREE_PARENT` edge whose endpoint was
  not a stored step, or a `BELONGS_TO` edge pointing at another trace; the failure surfaced
  later as a `KeyError` from `ancestors`. It now raises `ValueError` and inserts nothing from
  that batch, matching the LadybugDB store. `WROTE`/`READ` channel edges are unchecked as
  before, and node upsert still replaces by ID.
- OTLP and Phoenix imports crashed with a raw traceback on a timestamp of `Infinity`, `NaN`
  or a number beyond the representable date range, on a `spanId`/`parentSpanId` given as a
  list, and on Phoenix `annotations` that were not an array. A timestamp that cannot be read
  — including garbage text, which was silently dropped before — is now reported as
  unavailable and disclosed on stderr by `ingest-otlp`, `ingest-phoenix`, `analyze` and
  `phoenix diagnose`. Malformed ids and annotations are rejected with a usage error.
- An OTLP `status.code` of `true` was read as OK because `true == 1`, and `false` as UNSET
  because `false == 0`; both are now rejected, for OTLP and Phoenix imports alike.
- LangGraph: two derived task steps could receive the same id (checkpoint `A` with task
  `x#task:y`, checkpoint `A#task:x` with task `y`); the second silently replaced the first
  and gained a second cause it never had. The collision is now rejected like a collision
  with a checkpoint.
- LangGraph: a failed task that no candidate node reproduces is still reported unnamed, and
  the read now says so on stderr. An unnamed failure is also what a change in LangGraph's
  task-id algorithm would look like, so it is no longer silent.

- `diff` and `validate` printed producer text through Rich markup: a step named
  `tool[bold]` showed as `tool`, and an invalid edge naming `ghost[red]X[/red]` showed as
  `ghostX`. Both now print the text literally, as `inspect`, `explain` and `query` already
  did. Output paths are printed literally too: an `--out` path containing `[/]` used to
  raise a markup error after the file had been written, so the command exited 1 for work
  it had completed.
- `diff --structure` paired any two leftover subtrees because every label is empty under
  that option, then reported them under an empty path (`only in B under  > : subtree ·`).
  An empty label is no longer treated as evidence of correspondence, and the reported
  subtrees now show step names so they can be found, while the comparison still ignores
  them. The identical/not-identical verdict and exit code are unchanged.
- `explain` and `query --explain` labeled a nameless step by its `source`, which for
  OTLP and Phoenix spans is always `loop` and for LangGraph is checkpoint metadata
  (`input`, `loop`), not what the step was. They now use the step kind, like every other
  view.
- `export-incident-memo` wrote `links_preserved` as `true`/`false`/`null` in its front
  matter but as `True`/`False`/`None` in its summary. Both now use the front-matter form.
- The Phoenix CLI version-floor message is derived from the floor constant instead of
  repeating it.

- `analyze --baseline` published token deltas, and cost deltas, from totals that covered
  only some steps on one side: with tokens on 3 of 3 steps in the baseline and 1 of 3 in
  the current run it reported `-200`, although the silent steps could have used any amount.
  Like the duration delta, these deltas are now withheld with a note unless every step on
  both sides reports the metric. In traces where only model calls carry token counts this
  withholds the token delta. A cost total or delta withheld because steps or runs report
  different or unknown currencies is now disclosed too; before, `analyze` printed
  `cost=unavailable` with no reason.

- Writing an artifact or an analysis report left a hidden temporary file behind when the
  write or fsync failed, because the temporary name was recorded only after writing. All
  four output writers now share one helper that removes it on every failure.

### Compatibility notes for imports

These change what an input that imported successfully before now produces or whether it
imports at all. No committed fixture is affected.

- An OTLP token count given as a boolean (`true`, or `{"intValue": true}`) was stored as `1`;
  it is now unavailable, matching how an unusable cost is already treated. A non-finite
  token count is unavailable too instead of crashing the import. An `intValue` holding a
  boolean is no longer converted to `0`/`1` for any attribute.
- Raw OTLP input could inject evaluation summaries through the private
  `_tracegraph_evaluations` span key, bypassing the length limit the Phoenix importer
  applies. Only the Phoenix importer reads that key now.
- An empty-string `traceId` or `spanId` is rejected. A `parentSpanId` of `0`, `false` or
  `[]` used to be read as "no parent", which made a span with a malformed declared parent a
  root; it is now rejected. A missing, `null` or empty-string `parentSpanId` still marks a
  root span.
- A Phoenix evaluation score too large to represent crashed the import; it is now
  unavailable, like a non-finite score.
- When several spans carry the `graph.node.parent_id` a span declares (a loop), the importer
  picks the nearest execution that started earlier. A span with a missing or unreadable
  start time sorted as time 0 and could win that choice, so a timestamp nobody recorded
  decided a causal edge. Such spans no longer take part: the choice is treated as
  ambiguous, and the span falls back to its declared `parentSpanId`, or the import is
  rejected if it has none.
- The CLA workflow refused every pull request from the maintainer's own commit-author
  account, which is not the account that owns the repository. Both are allowlisted now.
- The release documentation asked for a personal access token with *read* access to pull
  requests. Posting the CLA comment failed with that configuration and succeeded after
  raising it to write; the cause remains unconfirmed, since GitHub documents either Issues
  write or Pull requests write as sufficient. The documentation now asks for write on
  Contents, Issues and Pull requests alike and records the observation rather than
  inferring a rule.
- It also did not say that a Trusted Publisher's "Environment name" is the GitHub
  environment rather than the index, which failed the first 0.2.0 rehearsal.

### Dependencies

- The optional `[cypher]` extra now accepts LadybugDB `>=0.18,<0.22` (was `<0.19`), and the
  lockfile moves to 0.21.2. The full test suite and every fixture's output are unchanged
  on 0.21.2.
- CI now tests against Phoenix CLI 1.18.8 (was 1.8.1). The runtime floor stays 1.0.4: this
  pin is what CI proves, not what tracegraph requires.

### Added

- `tracegraph export-incident-memo <artifact> -o memo.md` writes a Markdown post-mortem
  intended for long-term memory stores. Its output is a strict allowlist: the run id appears
  only as a truncated SHA-256 `run_digest`, steps appear as `step_<n>_<kind>` aliases, and
  no step name, error message, evaluation label or payload is written. Edges that only show
  containment or have no recorded origin are rendered as "preceded by", never "caused by".
  The memo carries the SHA-256 of the exact artifact bytes it was built from.

### Changed

- **Artifact schema version 4.** When a step has several causes, its single TREE_PARENT is
  now chosen by edge origin first — a declared graph or checkpoint parent over span
  containment, over a span link, over an unknown origin — and only then by time. An artifact
  whose tree differs from the purely temporal choice is stamped version 4; every other
  artifact serializes as version 2 or 3, byte for byte as before. **Readers from 0.2.0 and
  older refuse version 4 files** with an explicit unsupported-version error. This build reads
  both projections, and the LadybugDB store re-emits a legacy file unchanged. The derived
  tree feeds `inspect`, `diff`, and `analyze --baseline`, where it decides both the topology
  changes and how steps are matched between the two runs: a step whose tree parent moved
  can now appear as one removed and one added behavior change, and its logical step key
  changes. Raw causal queries (`explain`, `query`) are unaffected.
- Phoenix timestamps without a timezone are now read as UTC rather than in the local
  timezone of the machine running the import. **Digest impact:** artifacts built from such
  exports now hash the same on every machine, and differ from any artifact previously built
  where the local timezone applied a nonzero offset to those timestamps. Timestamps that
  carry a `Z` or an offset are unchanged.
- **Phoenix artifacts no longer carry free-text annotation names or labels.** The Phoenix
  import is documented as body-free and already kept only identifier-shaped span names, but
  annotation names and labels were kept as free text (whitespace-collapsed, cut to 200
  characters), so an evaluator's note such as `user said: my password is hunter2` reached
  the artifact. They now pass the same identifier filter as span names: an evaluation whose
  name fails it is dropped, and a label that fails it becomes `null` while the score is
  kept. **Artifact content change:** a re-import of a Phoenix export with such annotations
  produces different bytes and digests. Identifier-shaped text, which can still be personal
  data such as an e-mail address, is kept.

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
