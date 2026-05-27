# tracegraph

Causal-graph analysis of LangGraph agent traces — *time-travel debugging and
root-cause analysis on a causal graph.*

tracegraph ingests agent execution traces that already exist (LangGraph checkpoint
history first; OpenInference/OTLP spans later) into a **normalized causal graph**,
then runs analyses the mainstream observability tools don't:

- **`explain`** — walk backward from a failure over the *raw* causal graph to every real cause.
- **`diff`** — exact structural regression diff between two runs (rooted-tree / AHU isomorphism).
- **cross-trace pattern matching** *(post-MVP)* — e.g. "tool X → retry → tool X → failure" across all runs.

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
  • diverges at CHAIN > CHAIN > plan > call_tool: A='handle_error' B='respond'
```

## Status

**MVP works** — ingest → inspect / explain / diff on real LangGraph traces.

- **Phase 0 (frozen contract):** `RawTrace` vs `NormalizedTrace`, portable JSON artifact (system of record), `validate_raw` → projection → `validate_tree`/`validate_normalized`, in-memory store, `explain`.
- **Phase 1 (ingestion):** `LangGraphCheckpointAdapter` reads any `BaseCheckpointSaver` (root namespace), reconstructs the causal chain from `parent_config`; `examples/tiny_agent.py` generates real traces.
- **Phase 2 (analysis + CLI):** AHU rooted-tree diff and the Typer CLI.

Caveats: this is a **checkpoint-level** view (one node per super-step); node names/kinds are
best-effort display metadata. Cross-namespace/subgraph ingestion is deferred (the adapter
refuses it loudly rather than guessing).

Next: optional Cypher backend (`tracegraph[cypher]`) for cross-trace pattern queries; the
OTLP/OpenInference adapter.

## Develop

```bash
uv sync
uv run pytest          # 41 tests, headless
```

The optional Cypher accelerator (`tracegraph[cypher]`, Kùzu) is **not** required for
the core and is pinned because its upstream was archived in Oct 2025.
