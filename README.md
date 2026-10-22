# tracegraph

Causal-graph analysis of LangGraph agent traces — *time-travel debugging and
root-cause analysis on a causal graph.*

tracegraph ingests agent execution traces that already exist (LangGraph checkpoint,
Phoenix CLI exports, and OpenInference/OTLP spans) into a **normalized causal graph**,
then runs analyses the mainstream observability tools don't:

- **`explain`** — walk backward from a failure over the *raw* causal graph to every recorded cause.
- **`diff`** — exact structural regression diff between two runs (rooted-tree / AHU isomorphism).
- **cross-trace pattern matching** — e.g. "tool X → retry → tool X → failure" across all runs.

It is **not** a checkpointer — a graph-backed `BaseCheckpointSaver` already exists.
tracegraph is the read-path *analysis* layer. See [`docs/FEASIBILITY.md`](docs/FEASIBILITY.md)
for the full rationale and competitive landscape. Korean readers can start with
[`docs/USAGE_KO.md`](docs/USAGE_KO.md), or run the hands-on notebook
[`examples/getting_started_ko.ipynb`](examples/getting_started_ko.ipynb) from a repository
checkout.
Current delivery evidence, known limitations, and next work are maintained in
[`docs/HANDOFF.md`](docs/HANDOFF.md).

## Install

```bash
uv tool install agent-tracegraph
# or: python -m pip install agent-tracegraph

# With the optional Cypher accelerator (LadybugDB):
uv tool install "agent-tracegraph[cypher]"

# Or run it without installing:
uvx --from agent-tracegraph tracegraph --version
```

**The distribution is `agent-tracegraph`; the command and the import package are
`tracegraph`.** The plain name was already taken on PyPI by an unrelated project, so only the
name you install differs — you still run `tracegraph` and still `import tracegraph`.

Requires Python 3.12. CI exercises 3.12 on Linux; other versions and platforms are untested
rather than unsupported.

## Design commitment: causality is never faked

We persist the **raw multi-parent causal graph** as the system of record (`CAUSED_BY`,
keeping *every* recorded predecessor — every edge the ingest adapter emitted) and treat the
single-parent tree (`TREE_PARENT`) as an explicitly **derived, lossy projection**. Any step
whose causality is collapsed for the tree view is flagged `projection_lossy`, so RCA never
silently presents the tree's single parent as "the" cause.

Recorded is not the same as declared by the source. Adapters reconstruct some edges: OTLP and
Phoenix pick which run of a looped node is the parent by start time or start order, or fall back
to `parentSpanId`; LangGraph routes a subgraph's return through the last checkpoint that
subgraph invocation wrote before the parent graph continued. Some sources lose edges: OTLP span links that resolve to no span in the trace,
and every link in a Phoenix export, which is ingested parents only. `projection_lossy` measures
projection loss (more than one recorded parent), not capture fidelity. Some known limitations are
disclosed per trace (`trace.capture` gaps, `causal_fidelity`, `links_preserved`) or per edge (its
`origin`, such as `checkpoint_subgraph_return` for the LangGraph return edge), and an empty gap
list does not mean the capture is complete.

| analysis | reads |
|---|---|
| `explain` / RCA | raw `CAUSED_BY` |
| `diff` (AHU) | derived `TREE_PARENT` |
| `inspect` render | derived `TREE_PARENT` (lossiness annotated) |

## Usage

```bash
# ingest a LangGraph thread's checkpoint history from a SqliteSaver DB
tracegraph ingest --sqlite trace.db --thread A -o A.json
tracegraph ingest --sqlite trace.db --thread B -o B.json

tracegraph inspect A.json            # render the causal tree (errors in red)
tracegraph explain A.json <step_id>  # raw causal chain back to the root cause
tracegraph diff A.json B.json        # structural regression diff (AHU isomorphism)
tracegraph validate A.json traces/   # validate artifacts before CI/query jobs
tracegraph analyze A.json            # automatically select and explain failures
tracegraph check B.json --baseline A.json --rule failures-not-increased   # regression verdict for CI

# Phoenix: one command after configuring the official `px` CLI
tracegraph phoenix doctor
tracegraph phoenix diagnose                 # latest failed trace; latest trace as fallback
tracegraph phoenix diagnose <trace-id>      # explicit trace
tracegraph phoenix diagnose --project my-agent
# save only the body-free normalized evidence/report when needed
tracegraph phoenix diagnose <trace-id> --save-artifact safe.json --json-out report.json

tracegraph presets                   # list cross-trace query patterns
tracegraph query tool-failure A.json B.json   # find a causal pattern across many traces
tracegraph query tool-retry-failure *.json    # explicit retry marker, then same tool failing
tracegraph query tool-failure --backend ladybug A.json B.json   # use the optional Cypher accelerator
tracegraph summarize tool-retry-failure traces/ -o motifs.json  # group matches by observed shape

# export versioned, body-free governance evidence (an empty match set is a valid report)
tracegraph export-review-candidates tool-retry-failure *.json -o candidates.json

# export an allowlisted incident post-mortem (Markdown) for a long-term memory store
tracegraph export-incident-memo A.json -o incident.md
```

Frequently-used options, beyond `--out` / `--json-out`:

| option | commands | effect |
|---|---|---|
| `--trace`, `-t` | `ingest-otlp`, `ingest-phoenix`, `analyze`, `check` | Pick one trace out of a multi-trace export. For `analyze`, `check` and `ingest-phoenix` on an artifact, it must name the artifact's own trace (exit 2 otherwise). |
| `--error-channel` | `ingest` | LangGraph state channel that signals a step error (default `error`). |
| `--baseline` | `analyze`, `check`, `phoenix diagnose` | Compare against a known-good run: a file for `analyze` and `check`, a Phoenix trace id for `phoenix diagnose`. |
| `--baseline-trace` | `analyze`, `check` | Pick the baseline's trace out of a multi-trace file. On an artifact baseline, it must name its own trace (exit 2 otherwise). |
| `--limit`, `-l` | `analyze`, `phoenix diagnose`, `query` | Cap how many failure candidates or matches are rendered (default 3 for diagnosis). |
| `--explain` | `query` | Print each match's raw causal chain back to its root cause. |
| `--structure` | `diff` | Compare topology only, ignoring labels. |
| `--backend` | `query` | Use the optional LadybugDB Cypher accelerator instead of the pure-Python matcher. |
| `--strict` | `query`, `summarize`, `export-review-candidates` | Fail with exit 2 when a directory holds a `*.json` file that is not a valid artifact, instead of skipping it with a warning. Use it in CI gates, where a skipped corrupt run would otherwise read as "no match". |
| `--rule` | `check` | Rule to evaluate, repeatable; there is no default rule set. |

### Regression check

`check` judges one current run against one baseline you name, with rules you name, and answers
with an exit code a CI job can use directly:

```bash
tracegraph check current.json --baseline baseline.json \
  --rule failures-not-increased --rule pattern-not-increased:tool-retry-failure \
  --json-out check.json
```

| exit | verdict |
|---|---|
| 0 | `PASS` — every rule held on the recorded evidence |
| 1 | `FAIL` — some rule was definitely violated (more failures or matches, or a changed tree) |
| 3 | `INSUFFICIENT_EVIDENCE` — some rule could not decide, and none failed |
| 2 | invalid input or configuration; no report is written |

Rules: `failures-not-increased` (recorded errors per step `(kind, name)` and in total),
`pattern-not-increased:<preset>` (confirmed matches of a preset), and `topology-unchanged`
(derived tree with labels) or `topology-unchanged:shape`. Counts compare step signatures,
because no source records which step of one run is which step of another; two same-named steps
trading which one fails is not detected (`analyze --baseline` shows the positional view).
Evidence that is unknown is never read as zero. When something the selected rule depends on is
unknown — an `unset` status, a value the adapter substituted (`unknown_fields`) or a missing
name on a step the rule could count, or a capture gap that may have left a step out of the artifact — it blocks PASS
on the current side and FAIL on the baseline side, so the rule reports `INSUFFICIENT_EVIDENCE`.
The topology rule compares trees rather than counts, so such a gap on either side makes it
insufficient even when the observed trees match. Unknowns a rule cannot count leave it decided: an unset CHAIN does not block
`pattern-not-increased:tool-failure`, and a withheld error message blocks only `timeout`.
Pairs from different sources or ingested before capture provenance (artifact version 7) are
insufficient for every rule; pairs read with different causal settings, or with causal edges
chosen heuristically at ingest, are insufficient for the rules that read causal structure
(multi-step presets and topology). A step without a name is
treated as unknown wherever a rule compares names, and LangGraph checkpoint names are inferred,
so the labeled topology rule is insufficient for LangGraph runs; the shape rule is not.
`check` takes exactly one current and one baseline input (artifact, OTLP export, Phoenix export
or `-`), never a directory, and never picks a baseline. It also assumes each input is a
complete record of its run: an export missing a failing span cannot be told from a clean one.
The `--json-out` report is `tracegraph.check` v1 (`contracts/check-report.schema.json`), bound
to both canonical artifact digests and each rule's id, version and parameters.

## Phoenix-first workflow

Phoenix remains the trace UI, evaluation, and operational-observability layer. tracegraph
adds deterministic causal diagnosis, retry-pattern detection, and baseline regression analysis
without copying prompt or output bodies into its artifacts.

Instrument LangGraph with the official OpenInference integration and configure Phoenix CLI
1.0.4 or newer as described in the
[Phoenix LangGraph guide](https://arize.com/docs/phoenix/integrations/python/langgraph/langgraph-tracing)
and [Phoenix CLI reference](https://arize.com/docs/phoenix/sdk-api-reference/typescript/arizeai-phoenix-cli).
Check the connection, then diagnose without finding a trace id first:

```bash
tracegraph phoenix doctor
tracegraph phoenix diagnose
tracegraph phoenix diagnose --project my-agent
tracegraph phoenix diagnose <trace-id>
tracegraph phoenix diagnose <trace-id> --baseline <known-good-trace-id>
```

Without a trace id, tracegraph scans Phoenix's 20 newest traces and selects the newest failed
trace. If none failed, it says so and diagnoses the newest trace, so a first successful run is
still useful. Explicit ids bypass that selection. The command invokes only read-only
`px trace list/get --include-annotations` exports, analyzes them in memory, and does not persist
the raw Phoenix response. Phoenix connection details and credentials remain owned by `px`
profiles or environment variables; tracegraph never accepts an API key option. Both commands exit
1 when `px` is missing, cannot run, or returns a response tracegraph cannot use, and 2 for
invalid arguments or output paths. For an already exported file or a shell pipeline:

Automatic retry diagnosis intentionally requires an upstream `retry:` CHAIN marker. SyncMill's
controlled retry telemetry emits this contract; generic OTLP and LangGraph-checkpoint producers
that do not emit it will not be labeled as retries automatically. Their repeated-tool signal
remains available with `tracegraph query tool-repeat-failure-heuristic`, but this inference-only
preset cannot create governance review candidates.

```bash
tracegraph ingest-phoenix --file phoenix-trace.json --out tracegraph.json
tracegraph analyze phoenix-trace.json --json-out analysis.json
px trace get <trace-id> --format raw --no-progress | tracegraph analyze -
```

Phoenix exports preserve span parents but currently do not expose the original OTLP span
links. These artifacts are explicitly marked `causal_fidelity=parent_only`; tracegraph warns
that additional fan-in causes may be missing instead of claiming a complete DAG.

Phoenix's own export gives times as ISO strings. A time given as a number is read as epoch
nanoseconds only from 10^17 up; a smaller one (seconds, milliseconds, a relative offset) is in
a unit tracegraph cannot tell, so it is reported as unavailable and disclosed as
`otlp.unreadable_timestamp`. Such numbers still order spans for one purpose: when several
spans carry the `graph.node.id` that a span names as its `graph.node.parent_id`, and that span
and every candidate start at such a number, the nearest preceding candidate by those numbers is
the parent. That assumes the export gives every time in one unit, and it is disclosed under
`otlp.graph_parent_by_timing` with its own warning line, so `check` treats the edge as heuristic.
Either way the chosen edge has origin `graph_parent_heuristic` rather than `graph_parent`, unless
the span also links to that parent, which declares it.

The exact Phoenix CLI and server versions used by CI are centrally pinned. See the
[Phoenix runtime pin maintenance runbook](docs/phoenix-cli-contract.md) for the controlled
upgrade and contract-verification procedure, and the
[Phoenix + SyncMill operational E2E runbook](docs/phoenix-syncmill-e2e.md) for the scheduled
real-server verification boundary.

The default `safe-v1` privacy contract governs **analysis reports** (`analyze`,
`phoenix diagnose`, `--json-out`). It retains structural IDs, identifier-shaped operation
names, kind/status/time, tokens, explicit cost, and annotation name/label/score. It drops
prompts, inputs/outputs, messages, tool arguments/results, retrieved documents, arbitrary
metadata, raw errors and stacktraces, annotation explanations, session/user/project
identifiers, and credentials. Missing metrics are reported as `unavailable`, never as zero.

**Artifacts are not reports.** Phoenix ingestion applies the same filtering at the source
(identifier-shaped span names only, error messages dropped), but the OTLP and LangGraph
adapters record the span name and error text they were given: OTLP keeps `status.message`
and exception messages, and LangGraph stringifies the configured error state channel. Those
values reach the artifact on disk and are printed verbatim by `inspect` and `explain`.
`ingest-otlp --redact-errors` and `ingest --redact-errors` leave the error text out: a failed
step keeps its `error` status and lists `error_msg` in `unknown_fields`, so a rule that reads
the message (the `timeout` preset) reports it as unknown rather than absent. Names are still
recorded as given. If your producer puts prompt text or secrets in span names, treat artifacts
from those two adapters as sensitive, or ingest through Phoenix. See "Analysis and output
guarantees" below.

For link-preserving analysis, use the optional Collector fan-out example at
[`examples/otel-collector-phoenix-tracegraph.yaml`](examples/otel-collector-phoenix-tracegraph.yaml).
It sends the normal stream to Phoenix and an allowlisted JSONL copy to tracegraph. Then run:

```bash
tracegraph analyze /tmp/tracegraph-otlp.jsonl --trace <trace-id>             # diagnose in memory
tracegraph ingest-otlp --file /tmp/tracegraph-otlp.jsonl --trace <trace-id> -o full-dag.json
tracegraph analyze full-dag.json
```

`analyze` reads an OTLP span export (JSON or Collector JSONL) directly, with the same adapter
settings as `ingest-otlp`, so the report is byte-identical to analysing the ingested artifact
(one ingested with `--redact-errors` can differ where a rule reads the message);
nothing is written unless `--json-out` asks for a report. The input is identified by shape alone
(`schema_version` → artifact, `resourceSpans` → OTLP, `spans`/`traces`/`data` → Phoenix); a
payload carrying both OTLP and Phoenix records is refused, naming the explicit ingest command
for each format (or, for a list, JSONL or envelope, asking you to extract one format's records). Already have an export? The shortest offline first run is
`tracegraph analyze your-export.otlp.json` (add `--trace <id>` when the file holds several), then
copy the `explain` command it prints after saving the artifact it names. The artifact caveat
above still applies: `ingest-otlp` keeps error text unless given `--redact-errors`, and
reports stay `safe-v1`.

The JSON analysis-report contract is versioned at
[`contracts/analysis-report.schema.json`](contracts/analysis-report.schema.json).
Schema v2 can also carry body-free Toolgraph preflight evidence: an exact SHA-256 artifact
digest, non-negative graph generation, and bounded verdict. This is trace metadata, never a
`CAUSED_BY` edge.

```text
$ tracegraph inspect A.json
A (langgraph)
└── -1: CHAIN
    └── 0: CHAIN
        └── 1: plan
            └── 2: call_tool  ✗ tool failed on input 'boom-please'
                └── 3: handle_error
                    └── 4: respond
6 steps · 1 tool · 1 error · 0 lossy-projection

$ tracegraph diff A.json B.json
NOT IDENTICAL
  • only in A under CHAIN > CHAIN > plan > call_tool: subtree handle_error(respond)
  • only in B under CHAIN > CHAIN > plan > call_tool: subtree respond

$ tracegraph query tool-failure A.json B.json
A: call_tool
1 match(es) across 1 trace(s)

$ tracegraph query tool-retry-failure retry.json
retry: search → retry:search → search
1 match(es) across 1 trace(s)
```

The official `tool-retry-failure@v2` matches three consecutive causal steps:
`TOOL X → CHAIN retry:* → TOOL X (ERROR)`. The producer must supply the explicit
retry marker and the endpoint must name the same tool. This strict pattern is review-exportable.
`tool-repeat-failure-heuristic` uses an unbounded gap and
`tool-retry-failure-near` a bounded gap (up to 30); both are query-only heuristics.
The generic matcher still supports gaps and back-references. Unbounded or oversized
Cypher patterns fall back to the Python matcher without truncating results.

Every shipped preset has a stable `pattern_id` and positive integer `pattern_version`, shown
as `pattern-id@vN` by `presets` and `query`. Increment the version when match semantics or
review-export eligibility changes; wording-only description edits keep the current version.

`summarize` groups one preset's matches across the same file/directory batches. It is meant for
deciding which recurring failure shape to look at first, without tallying `query` output by hand.
A group is one observed shape. Two matches share a group when, for every matched step, the kind,
status, source and safe-v1 name agree, and for every edge between them the `CAUSED_BY` origin
agrees, under the same source kind, causal fidelity and link setting, and the same answer to
whether the trace discloses causal edges chosen heuristically at ingest (`motif-signature/v1`).
That last answer is per trace: the gap says only that some edge was picked heuristically, not
which. (A parent picked by start order now has its own origin, `graph_parent_heuristic`, so the
edge comparison already separates it; an artifact written before that records it as
`graph_parent`, like an unambiguous one, and a fallback edge is `span_parent_fallback`.) The
answer is `disclosed` whenever a recognized heuristic-edge gap is present, else `not_disclosed`.
It is `unknown` for a trace with no capture record, or with a gap code this build does not know
and no recognized heuristic-edge gap. It is `not_applicable` for a single-step preset, which has
no edge.
Trace and step ids, error text and artifact file paths are never part of it. Names pass the same
safe-v1 rule as `analyze` reports. An identifier-shaped name is kept even when it looks like a path or an
address, and anything else becomes `redacted:<sha256>`. An operation whose name changes from run
to run gets one group per name. Each group counts its matches and, separately, the traces they
come from, and lists up to three representatives, each an artifact digest plus the matched step
ids. Only *confirmed* matches are grouped, by the same rule as `check`. A match that relies on a
substituted value (a defaulted kind, an unrecorded status) is counted as unconfirmed instead. A
field that is not known is written as `null` and listed under the step's `unknown`, never by the
value that stands in for it. The coverage section is always printed. It shows how many artifacts
were discovered, valid and skipped; how many traces may hide a match (`check`'s per-preset rule);
how many disclose any capture gap; how many carry no capture record; and the unconfirmed matches.
None of it is a rate, and an empty result is not an all-clear. A group does not show a shared root
cause. Version 1 accepts only strictly adjacent presets: between gap endpoints the matched path
is not recorded, so `repeated-agent-failure`, `tool-repeat-failure-heuristic` and
`tool-retry-failure-near` exit 2. `--out` writes the `tracegraph.motif-summary` schema-v1 JSON.
It must not name an input, is excluded from directory discovery, and is left untouched when the
run is refused. Exit 0 with at least one group, 1 with none (the report is still written), 2 on
an input error.

`export-review-candidates` accepts the same artifact-file/directory batches as `query` and
writes a deterministic schema-v1 envelope. Only presets ending in a failing `TOOL` predicate
are eligible. The matched endpoint name must already be a qualified `server::tool` key —
tracegraph never guesses that identity. Each candidate contains only `run_id`, pattern
id/version, tool key, and the SHA-256 digest of the exact normalized artifact that was queried.
There are no prompts, outputs, errors, step IDs, trace IDs, or local paths, and the command
never changes a Toolgraph manifest or policy. A matching artifact without `run_id` fails the
entire batch and leaves an existing output untouched; export never silently drops evidence.
A `*.json` file in a directory that is not a valid artifact is skipped with a warning, as for
`query`; pass `--strict` to fail the batch instead.
The public schema is a repository-vendored contract (not wheel package data) at
[`contracts/review-candidates.schema.json`](contracts/review-candidates.schema.json); G3
consumers should vendor that file explicitly. The schema allows additional fields at both
levels so that version 1 can grow, which means a document that validates can still carry text
tracegraph never writes, such as a prompt or an error message. A consumer must ignore fields it
does not know and must not store, display or forward them, as SyncMill and Toolgraph do; a field
that carries trace body text would need a new major version. `ReviewCandidateReport` in the
`tracegraph.review_candidates` module reads that way too: it drops unknown fields and keeps rejected
input out of the `ValidationError` message.

SyncMill can consume the report without importing this package:

```bash
SYNCMILL_BOARD__ENABLED=true syncmill board import-review-candidates candidates.json
```

The importer creates deterministic, human-required `review` items. It does not run agents,
invoke Toolgraph, or reinterpret strict enforcement policy. Controlled SyncMill E2E covers qualified tool spans, explicit retry causality, Phoenix
streaming, and review intake. Fixture tests remain separate evidence; general deployment
coverage and production acceptance are not established by the controlled workflow.

Toolgraph G3 can independently review the same report with `review-candidates list/annotate`.
Like SyncMill, it derives the same UUIDv5 from the exact candidate tuple for correlation,
but stores dispositions in a separate exact-report-digest-bound sidecar. Toolgraph
`accepted` neither completes the SyncMill board item nor changes a manifest, selector
result, blast radius, preflight result, or graph state.

## Status

Alpha (`0.x`): the artifact, report and review-candidate formats are versioned contracts,
and any change to them is called out in the [changelog](CHANGELOG.md).

| Area | What exists |
|---|---|
| Sources | LangGraph checkpoint savers (including subgraph namespaces and native task failures), OTLP/OpenInference span exports, Phoenix CLI exports |
| Commands | `ingest`, `ingest-otlp`, `ingest-phoenix`, `inspect`, `validate`, `explain`, `diff`, `analyze`, `presets`, `query`, `summarize`, `export-review-candidates`, `export-incident-memo`, `phoenix doctor`, `phoenix diagnose` |
| Patterns | Versioned presets matched over the raw causal graph; the governance-eligible `tool-retry-failure@v2` requires an explicit producer retry marker, and repetition-only heuristics are query-only |
| Backends | Pure-Python in-memory store (default); optional LadybugDB/Cypher accelerator (`[cypher]`), whose results must equal the pure-Python matcher's |
| Ecosystem | SyncMill run correlation (`syncmill.run_id`), Toolgraph preflight evidence as non-causal metadata, a weekly Phoenix + SyncMill end-to-end workflow (Phoenix CLI and server pinned; SyncMill tracks its default branch) |

Known limits:

- A LangGraph task that was scheduled but recorded nothing (a run stopped by the recursion
  limit, killed, or held at an `interrupt_before`/`interrupt_after` breakpoint) leaves no
  write. It is reported only when `ingest --graph` names the compiled graph (below); without
  it such a run reads `ok` unless some other step recorded an error. Trace status stays `ok`
  either way. A task paused by `interrupt()` is reported without a graph. Both are reported
  only at the root graph: the subgraph node is named, not the node inside it.
- Trace status reports whether a recorded step failed, not whether the run completed: a
  paused or stopped run reads `ok` unless a step failed. Next to it, `analyze` (as a warning,
  also in `--json-out`) and `export-incident-memo` show the recorded task markers:
  `interrupted`, `scheduled`, and `undecided interrupt` (an interrupt recorded with a resume or
  an error and no result; it gets no interrupt or scheduled step and is counted from its
  capture gap).
  `inspect` shows the first two. They count what the newest root checkpoint records, per root
  task: an interrupt record outlives a revised graph that finished without writing a
  checkpoint, a resumed subgraph can leave its parent's record, several paused inner tasks
  count as one subgraph task, and `scheduled` is the answer for the graph you passed. A
  malformed pending write is ignored and disclosed, so it can hide a marker; with `--graph`,
  unapplied `Command(goto=...)` input and `Send` packets to a node the graph lacks are
  disclosed rather than counted as scheduled. `check` judges each rule's evidence, not
  completion.
- Phoenix exports carry no span links, so their artifacts are parent-only; additional fan-in
  causes may be missing.
- Report privacy (`safe-v1`) applies to reports; OTLP and LangGraph artifacts keep the
  producer's names, and its error messages unless ingested with `--redact-errors`.
- CI covers Python 3.12 on Linux.

Caveat for the checkpoint adapter: it is a **checkpoint-level** view (one node per
super-step), and node names/kinds are best-effort metadata. Phoenix/OTLP adapters are
span-level. Subgraph checkpoints are namespaced, but cross-namespace causality remains
limited to parent links declared by LangGraph checkpoint metadata, plus the return edge the
adapter reconstructs when the parent namespace continues after a subgraph invocation. That edge
runs through the last checkpoint the invocation wrote before the continuation (a resumed
subgraph continues from, and returns, its namespace's latest checkpoint) and has origin
`checkpoint_subgraph_return`, which `analyze` counts as explicit ancestry and the derived tree
ranks as a declared checkpoint parent. A checkpoint a caller wrote from the checkpoint that
launched the subgraph (`update_state`, a replay's fork) is linked this way only if its
`versions_seen` shows it applied the subgraph node's task, so an edit as another node or a
replay keeps its declared parent, while `update_state(..., as_node=END)` applying the finished
subgraph's output, or a copy of the continuation, returns through the subgraph. Checkpoints do
not show everything, so the edge can still be wrong where they cannot: an `update_state` as the
subgraph node itself (metadata does not record `as_node`), clearing tasks with
`as_node=END` while the subgraph itself is interrupted, fresh input on that launcher, time
travel around a `Send` or a node whose task id the adapter cannot recompute (one triggered by a
plain channel, some barrier configurations), and a subgraph checkpoint written
after the subgraph finished but before the parent graph continued (a node editing its own
subgraph while it is still running, or an outside `update_state` of a finished subgraph while a
sibling task is interrupted). A persistent subgraph (`compile(checkpointer=True)`) keeps one
namespace across the parent graph's runs; each run is told apart by the launcher its
checkpoints record in `metadata.parents`. A caller's edit of an earlier run's checkpoint joins
the run in progress, because the subgraph resumes from its namespace's latest checkpoint. A
`__copy__` of a continuation holds output an earlier checkpoint applied, and nothing records
which one: when earlier checkpoints from that launcher returned through a different subgraph
checkpoint than the last one written before the copy (a subgraph checkpoint was edited in
between, or the launcher was resumed twice), the copy keeps its declared parent without that
return, and the gap `langgraph.ambiguous_subgraph_return` is recorded, which makes `check`'s
multi-step path rules and topology rules insufficient evidence. An `update_state` from the launcher still returns
through the last subgraph checkpoint written before it, edited or not. When an interrupted run is
finished by an `update_state` of the subgraph after the launcher itself was edited, the
continuation keeps the edited launcher as its parent and does not return through the subgraph.

## Develop

```bash
uv sync                    # core only
uv sync --extra cypher     # include the optional LadybugDB backend
uv run pytest              # headless
```

CI also builds the wheel and installs it with locked runtime dependencies in fresh
core and Cypher environments. `scripts/verify_wheel.py` exercises the installed CLI
outside the checkout, checks candidate golden bytes and baseline diagnosis, and verifies
the optional-backend boundary and parity. See the [handoff runbook](docs/HANDOFF.md)
for local reproduction and the distinction between local checks and remote/server evidence.

Releases are tag-driven: a `v*` tag publishes to PyPI through Trusted Publishing, and a
`test-v*` tag rehearses the same build against TestPyPI. The procedure, including what is
verified before anything is uploaded, is in the [release runbook](docs/releasing.md).

The optional Cypher accelerator (`agent-tracegraph[cypher]`, LadybugDB) is **not** required for the
core; `explain` and `query` can opt into it with `--backend ladybug`. Its tests are marked
`@pytest.mark.cypher` and skip cleanly without the extra. The tested LadybugDB compatibility
range is declared in `pyproject.toml`; its embedded cache format is not load-bearing because
the portable JSON artifact remains authoritative and can rebuild the cache.

## Analysis and output guarantees

Implicit ingest filenames keep short portable identifiers as `<id>.json`; other IDs use
`trace-<sha256>.json`. Existing default outputs cause exit 2; use `--out` to explicitly
replace a file. Outputs cannot alias an input or another output. Completed artifacts
are published atomically. SQLite ingest uses a read-only connection and a private backup
with a 30-second deadline, preserving source data, schema, and journal mode. Live WAL
readers can still participate in SQLite's WAL/shared-memory coordination.

`safe-v1` reports retain identifier-shaped display names and replace other display text
with deterministic SHA-256 aliases, and disclose in `warnings` when an alias actually reached
the report. Trace/step identifiers remain; this is not anonymization.
Raw artifact names and bytes are not rewritten for report privacy or matching, so the
artifact — unlike the report — can still carry whatever the producer put in a span name or
error message (`--redact-errors` on `ingest` and `ingest-otlp` drops the message, not the
name). Phoenix ingestion is the exception: it filters at the source, keeping span
names and annotation names and labels only when they are identifier-shaped, and dropping
status messages. Identifier-shaped is not the same as anonymous: an e-mail address passes.
`export-review-candidates` output is not passed through the report redactor: its `tool_key`
is the raw endpoint span name. `export-incident-memo` goes further than `safe-v1`: it writes
no step names, error messages or evaluation labels at all. Steps appear as kind-based
aliases and the run id as a truncated digest; the rest is structural metadata such as trace
status, causal fidelity, step and error counts, preset pattern ids and versions, edge
origins, fidelity disclosures, and the digest of the exact artifact bytes. Logical
comparison keys are JSON-encoded typed paths; consumers should treat them as opaque strings.
Containment and unknown edges preserve all error investigation candidates, deeper first.
Explicit causal ancestry supplies context, not proof that an exception propagated.
When the artifact records that a causal parent was chosen heuristically at ingest
(`otlp.graph_parent_by_timing` or `otlp.graph_parent_fallback`), `analyze` adds a warning for
that side and the memo a fidelity disclosure. A loop parent picked by start order has origin
`graph_parent_heuristic`: `analyze` does not count it as explicit ancestry when it splits primary
from propagated failures, and the memo writes it "preceded by (graph parent, chosen
heuristically)". An artifact written before that origin existed does not say which edge, so a
memo of such a trace with `otlp.graph_parent_by_timing` writes every `graph_parent` edge as
"preceded by (graph parent, possibly chosen heuristically)" instead of "caused by", and
`analyze` classifies it as before. `inspect` prints the
same caveat under its tree, and `explain` and `query --explain` under every chain that has at
least one cause; there their fan-in note no longer calls the causes real or says to trust the
chain.
Explicit OTLP OK takes precedence over handled exception events on new ingestion; old
artifacts are not reinterpreted.
A span whose status is UNSET and that carries no exception event (one with a message or
type makes it `error` in both importers) is read differently by source. `ingest-otlp`
records it as `ok`, because UNSET is OpenTelemetry's default status and producers such as
SyncMill leave successful spans unset. Phoenix ingestion keeps it as `unset`. Trace status
is the same either way, but step statuses differ wherever they are shown, and a preset that
requires an `ok` step does not match an unset one: a SyncMill run where an agent attempt
left UNSET directly causes a failing `gate:` tool reports `gate-failure-after-success`
through `ingest-otlp` and not through Phoenix. Compare a run with a baseline from the same
source; mixing them can report a step whose status differs only by source as a behavior
change (`ok` to `unset`, or the reverse).

LangGraph ingestion reads both the configured error state channel and native task failures
from checkpoint pending writes. A checkpoint is an error where the channel first holds an error.
An error the parent graph already held is not recorded again where a subgraph receives it, nor
where the parent graph continues after a subgraph that did not carry it, and a fork copy
(`update_state(..., as_node="__copy__")`), which runs no node, never records one.
One time-travel shape still records an error twice: copying a subgraph's *input* checkpoint as a
fork and resuming. The subgraph's next start records the error again, whether the parent graph
held it (#90) or a `Send` payload carried it (#96). The copy holds the same channels as the input
it copies but no link back to it. Each marker tried (an unread `__start__` value, a pending
`__start__` task) also appears on a fork of a plain `Pregel` graph's loop checkpoint, where
treating it as an input hid a genuine failure. Matching the copy to an input checkpoint with
identical channels and versions fails too. A `Pregel` input's first step can run a user node, so
the original run is no baseline for the copy's run. And a no-op
`update_state(..., as_node="__input__")` can write an input identical to a loop checkpoint. So
the duplicate is left in place.
A node that writes the channel again while it still holds one may have failed again, or only
have returned the whole state (and a reducer can change the value either way); the checkpoint
cannot tell which, so such a write is not recorded as a failure but disclosed as the capture
gap `langgraph.rewritten_error_channel`. That makes `check`'s count rules one-sided and
success-dependent patterns such as `gate-failure-after-success` insufficient either way.
Clear a plain (last-value) error key before retrying (`{"error": None}`) to have every attempt
recorded; a reducer on the channel decides whether `None` clears it. A node that raises becomes a derived `task` step under the
checkpoint that scheduled it, never a mark on that checkpoint, whose own name belongs to the
node that produced it. The failing node is named by recomputing LangGraph's task id from
checkpoint data; when nothing proves which node ran, the step stays unnamed rather than
borrowing a neighbour's name. When one task raises, LangGraph
cancels its siblings and records that through the same channel; a cancelled task is reported
as an `unset` step with no message, because it was torn down rather than at fault. Only the
latest persisted error survives per task, so repeated attempts are not reconstructed, and
`Send` packets are nameable only where `langgraph.types` is importable — an ordinary install
discloses them instead. A task that called `interrupt()` and whose only write on the thread's
newest root checkpoint is that interrupt becomes an `unset` step with source `interrupt` under
that checkpoint: the recorded history holds no resume and no result for it. That describes the
history, not whether LangGraph would resume the task today. An interrupt recorded together with
a resume or an error and no result is ambiguous in the checkpoint itself — under LangGraph 1.2.2
a task resumed with nothing to write looks exactly like one paused a second time — so it is
disclosed at ingest, not reported. Scheduled tasks need the compiled graph: `tracegraph ingest --sqlite trace.db --thread A
--graph path/to/agent.py:graph` (the `langgraph.json` form) or `--graph pkg.module:graph`.
LangGraph's own `get_state` then lists the tasks that graph schedules at the thread's newest
root checkpoint, and each one with no write there becomes an `unset` step with source
`scheduled`. Whether a task finished, failed or paused is still read from the checkpoint's
writes, so a `scheduled` step never duplicates a `task` or `interrupt` step. A `scheduled`
step does not claim that the task never started: a task killed mid-run records nothing
either. The answer belongs to the graph you pass: a different graph can schedule different
tasks from the same checkpoint, and nothing in one checkpoint proves which graph wrote it, so
the artifact records the graph's structural fingerprint (`trace.schedule_graph`) — even when
nothing is scheduled, which is how "checked, nothing pending" differs from "not checked".
A `Command(goto=...)` input saved on that checkpoint but not yet applied, and `Send` packets
addressed to a node the graph lacks, are left out of LangGraph's own answer; ingest discloses
both instead.
`--graph` runs code: it imports the module (top-level code runs), and reading the checkpoint
runs the graph's state callbacks — schema mappers and validators, reducers, custom channels,
managed values, cache-key functions — though no node runs. It is never inferred.
Every ingest records what the adapter could not observe: `trace.capture` is an object whose
`gaps` list holds a fixed code and count per ingest disclosure (empty when there was none),
and a step's `unknown_fields` names a kind, name or error message the adapter substituted. That
record makes a fresh artifact declare `schema_version` 7, 8 when a causal edge has origin
`graph_parent_heuristic`, or 9 when one has origin `checkpoint_subgraph_return`. The envelope
otherwise follows
content, which is what an artifact written by an earlier release keeps when it is loaded and
saved again: a graph read (`--graph`) needs 6, interrupt steps 5, a derived tree that relies on
EdgeOrigin precedence 4, derived task steps 3, and anything else 2, byte for byte (a v1 file is
migrated to 2 as before).
Missing observed errors still do not
prove success: an artifact ingested by an older build, or one using an unrecognized checkpoint
layout, carries no derived step and looks exactly like a clean run, which is why the capture
disclosure is unconditional. Link preservation
means valid in-trace links only: `declared_dag` and `links_preserved` say how links were read,
not that every declared link resolved. A link that names this trace but no span in the export,
names no trace, or names another trace is not recorded as a cause; each kind is disclosed on
stderr and recorded as a capture gap (`otlp.unresolved_span_link`, `otlp.unscoped_span_link`,
`otlp.foreign_trace_link`), and `check` treats the first two like a step that may be missing. Structural diff compares the derived tree and discloses
lossy step counts. Metrics sum available span observations: partial coverage and producer
aggregation can undercount or double count. Coverage gaps are disclosed rather than absorbed
— a wall duration derived from a subset of steps says so, one whose end precedes its start is
withheld, and a single unparseable cost withholds the whole total instead of understating it.
An OTLP or Phoenix import records an unreadable total cost as `unreadable` for that reason, and
reports token counts, other costs, currencies and evaluation scores it cannot read as
unavailable, disclosing each on stderr; the producer's value is never kept. File input, stdin
and Phoenix `px` output share one size limit (`TRACEGRAPH_MAX_ARTIFACT_BYTES`). `query` reads
artifacts one after another and keeps only the matches it prints (plus the traces `--explain`
renders). With `--limit`, its peak memory therefore grows with the number of artifacts only
by a small per-file record (path, trace id); without it, by the matches it prints. Inside one
trace, too, it keeps only the matches that still fit under `--limit`, but it still enumerates
and counts every match to report the exact total, so time grows with the match count: one
2500-step trace with 3.1M matches takes about 3 s at `--limit 1`, in 70 MiB. The search's own
working state (steps reached at each distance of a gap) does not depend on `--limit`. With
`--backend ladybug` on a pattern that compiles to Cypher, LadybugDB itself buffers the
query's full result set: a 3001-step trace with 2.25M `tool-retry-failure` matches peaked
at 757 MiB, where the store alone takes about 300 MiB. A plain read, `count(*)` and
`ORDER BY … LIMIT` all buffered it in our measurements.

Both stores isolate returned mutable objects. InMemory node upsert replaces by ID;
Ladybug's existing insertion semantics are unchanged. Use `is_isomorphic` or `diff` for
deep-tree equality; `canonical()` preserves nested tuples, whose external Python equality
can still reach the interpreter recursion limit. CI currently covers Python 3.12/Linux;
other supported Python versions and platforms require separate validation.

## Contributing, security, and license

- **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md) — development setup, what this project
  treats as a defect, and the Contributor License Agreement.
- **CLA:** [CLA.md](CLA.md). Signing is one-time per GitHub account per repository; a workflow
  comments on your first pull request with instructions.
- **Code of conduct:** [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — applies to issues, pull
  requests and discussions.
- **Security:** [SECURITY.md](SECURITY.md) — please use private vulnerability reporting rather
  than a public issue. Read the redaction boundary there before sharing artifacts: reports are
  filtered, **artifacts are not**.
- **License:** Apache License 2.0 — see [LICENSE](LICENSE).
  Copyright 2025-2026 DAPADA Inc. and memtomem contributors.
