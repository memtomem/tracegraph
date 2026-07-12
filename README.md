# tracegraph

Causal-graph analysis of LangGraph agent traces — *time-travel debugging and
root-cause analysis on a causal graph.*

tracegraph ingests agent execution traces that already exist (LangGraph checkpoint
history and OpenInference/OTLP spans) into a **normalized causal graph**,
then runs analyses the mainstream observability tools don't:

- **`explain`** — walk backward from a failure over the *raw* causal graph to every real cause.
- **`diff`** — exact structural regression diff between two runs (rooted-tree / AHU isomorphism).
- **cross-trace pattern matching** — e.g. "tool X → retry → tool X → failure" across all runs.

It is **not** a checkpointer — a graph-backed `BaseCheckpointSaver` already exists.
tracegraph is the read-path *analysis* layer. See [`docs/FEASIBILITY.md`](docs/FEASIBILITY.md)
for the full rationale and competitive landscape.

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

tracegraph presets                   # list cross-trace query patterns
tracegraph query tool-failure A.json B.json   # find a causal pattern across many traces
tracegraph query tool-retry-failure *.json    # the marquee: same tool retried, then failing
tracegraph query tool-failure --backend kuzu A.json B.json   # use the optional Cypher accelerator

# export versioned, body-free governance evidence (an empty match set is a valid report)
tracegraph export-review-candidates tool-retry-failure *.json -o candidates.json
```

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

$ tracegraph query tool-retry-failure A.json
A: search → search
1 match(es) across 1 trace(s)
```

The marquee pattern — *"tool X → retry → tool X → failure"* — is a real query, not just a
tagline. It needs three things a flat predicate list can't express, all added to
`PathPattern` without touching the frozen model: a **back-reference** (`same_name_as`, "the
*same* tool X both times"), a **variable-length gap** (`gap=(lo, hi)`, "→ retry →" = some
intervening steps), and **de-duplication** of fan-in paths. The gap is *unbounded* by default,
because real traces are deep and a retry can be many super-steps later — so the pure-Python
matcher (the system of record) catches it at any distance. Kùzu's openCypher backend hard-caps
variable-length hops at 30, so rather than silently truncate, `compile_to_cypher` refuses an
unbounded gap (`UncompilablePattern`) and `KuzuStore` transparently falls back to the
pure-Python matcher — *the accelerator degrades to slower, never to wrong*. The bounded
`tool-retry-failure-near` variant stays within the cap and runs as native Cypher.

Every shipped preset has a stable `pattern_id` and positive integer `pattern_version`, shown
as `pattern-id@vN` by `presets` and `query`. Increment the version when match semantics or
review-export eligibility changes; wording-only description edits keep the current version.

`export-review-candidates` accepts the same artifact-file/directory batches as `query` and
writes a deterministic schema-v1 envelope. Only presets ending in a failing `TOOL` predicate
are eligible. The matched endpoint name must already be a qualified `server::tool` key —
tracegraph never guesses that identity. Each candidate contains only `run_id`, pattern
id/version, tool key, and the SHA-256 digest of the exact normalized artifact that was queried.
There are no prompts, outputs, errors, step IDs, trace IDs, or local paths, and the command
never changes a Toolgraph manifest or policy. The public schema is
[`contracts/review-candidates.schema.json`](contracts/review-candidates.schema.json).

## Status

**MVP works** — ingest → inspect / explain / diff on real LangGraph traces.

- **Phase 0 (frozen contract):** `RawTrace` vs `NormalizedTrace`, portable JSON artifact (system of record), `validate_raw` → projection → `validate_tree`/`validate_normalized`, in-memory store, `explain`.
- **Phase 1 (ingestion):** `LangGraphCheckpointAdapter` reads any `BaseCheckpointSaver`, including subgraph checkpoint namespaces, and reconstructs declared checkpoint parentage; `examples/tiny_agent.py` generates real traces.
- **Phase 2 (analysis + CLI):** AHU rooted-tree diff and the Typer CLI.
- **Phase 3 (cross-trace queries):** backend-neutral `PathPattern` matcher over the raw causal graph + `query`/`presets` CLI — pure-Python, proving the "graph queries" value before any Cypher backend. Supports variable-length **gaps** and **back-references** (`same_name_as`), which is what makes the marquee `tool-retry-failure` pattern expressible; uncompilable (unbounded) patterns degrade honestly rather than truncate.
- **Phase 5 (OTLP/OpenInference adapter):** `OTLPSpanAdapter` ingests exported spans (Phoenix/Langfuse/Collector) into the same causal model — the source that actually exercises the raw/derived split.
- **Phase 6 (optional Cypher backend):** `tracegraph[cypher]` ships a `KuzuStore` that compiles the **same** `PathPattern` spec to openCypher (`compile_to_cypher`); equivalence with the pure-Python matcher is the test contract, so the Cypher path is an accelerator, never a second source of truth.
- **Ecosystem T3 (feedback preview producer):** OTLP `syncmill.run_id` correlation, versioned presets, and deterministic body-free `export-review-candidates`; Toolgraph intake and SyncMill board exposure remain separate follow-ups.

Caveats: this is a **checkpoint-level** view (one node per super-step); node names/kinds are
best-effort display metadata. Subgraph checkpoints are ingested as namespaced steps, but
cross-namespace causality is limited to parent links declared by LangGraph checkpoint
metadata.

## Develop

```bash
uv sync                    # core only
uv sync --extra cypher     # include the optional Kùzu backend
uv run pytest              # headless
```

The optional Cypher accelerator (`tracegraph[cypher]`, Kùzu) is **not** required for the
core; `explain` and `query` can opt into it with `--backend kuzu`. Its tests are marked
`@pytest.mark.cypher` and skip cleanly without the extra. Kùzu is pinned because its
upstream was archived in Oct 2025 — the embedded format isn't load-bearing here (the JSON
artifact is).
