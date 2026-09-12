# Security policy

## Supported versions

Tracegraph is an alpha project. Security fixes are provided for the latest published
`0.x` release only.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for
[`memtomem/tracegraph`](https://github.com/memtomem/tracegraph/security/advisories/new).
If that form is unavailable, email **contact@dapada.co.kr** with `tracegraph security` in
the subject; do not treat an empty advisory page as "no channel". Do not open a public issue
containing credentials, private endpoint details, or an unpatched exploit. Include affected
versions, reproduction steps, impact, and any suggested mitigation.

## Security boundary

Tracegraph is a **read-path** tool. It reads LangGraph checkpoints, OTLP/OpenInference span
exports, and Phoenix exports, and writes artifacts and analysis reports. It does not execute
graphs, does not write to a checkpointer, and is not in any request path. A SQLite
checkpoint file is read through a read-only URI and copied to a private snapshot before it
is opened, so analyzing a live database does not mutate it.

## What is redacted, and what is not

Be precise here, because the boundary is narrower than "the tool redacts secrets".

**Reports are filtered. Artifacts are not.** The `safe-v1` privacy contract governs analysis
reports (`analyze`, `phoenix diagnose`, `--json-out`): it keeps structural ids,
identifier-shaped operation names, kind/status/time, tokens and explicit cost, and it drops
prompts, inputs and outputs, tool arguments and results, raw errors and stacktraces, and
session/user/project identifiers. Other display text is replaced with a deterministic
SHA-256 alias, and the report discloses in `warnings` when an alias actually reached it.

The artifact on disk is a different thing. Phoenix ingestion filters at the source, but the
OTLP and LangGraph adapters record what they were given:

- OTLP keeps `status.message` and exception messages;
- LangGraph stringifies the configured error state channel, **and** the `repr` of an
  exception a node raised, which it reads from checkpoint pending writes.

Those values reach the artifact and are printed verbatim by `inspect` and `explain`.
`export-review-candidates` is likewise not passed through the report redactor: its
`tool_key` is the raw endpoint span name.

**So: if your producer puts prompt text, customer data, or credentials into span names or
exception messages, those land in the artifact.** Treat artifacts with the same care as the
traces they came from. Reports, not artifacts, are the artifact you share.

Identifiers are not anonymized in either form — trace and step ids are preserved by design,
because they are what makes a finding traceable back to the run.

## Reading untrusted input

Artifact and span-export loading is a parsing boundary, not a trust boundary: input is
validated, never executed. Malformed input raises a clean error rather than a traceback, and
tree walks are iterative so a deeply nested document cannot exhaust the stack. **File inputs
named on the CLI** are size-capped (`TRACEGRAPH_MAX_ARTIFACT_BYTES`); that cap is a property
of the command-line entry points, not of the library — `artifact.load()`,
`OTLPSpanAdapter.from_file()` and piped stdin read what they are given, so a library caller
handling untrusted input should bound it themselves. Still, analyzing an artifact you did not
produce means reading strings an attacker may have chosen; render them as data.

Subprocess use is limited to one call site, the Phoenix CLI (`px`), invoked as an argument
list rather than through a shell, with a timeout and without echoing its output.

### A checkpoint database is executable input, and is treated as such

This is the one input format that can run code, so be precise about it. LangGraph's
checkpoint serializer revives stored objects by importing a module and calling a name, both
read from the payload. Its default behaviour for an unrecognized target is to log a warning
and then call it anyway, so a crafted checkpoint row naming `os.system` or `subprocess.run`
executes on the machine doing the analysis. We verified this against the pinned
`langgraph-checkpoint`: both actually ran.

`tracegraph ingest` therefore hands the saver a serializer with an **empty** allowlist
(`tracegraph/sqlite_snapshot.py`). Framework-registered types still revive; an unregistered
`(module, name)` pair is refused and returned as inert data. Two tests pin this, one through
the public ingest path with a hostile payload embedded in an otherwise valid checkpoint.

Two limits on that guarantee:

- It covers the `ingest` path. **If you construct `LangGraphCheckpointAdapter` yourself with
  your own saver, its serializer is yours to choose** — pass
  `JsonPlusSerializer(allowed_msgpack_modules=())` or set `LANGGRAPH_STRICT_MSGPACK=true`.
- It is a hardening measure over a third-party deserializer, not a sandbox. Prefer to treat a
  checkpoint database from an untrusted source the way you would treat a script from one.

JSON artifacts and span exports are a different matter: they are parsed and validated, never
executed, and no analysis path evaluates code or deserializes pickles.
