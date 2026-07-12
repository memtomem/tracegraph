# Live qualified-tool producer fixture

`live-qualified-tool.otlp.json` is produced with SyncMill's body-free
`FileTracer` contract and deterministic IDs/timestamps. It intentionally mixes
one failing qualified MCP TOOL span with one failing quality-gate CHAIN span.

The real CLI test proves that only `gate_e::always_fail` reaches the
`tool-failure@v1` review-candidate export. The gate must never become a tool
candidate, and the consumer remains fail-closed for genuinely unqualified TOOL
endpoints. No Tracegraph schema or pattern version changes are required.
