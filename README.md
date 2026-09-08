# tracegraph

Causal-graph analysis of LangGraph agent traces — *time-travel debugging and
root-cause analysis on a causal graph.*

tracegraph ingests agent execution traces that already exist (LangGraph checkpoint,
Phoenix CLI exports, and OpenInference/OTLP spans) into a **normalized causal graph**,
then runs analyses the mainstream observability tools don't:

- **`explain`** — walk backward from a failure over the *raw* causal graph to every real cause.
- **`diff`** — exact structural regression diff between two runs (rooted-tree / AHU isomorphism).
- **cross-trace pattern matching** — e.g. "tool X → retry → tool X → failure" across all runs.

It is **not** a checkpointer — a graph-backed `BaseCheckpointSaver` already exists.
tracegraph is the read-path *analysis* layer. See [`docs/FEASIBILITY.md`](docs/FEASIBILITY.md)
for the full rationale and competitive landscape. Korean readers can start with
[`docs/USAGE_KO.md`](docs/USAGE_KO.md).
Current delivery evidence, known limitations, and next work are maintained in
[`docs/HANDOFF.md`](docs/HANDOFF.md).

## Design commitment: causality is never faked

We persist the **raw multi-parent causal graph** as the system of record (`CAUSED_BY`,
keeping *every* real predecessor) and treat the single-parent tree (`TREE_PARENT`) as an
explicitly **derived, lossy projection**. Any step whose causality is collapsed for the
tree view is flagged `projection_lossy`, so RCA never silently presents a fabricated cause.

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

# export versioned, body-free governance evidence (an empty match set is a valid report)
tracegraph export-review-candidates tool-retry-failure *.json -o candidates.json
```

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
profiles or environment variables; tracegraph never accepts an API key option. For an already
exported file or a shell pipeline:

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

The exact Phoenix CLI and server versions used by CI are centrally pinned. See the
[Phoenix runtime pin maintenance runbook](docs/phoenix-cli-contract.md) for the controlled
upgrade and contract-verification procedure, and the
[Phoenix + SyncMill operational E2E runbook](docs/phoenix-syncmill-e2e.md) for the scheduled
real-server verification boundary.

The default `safe-v1` privacy contract retains structural IDs, identifier-shaped operation names,
kind/status/time, tokens, explicit cost, and annotation name/label/score. It drops prompts,
inputs/outputs, messages, tool arguments/results, retrieved documents, arbitrary metadata,
raw errors and stacktraces, annotation explanations, session/user/project identifiers, and
credentials. Missing metrics are reported as `unavailable`, never as zero.

For link-preserving analysis, use the optional Collector fan-out example at
[`examples/otel-collector-phoenix-tracegraph.yaml`](examples/otel-collector-phoenix-tracegraph.yaml).
It sends the normal stream to Phoenix and an allowlisted JSONL copy to tracegraph. Then run:

```bash
tracegraph ingest-otlp --file /tmp/tracegraph-otlp.jsonl --trace <trace-id> -o full-dag.json
tracegraph analyze full-dag.json
```

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

`export-review-candidates` accepts the same artifact-file/directory batches as `query` and
writes a deterministic schema-v1 envelope. Only presets ending in a failing `TOOL` predicate
are eligible. The matched endpoint name must already be a qualified `server::tool` key —
tracegraph never guesses that identity. Each candidate contains only `run_id`, pattern
id/version, tool key, and the SHA-256 digest of the exact normalized artifact that was queried.
There are no prompts, outputs, errors, step IDs, trace IDs, or local paths, and the command
never changes a Toolgraph manifest or policy. A matching artifact without `run_id` fails the
entire batch and leaves an existing output untouched; export never silently drops evidence.
The public schema is a repository-vendored contract (not wheel package data) at
[`contracts/review-candidates.schema.json`](contracts/review-candidates.schema.json); G3
consumers should vendor that file explicitly.

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

**MVP works** — LangGraph/Phoenix/OTLP ingest plus inspect, explain, analyze, diff, and query.

- **Phase 0 (frozen contract):** `RawTrace` vs `NormalizedTrace`, portable JSON artifact (system of record), `validate_raw` → projection → `validate_tree`/`validate_normalized`, in-memory store, `explain`.
- **Phase 1 (ingestion):** `LangGraphCheckpointAdapter` reads any `BaseCheckpointSaver`, including subgraph checkpoint namespaces, and reconstructs declared checkpoint parentage; `examples/tiny_agent.py` generates real traces.
- **Phase 2 (analysis + CLI):** AHU rooted-tree diff and the Typer CLI.
- **Phase 3 (cross-trace queries):** backend-neutral `PathPattern` matcher over the raw causal graph + `query`/`presets` CLI — pure-Python, proving the "graph queries" value before any Cypher backend. The official `tool-retry-failure@v2` requires an explicit producer marker; variable-length **gaps** and **back-references** (`same_name_as`) remain available for query-only heuristics. Uncompilable unbounded patterns degrade honestly rather than truncate.
- **Phase 5 (OTLP/OpenInference adapter):** `OTLPSpanAdapter` ingests Collector JSON/JSONL spans into the same causal model — the source that exercises full link-preserving raw/derived causality.
- **Phoenix diagnosis:** `PhoenixExportAdapter`, `analyze`, and `phoenix diagnose` provide body-free automatic failure selection, retry detection, telemetry/evaluation summaries, and explicit parent-only fidelity warnings.
- **Phase 6 (optional Cypher backend):** `tracegraph[cypher]` ships a `LadybugStore` that compiles the **same** `PathPattern` spec to Cypher (`compile_to_cypher`); equivalence with the pure-Python matcher is the test contract, so the Cypher path is an accelerator, never a second source of truth.
- **Ecosystem T3/P4 review slice:** OTLP `syncmill.run_id` correlation, versioned presets, deterministic body-free `export-review-candidates`, SyncMill human-review board intake, Toolgraph G3 artifact annotation, and the pinned live single-failure review path are complete. Explicit retry causality and SyncMill-to-Phoenix streaming are covered by the controlled E2E workflow; broader operational coverage remains separate.
- **SyncMill contract completion:** route/pipeline/compete/council/decompose plus cancellation fixtures, stable span naming, body-free artifact digests, fail-open exporter reference behavior, operational failure presets, and non-causal Toolgraph preflight evidence are covered by executable tests.

Caveat for the checkpoint adapter: it is a **checkpoint-level** view (one node per
super-step), and node names/kinds are best-effort metadata. Phoenix/OTLP adapters are
span-level. Subgraph checkpoints are namespaced, but cross-namespace causality remains
limited to parent links declared by LangGraph checkpoint metadata.

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

The optional Cypher accelerator (`tracegraph[cypher]`, LadybugDB) is **not** required for the
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
with deterministic SHA-256 aliases. Trace/step identifiers remain; this is not anonymization.
Raw artifact names and bytes are not rewritten for report privacy or matching. Logical
comparison keys are JSON-encoded typed paths; consumers should treat them as opaque strings.
Containment and unknown edges preserve all error investigation candidates, deeper first.
Explicit causal ancestry supplies context, not proof that an exception propagated.
Explicit OTLP OK takes precedence over handled exception events on new ingestion; old
artifacts are not reinterpreted.

LangGraph observes the configured error state channel; native pending-write exceptions
are not yet ingested. Missing observed errors do not prove success. Link preservation
means valid in-trace links only. Structural diff compares the derived tree and discloses
lossy step counts. Metrics sum available span observations: partial coverage and producer
aggregation can undercount or double count. File input has a size limit, but stdin/Phoenix
subprocess output and query materialization are not streaming memory guarantees.

Both stores isolate returned mutable objects. InMemory node upsert replaces by ID;
Ladybug's existing insertion semantics are unchanged. Use `is_isomorphic` or `diff` for
deep-tree equality; `canonical()` preserves nested tuples, whose external Python equality
can still reach the interpreter recursion limit. CI currently covers Python 3.12/Linux;
other supported Python versions and platforms require separate validation.
