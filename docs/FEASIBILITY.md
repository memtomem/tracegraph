# tracegraph — Feasibility & Strategy

*Last updated: 2026-05-28*

## TL;DR

The original concept — "time-travel debugging and causal RCA for LangGraph agents,
on a graph" — is **feasible**, but the pitch needs three corrections before it
describes a defensible project:

1. The headline MVP it proposed (a drop-in graph-backed `BaseCheckpointSaver`) is
   **already built** by someone else. The defensible value is the *read-path analysis
   layer*, not the saver.
2. One of its citations is **fabricated**; the underlying algorithmic reasoning is
   sound on its own and survives without the bad citation.
3. The "graph database is the right substrate" thesis is **contested** by where the
   incumbents are actually investing. That's fine — but it should be framed as a
   hypothesis the project tests, not a settled fact.

What remains after those corrections is a genuinely useful, demo-able open-source
tool with a clear wedge.

## Claim-by-claim verdict

| Claim in the original concept | Reality |
|---|---|
| A drop-in graph `BaseCheckpointSaver` is the MVP | **Already solved.** `langgraph-checkpoint-neo4j` exists and passes the official checkpointer conformance suite. Rebuilding the write path is wasted effort. |
| Checkpoints are "opaque pickle blobs" we'd "decompose" into nodes/edges | **False framing.** A LangGraph checkpoint is a structured `TypedDict` (`id` is a monotonic UUIDv6, plus `ts`, `channel_values`, `channel_versions`, and `metadata.source/step/parents`). The causal parent already exists via `parent_checkpoint_id`. Only oversized payload values fall back to pickle. There is nothing to "decompose" — the structure is handed to you. |
| CTEG (arXiv 2604.17557), AgentTrace (2603.14688), AgentGraph (AAAI 2026) | **Real papers, on-topic.** Causal-graph analysis of agent traces is a genuine 2026 research area. |
| "CTEG §2.13 proves graph isomorphism is polynomial under the single-parent assumption" | **Fabricated.** §2.13 is about a different point entirely. The *reasoning*, however, is independently correct: a single-parent DAG is a forest, and rooted-tree isomorphism (the AHU algorithm) runs in linear time. We use the reasoning and drop the citation. |
| There's a market gap for graph/Cypher trace analysis | **Real, but niche.** No major player exposes Cypher/graph-query causal trace analysis today (see landscape below). |
| Neo4j/FalkorDB, "Apache 2.0 recommended" | **Licensing trap for a hosted product.** Neo4j Community is GPL/AGPL with active litigation; FalkorDB is SSPL. Mostly moot for a self-hosted OSS tool, but it dictates that storage must sit behind an interface and never be a hard dependency. |

## Corrected positioning

**tracegraph is a read-path analysis layer, not a checkpointer.** It ingests traces
that *already exist* (LangGraph checkpoint history and OpenInference/OTLP spans) into
a normalized causal graph, then runs analyses that the incumbents don't:

- **`explain` / causal RCA** — backward traversal over the *raw* causal graph from a
  failure, surfacing every real predecessor.
- **structural regression diff** — rooted-tree (AHU) isomorphism over a *derived*
  single-parent view; exact and linear-time.
- **cross-trace pattern matching** — e.g. "tool X → retry → tool X → failure" across
  all stored traces.

The one non-obvious design commitment that makes the RCA *honest*: we persist the
**raw multi-parent causal graph** as the system of record and treat the single-parent
tree as an explicitly **derived, lossy projection**. Any step whose real causality had
to be collapsed for the tree view is flagged, so an analysis never silently presents a
fabricated single "cause." (This was the sharpest critique surfaced in review, and it
shaped the schema — see `normalize.py` and the `test_normalize_lossy.py` golden test.)

## Competitive landscape (early–mid 2026)

| Tool | Store | Trace model | Cross-trace graph queries? | Self-host |
|---|---|---|---|---|
| LangSmith | SmithDB (Rust / DataFusion + object storage) | run tree | No (trajectory queries / filtering) | Enterprise only |
| Langfuse | ClickHouse (acquired by ClickHouse, 2026-01-16; still MIT) | OTLP spans | No (OLAP/columnar) | Yes (MIT) |
| Arize Phoenix | OTLP-native (ELv2 license) | OTLP spans | No | Yes (ELv2) |
| Braintrust | "Brainstore" (purpose-built) | trace trees | No | — |
| **tracegraph** | portable artifact + pluggable graph backend | **raw causal graph + derived tree** | **Yes** | Yes (OSS) |

The gap is real: nobody exposes graph/Cypher causal-trace analysis. But note the
*direction of incumbent investment* — LangSmith built a purpose-built columnar store
(SmithDB), Langfuse doubled down on ClickHouse, Braintrust built Brainstore. None bet
on a general-purpose graph DB. That's the honest counter-signal to the thesis.

## Licensing summary

- **Neo4j Community:** GPLv3/AGPL with ongoing litigation → unsafe to embed in a hosted
  product. Fine for self-hosted/dev use.
- **FalkorDB:** SSPLv1 → source-available; commercial SaaS needs a license or source
  disclosure.
- **KùzuDB:** MIT, embedded, Cypher — ideal UX, **but the original repo was archived on
  2025-10-10** and is read-only.
- **LadybugDB:** MIT, embedded, Cypher — an actively maintained Kùzu fork/successor and the
  target of tracegraph's optional Cypher accelerator.

Consequence for tracegraph: the **portable artifact is the system of record**, the
**pure-Python in-memory store is the default**, and LadybugDB is an *optional, rebuildable
accelerator* behind the `GraphStore` interface. Its database cache is never load-bearing;
the portable JSON artifact remains authoritative across backend upgrades.

## The honest open question

Is a graph substrate actually a *better* way to debug agents than the tree/eval/columnar
approaches the incumbents chose? We don't assert it — tracegraph is the experiment that
tests it. The bet is that causal RCA and cross-trace structural pattern matching are
materially easier to express and faster to run on a graph than on rows or spans. If that
turns out to be false, the project still delivers a clean, honest trace model; if it's
true, the differentiation is durable.
