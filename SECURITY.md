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
exports, and Phoenix exports, and writes artifacts and analysis reports. It never runs a graph
node, does not write to a checkpointer, and is not in any request path. A SQLite
checkpoint file is read through a read-only URI and copied to a private snapshot before it
is opened, so analyzing a live database does not mutate it.

"Read-path" describes what tracegraph does to your systems, not a sandbox. Inputs fall into
three classes, covered in [Reading untrusted input](#reading-untrusted-input):

| input | how it is read | can it run code? |
|---|---|---|
| JSON artifacts, OTLP and Phoenix exports | parsed and validated | no |
| a LangGraph checkpoint database (`ingest --sqlite`) | deserialized with an empty allowlist | hardened, not sandboxed |
| a Python graph you name with `ingest --graph` | imported, and its state callbacks run | **yes, by design** |

## What is redacted, and what is not

Be precise here, because the boundary is narrower than "the tool redacts secrets".

**Reports are filtered. Artifacts are not.** The `safe-v1` privacy contract governs analysis
reports (`analyze`, `phoenix diagnose`, `--json-out`): it keeps structural ids,
identifier-shaped operation names, kind/status/time, tokens and explicit cost, and it drops
prompts, inputs and outputs, tool arguments and results, raw errors and stacktraces, and
session/user/project identifiers. Other display text is replaced with a deterministic
SHA-256 alias, and the report discloses in `warnings` when an alias actually reached it.

The artifact on disk is a different thing. Phoenix ingestion filters at the source, but by
default the OTLP and LangGraph adapters record what they were given:

- OTLP keeps `status.message` and exception messages;
- LangGraph stringifies the configured error state channel, **and** the `repr` of an
  exception a node raised, which it reads from checkpoint pending writes.

Those values reach the artifact and are printed verbatim by `inspect` and `explain`, unless
the trace is ingested with `--redact-errors` (`ingest-otlp` and `ingest` both take it), which
leaves every error message out and marks it unknown on the step. Span and node names are
recorded as given either way.
`export-review-candidates` is likewise not passed through the report redactor: its
`tool_key` is the raw endpoint span name.

**So: if your producer puts prompt text, customer data, or credentials into span names or
exception messages, those land in the artifact** — the messages only when ingested without
`--redact-errors`, the names always. Treat artifacts with the same care as the
traces they came from. Reports, not artifacts, are the artifact you share.

Identifiers are not anonymized in either form — trace and step ids are preserved by design,
because they are what makes a finding traceable back to the run.

## Reading untrusted input

Artifact and span-export loading is a parsing boundary, not a trust boundary: input is
validated, never executed. Malformed input raises a clean error rather than a traceback, and
tree walks are iterative so a deeply nested document cannot exhaust the stack. **CLI JSON
inputs** (artifacts and span exports), files named on the command line and `-` for stdin
alike, are size-capped by default at 256 MiB (`TRACEGRAPH_MAX_ARTIFACT_BYTES` changes it, and
`0` disables it). A LangGraph checkpoint database (`ingest --sqlite`) is not byte-capped: it
is copied to a private snapshot under a deadline (30 seconds by default) before it is read.
The cap is a property of the command-line entry points, not of the library:
`artifact.load()`, `OTLPSpanAdapter.from_file()`, `PhoenixExportAdapter.from_file()`, and
`InMemoryStore.load_artifact()` and `LadybugStore.load_artifact()` (which call
`artifact.load()`) read what they are given, so a library caller handling untrusted input
should bound it themselves. Still, analyzing an artifact you did not produce means reading
strings an attacker may have chosen; render them as data.

Subprocess use is limited to one call site, the Phoenix CLI (`px`), invoked as an argument
list rather than through a shell, with a timeout and without echoing its output. Its stdout is
held to the same `TRACEGRAPH_MAX_ARTIFACT_BYTES` ceiling: it is written to an unlinked temporary
file rather than memory, and `px` is killed once that file passes the limit, so disk use can
exceed it by about one 50 ms poll interval of output. The capture is the file up to the size
sampled once `px`'s exit is observed. On POSIX it is read by position, so reading never moves
the offset other writers share and cannot let their writes overwrite it. `px` is trusted the way
any executable you installed is: tracegraph stops and reaps `px` itself but does not contain the
processes it starts. One that keeps `px`'s stdout can go on writing to the unlinked file after
`px` exits, until it stops. Its stderr is discarded. The ceiling bounds
what is read, not the JSON parse that follows.

### A checkpoint database is executable input, and is treated as such

This is the one input *format* that can run code, so be precise about it. (The other way
tracegraph runs code is a graph you name yourself; see the next section.) LangGraph's
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

### `ingest --graph` runs the Python code you name

`tracegraph ingest --graph path/to/file.py:attr` (or `pkg.module:attr`) loads the compiled graph
that wrote the thread, so ingest can report tasks the graph schedules but that recorded nothing.
That is running your code, not reading data:

- **Importing the reference runs the module's top-level code**, with the file's directory (or,
  for `pkg.module`, the current directory) importable while it loads, as `python path/to/file.py`
  or `python -m pkg.module` would.
- **Reading the checkpoint through the graph runs the callbacks the graph carries**: state schema
  mappers and validators, reducers, custom channels, managed values and cache-key functions.
  LangGraph's `get_state` enumerates the scheduled tasks; **no graph node runs**.

The protections above do not extend to that code. The graph reads through the same private
snapshot (a copy taken from the source database, which is opened read-only) and the same
empty-allowlist serializer, so checkpoint *payloads* are still revived as inert data. The module
and its callbacks, though, run with your privileges, unsandboxed: nothing stops them from
touching files, the network or the source database itself. The "read-path"
label means tracegraph does not execute the graph's work or write to your checkpointer, not
that the code you name is contained.

`--graph` is never inferred. Ordinary `ingest` neither needs nor looks for a graph, and
the option exists only on `ingest`. Pass it only for code you would be willing to run directly.

### JSON inputs

JSON artifacts and span exports are a different matter: they are parsed and validated, never
executed, and no analysis path evaluates code or deserializes pickles.
