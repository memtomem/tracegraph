# Changelog

All notable changes to Tracegraph are documented here.

The **artifact** schema version and the **report**/**review-candidate** JSON schemas are
separate contracts from this version number; changes to any of them are called out
explicitly.

## 0.8.0 - 2026-10-20

### Changed

- **`analyze` and the incident memo say when a causal parent was chosen heuristically** (#138).
  Ingest records `otlp.graph_parent_by_timing` (a loop parent taken by start time or, since #136,
  by start number) or `otlp.graph_parent_fallback` (a declared graph parent not honored, so the
  span parent is used) in the artifact, and `check` and `summarize` read those codes, but
  `analyze` and `export-incident-memo` did not, so a report or memo built from the artifact did
  not say that a parent may have been chosen heuristically, and the memo wrote a graph parent
  taken by start order as "caused by". `analyze` now adds a warning for each side that
  carries either code, and the memo adds a fidelity disclosure. The record does not say which
  edge was chosen that way, so in a trace with `otlp.graph_parent_by_timing` the memo writes every
  `graph_parent` edge in its causal ancestry graph as "preceded by (graph parent, possibly chosen heuristically)" instead of
  "caused by"; checkpoint parents and span links keep "caused by". Reports and memos of traces
  without these codes are unchanged.

- **`explain`, `query --explain` and `inspect` say when a causal parent was chosen heuristically**
  (#140). They read the same two codes: `inspect` prints the caveat `analyze` uses under its
  tree, and `explain` and `query --explain` under every chain with at least one cause (a root
  step has no parent to qualify). In such a trace their fan-in note no longer says "multiple real
  causes" or "trust this raw chain, not the tree"; it says the single-parent tree drops some
  recorded causes and the raw chain lists every recorded ancestor, and the `query --explain` note
  says "in this chain" rather than "in this match", since the step with several causes can be an
  ancestor outside the match. Output for traces without these codes is unchanged. `analyze` still
  classifies failures through a `graph_parent` edge as it did.

- **A Phoenix loop parent is chosen by start numbers whose unit is unknown again** (#136). Since
  0.7.0 a Phoenix time given as a number below 10^17 is reported as unavailable, and with it the
  only evidence for choosing among several spans that carry the `graph.node.id` a span names as
  its `graph.node.parent_id`. The span then fell back to its `parent_id`, or the export was
  refused without one. Now, when that span and every candidate start at such a number, the
  nearest preceding candidate by those numbers is taken. 0.6.1 also chose by them, read as
  nanoseconds, but it dropped a float's fraction first, so with fractional seconds (`a` at
  1783987200.25, `b` at 1783987200.75, the span at 1783987200.5) it took `b` where this release
  takes `a`. The times themselves stay unavailable. The choice is counted under the existing
  `otlp.graph_parent_by_timing` gap with its own warning line, which says it assumes the export
  gives every time in one unit; `check` treats it as a heuristic edge, as it does a choice by
  readable time. Exports with ISO or nanosecond times are read as before, and so is a set of
  candidates where any one has a readable time, no start, or no number below the floor. The
  0.7.0 example (spans `a` and `b` at 1783987200 and 1783987201, `c` at 1783987202 with no
  `parent_id`) is accepted again with `b` as `c`'s parent. Where 0.7.0 fell back to the span's
  `parent_id`, the parent taken now can differ, and so can what follows from it. Each case below
  is a separate export built from that example, adding only what it names:
  - The export can be refused if the new choice closes a causal cycle. Add a root span `r` at
    1783987199 and give `c` the `parent_id` `r` and `b` the `parent_id` `c`: 0.7.0 kept `r` as
    `c`'s parent and exited 0; this release takes `b`, so `ingest-phoenix` and `analyze` exit 2
    and `phoenix diagnose` exits 1 (0.6.1 refused that export too).
  - `analyze` can classify failures differently, because the edge is now a `graph_parent`, which
    it reads as explicit ancestry (#142). Give `c` the `parent_id` `b` and let `b` and `c` fail:
    0.7.0 reported both as primary failures; this release reports `b` as primary and `c` as
    propagated.
  - Step numbers can change. Add spans `x`, `y` (`parent_id` `x`) and `z` with no start time,
    and give `b` the `parent_id` `y` and `c` the `parent_id` `z`: 0.7.0 numbered `c` 3 and `b` 5;
    this release numbers `b` 4 and `c` 5.

  Ties are still broken with these spans at time 0, so step order can also differ from 0.6.1.

## 0.7.0 - 2026-10-20

### Added

- **`--redact-errors` on `ingest-otlp` and `ingest`** (#125) leaves error messages out of the
  artifact. The OTLP and LangGraph adapters record the producer's error text by default
  (`status.message` and exception events; the error channel's value and a raised exception's
  `repr`), and `inspect` and `explain` print it; until now only Phoenix ingestion could drop it.
  With the flag a failed step keeps its `error` status and lists `error_msg` in
  `unknown_fields`, and nothing else in the artifact changes. A rule that reads the message
  therefore treats it as unknown, not absent: `check --rule pattern-not-increased:timeout` reports
  `INSUFFICIENT_EVIDENCE` for a redacted current run, and against a redacted baseline it can still
  pass but not fail. Names are still recorded as given. The default is unchanged. In Python,
  `LangGraphCheckpointAdapter` and `ingest_snapshot` take `include_error_messages=False`, as
  `OTLPSpanAdapter` already did.

### Changed

- **`phoenix diagnose` exits 1, not 2, when `px` is unavailable or its answer cannot be used**
  (#122). That covers a `px` that is missing or cannot run, fails, times out, prints too much,
  prints output that is not UTF-8 or not valid JSON, reports no recognizable version or one
  older than the supported floor, or returns an export tracegraph cannot read, and a project
  with no traces. The command now
  prints `FAIL <reason>` on stderr, as `phoenix doctor` does, instead of the usage banner.
  `diagnose` used 2 for these, the same code as an invalid invocation, so a wrapper could not
  tell "called wrong" from "Phoenix is not reachable". Some exports 0.6.1 accepted are now refused
  as unreadable, so `diagnose` goes from 0 to 1 on them; see the Phoenix time fix below. 2 still
  means invalid arguments or output paths, such as `--limit 0`, two outputs naming the same file,
  or a destination that cannot be written. `doctor` is unchanged.

### Fixed

- **A Phoenix time given as a number too small to be epoch nanoseconds is no longer read as one**
  (#124). The Phoenix adapter took every numeric `start_time` / `end_time` as nanoseconds, so a
  time in seconds, milliseconds or microseconds became a moment near 1970, with its sub-unit
  digits truncated and no warning: a 500 ms span given in float seconds was recorded at
  `1970-01-01T00:00:01.783987+00:00` with a duration of 0.0. A number is now read as nanoseconds
  only from 10^17 (1973-03-03T09:46:40Z) up. Below that, seconds, milliseconds and microseconds
  for any date before the year 5138 cannot be told apart, so the time is reported as unavailable
  and disclosed as `otlp.unreadable_timestamp`, the code an unreadable time already uses, which
  0.6.x readers know. That applies to numbers, digit strings, and strings `int()` reads with a
  sign, surrounding whitespace or underscores. ISO strings are read as before, including compact
  forms such as `20260714_123456`, and so are nanosecond values. Phoenix's own `trace get` export
  uses ISO strings, so artifacts change only for exports that carried such numbers.

  For an export that carries such numbers, the step sequence, the parent chosen among several
  spans sharing a `graph.node.id`, and whether the export is accepted at all can also differ from
  0.6.1, which read the numbers as nanoseconds and ordered spans by them. For example, take spans
  `a` and `b` with `graph.node.id` `loop`, starting at 1783987200 and 1783987201, and a span `c`
  starting at 1783987202 with `graph.node.parent_id` `loop` and no `parent_id`. 0.6.1 took `b` as
  `c`'s parent and exited 0; now `ingest-phoenix` and `analyze` exit 2 and `phoenix diagnose`
  exits 1.

## 0.6.1 - 2026-10-20

### Dependencies

- `langgraph-checkpoint-sqlite` now requires `>=3.1.1` (was `>=2`), the first release with the
  fix for GHSA-47pj-3jcm-6whg, where store namespace prefixes could match across segment
  boundaries. Tracegraph reads checkpoints through `SqliteSaver` and never uses the store; the
  floor keeps an install from resolving a flagged version. The source, commands and JSON
  contracts are the same as 0.6.0, but the install range is narrower: an environment that pins
  `langgraph-checkpoint-sqlite==3.1.0` resolves with 0.6.0 and not with 0.6.1.

## 0.6.0 - 2026-10-20

### Added

- **`tracegraph summarize <preset> <artifacts...>`** (#60) groups a preset's matches across an
  artifact batch by observed shape, so an operator can pick the recurring failure shape to look at
  first. A group's signature (`motif-signature/v1`) is the matched steps' kind, status, source and
  safe-v1 name, the `CAUSED_BY` origins between them, and the trace's source kind, causal
  fidelity, link setting and whether it discloses heuristically chosen edges (per trace, not per
  edge). It has no ids, error text or artifact file paths; an identifier-shaped name that looks
  like a path is kept, as in `analyze` reports. Each group reports its match count and trace count
  separately, plus up to three representatives (artifact digest and step ids). Only matches
  confirmed by `check`'s rule are grouped; the rest are counted as unconfirmed. Fields that are
  not known are kept as unknown. A coverage section always reports skipped artifacts, traces that
  may hide a match, disclosed capture gaps, traces with no capture record and unconfirmed matches.
  `--out` writes the `tracegraph.motif-summary` schema-v1 JSON with the same input protection as
  `export-review-candidates`. Only strictly adjacent presets are accepted; the three gap presets
  exit 2. Memory holds one trace and one entry per group, never the matches. A 160-wide retry fan
  (25,600 matches) adds about 27 KiB over a 40-wide one, where collecting the matches adds about
  2 MiB. The issue suggested validating demand first; this is the minimal version.

### Changed

- **`query --limit` no longer holds every match of the trace it is reading** (#79). Each
  trace's matches were collected and sorted in full before the cap was applied, so one trace
  with millions of matches took about 1 GiB at `--limit 1`. The matchers now produce matches
  one at a time and `query` keeps only those still under the cap. The footer's total stays
  exact, so every match is still counted. Measured on macOS/arm64 at `--limit 1` with
  synthetic traces: a 2500-step chain of failing `x` agents (`repeated-agent-failure`, 3.1M
  matches), and 1500 tools feeding one `retry:x` step that feeds 1500 failing tools
  (`tool-retry-failure` and `tool-retry-failure-near`, 2.25M matches each):

  | trace, preset | memory backend | `--backend ladybug` |
  | --- | --- | --- |
  | chain, `repeated-agent-failure` | 976 → 70 MiB, 5.5 → 2.8 s | 1226 → 308 MiB (Python fallback) |
  | fan, `tool-retry-failure` | 1097 → 74 MiB, 2.8 → 1.6 s | 1753 → 757 MiB (Cypher) |
  | fan, `tool-retry-failure-near` | 927 → 74 MiB, 3.2 → 2.1 s | 1470 → 613 MiB (Cypher) |

  The Cypher rows stay high because LadybugDB buffers the full result set; an empty store
  already takes about 300 MiB. The matches selected, their order, the footer total, exit codes
  and error precedence are unchanged, and `find_matches` returns the same list for every caller.
  Time still grows with the match
  count. A mode that stops early and reports no total was considered and not built.
  `LadybugStore` gains `first_matches(pattern, n)`; `tracegraph.analysis.patterns` gains
  `iter_matches` and `first_matches`.

### Fixed

- **Boundaries that promise `ValueError` no longer leak an error from describing the one they
  caught** (#109). Naming a caught exception runs caller code: `str(exc)` calls its `__str__`,
  and a metaclass can make the type's `__name__` a property. When that raised, its error
  escaped instead: from LangGraph ingest when the saver's listing or a supplied `schedule_graph`
  fails, and from `--graph` when the module fails to import. The validated copy that
  `artifact.dumps()` and `analyze()` use since #82 and #93 (both in this release) named its
  caught exception the same way and is hardened too. These now describe the exception through
  one internal guard (`tracegraph._errors`) that falls back to fixed text, and uses only a name or
  message that is a plain `str`.
  Ordinary exceptions are described exactly as before. `artifact.from_obj()` on a schema-v1
  payload also refuses, as `ValueError`, an edge sequence that raises `SystemExit` or another
  `BaseException` (the current schema already did for `SystemExit`), or whose type check
  raises; `KeyboardInterrupt` still propagates.

- **`--graph` loads a module file whose path is not valid UTF-8** (#110). On POSIX such a path
  arrives surrogate-escaped, and naming the module from its strict UTF-8 encoding refused it:
  `ingest --graph` exited 2 with a `UnicodeEncodeError` for a file it could import. Such a path
  is now named from its file-system bytes, under its own prefix so it cannot take another
  file's name. Every path that loaded before keeps its module name.

- **Cost totals and their baseline delta are exact** (#111). They were computed at `Decimal`'s
  default precision of 28 significant digits and rounded without a note, while the total was
  still reported as complete: a cost of `0.12345678901234567890123456789` was totalled as
  `0.1234567890123456789012345679`, and `10000000000000000000000000000` plus `0.01` as
  `10000000000000000000000000000`. They are now computed at a precision of 160, which is exact
  for any sum of costs within the 64-character bound up to about 10^32 steps. Past that, the total
  (or only the delta) is withheld with a note instead of rounded. Ordinary costs report the same
  digits as before (`1.00 + 1.00` is still `2.00`).

- **A cost too long to write out is no longer summed or stored** (#100). A few characters of
  exponent stand for a number of any size. A model-valid `total_cost` of `"1e1000000"` made
  `analyze` fail with `decimal.Overflow`, an `ArithmeticError` rather than the promised
  `ValueError`, with or without a baseline; `"9e999999"` did not overflow but wrote a
  million-digit total into the report. OTLP ingest wrote such a value out in full, so a 10-byte
  attribute `"1e99999999"` became a 100,000,000-character `total_cost` in the artifact. A cost is
  now read only if, written out in full, it is at most 64 characters (`model.MAX_COST_CHARS`),
  checked from its digits and exponent without writing it out (`model.cost_is_bounded(Decimal)`).
  `analyze` withholds the total
  with its own note, as for an unreadable cost; OTLP ingest treats a longer cost as unreadable,
  so the total and component costs are disclosed as `otlp.unreadable_total_cost` and
  `otlp.unreadable_component_cost` as before, and a readable alias is still used. Artifacts are
  not refused at load. The bound is on each cost read, not on the total: summed exactly (#111),
  two 64-character costs can make a 127-character total, and a million of them 133.

- **A lone surrogate in a producer string no longer ends a command in a traceback** (#101). JSON
  can carry one (`"\ud800"`), and the artifact loads and validates, but strict UTF-8 refused it
  in several places: `analyze` (and `check` and the new `summarize`, which share the helper)
  hashing a name into its `redacted:` alias; `ingest-otlp` without `--out` hashing the trace id
  into the default file
  name; `export-incident-memo` hashing the run id; `inspect`, `explain` and `query` writing the
  name to the terminal; and LangGraph ingest hashing a routing channel into a candidate task id.
  The three hashes now encode with `surrogatepass`, so every other string hashes to the same
  alias, file name and digest as before. The LangGraph candidate is skipped, because LangGraph
  could not have produced a task id from it. The CLI escapes what its output streams cannot
  encode instead of failing.

- **CLI output prints an emoji code in a producer string as written** (#102). The CLI's Rich
  consoles kept emoji substitution on, and escaping markup does not turn it off, so a step named
  `:ok:` printed as 🆗 in `query`, `inspect`, `explain` and `analyze` while the artifact and the
  JSON reports held `:ok:`. Both consoles now have it off, so the new `summarize` prints the same
  way. No text the CLI prints itself used an emoji code.

- **`artifact.from_obj()` on a schema-v1 payload raises only `ValueError`** (#103). When reading
  a caller-supplied lazy edge sequence failed, the migration formatted the exception into its
  message, so an exception whose `__str__` raises escaped as that error instead. The message no
  longer formats the exception at all (its type's name can run caller code too, through a
  metaclass); the original stays on the chain as `__cause__`. `loads()` and the CLI parse JSON
  into lists, so neither could reach this.

- **`analyze()`, `check.evaluate()` and `build_incident_memo()` raise only `ValueError` on a
  trace the model refuses** (#93). On a trace changed in place after it was built they raised a
  raw `TypeError` or `AttributeError` (`seq = "zero"`, `steps = None`, `trace = None`, a step
  `kind = "bogus"`, an integer `error_msg`), mostly from analysis code past their structural
  check, and the memo rendered a step `status = "maybe"` without complaint. Each now analyzes the
  copy the model validates, the same one `artifact.dumps()` and the writers use, so a value the
  model coerces (`"0"` for a `seq`) is analyzed as it would be read and the caller's trace is
  left unchanged. `analyze()` and the memo still accept structurally sound but non-canonical
  traces; `check.evaluate()` still requires the canonical form. The validated copy does not change
  the report for a valid trace; cost totals change separately in this release (#100, #111). That
  copy is now made from a Python-mode dump: the JSON-mode dump
  turned a NaN in `trace.run_id` or `trace.links_preserved` into `null`, which validated, so
  `dumps()` and both writers erased the value instead of refusing it. It no longer depends on
  the warnings filter either: under `-W error` a coercible value used to be refused through
  pydantic's serializer warning. Cost on a 6000-step chain, best of 7, measured in PR #95 against
  its parent `2eaa403` (which already had #82): `analyze()` 0.25–0.27 s
  → 0.30–0.32 s, with a baseline 0.57–0.58 s → 0.69–0.70 s, `check.evaluate()` 0.65–0.67 s →
  0.78–0.79 s, `build_incident_memo()` 0.016 s → 0.044 s.

- **`artifact.dumps()` raises only `ValueError`** (#82). A trace changed in place after it was
  built (pydantic does not revalidate on attribute assignment) could make it raise a raw
  `TypeError` (`seq = "zero"`, `steps = None`, `edges = None`) or write a field the readers
  reject (`status = "maybe"`, an integer `step_id`). `dumps()` now serializes a copy the model
  has validated, as the writers already did, and raises `ValueError` for anything the model
  refuses or cannot serialize: pydantic's `ValidationError` for a mistyped field, a
  serialization error (also a `ValueError`) for a value it cannot dump at all, such as
  `trace = None`. A value the model coerces (`"0"` for a `seq`) is written as it would be read.
  A lazy sequence that fails while being serialized, through `dumps()` or either writer, is now
  reported as a `ValueError` too, instead of whatever it raised. `dumps()` still checks fields,
  not structure: a dangling edge or a stale trace status is written. Checking structure there
  would refuse the structurally sound but non-canonical traces that `analyze()` digests, such as
  one with a stale trace status (`analyze()` itself refuses a dangling edge). Unknown-field flags
  without a capture record now reach a `dumps()`
  caller as `ValidationError`, as they already did for the writers. A valid trace is
  serialized byte for byte as before. Cost on a 6000-step chain, measured in PR #94 (its helper
  was changed again by #93): `dumps()` 0.15 s → 0.20 s;
  `analyze()` digests the current trace through it, a baseline comparison and `check` digest
  both.

- **An error already in the parent graph is no longer recorded again around a subgraph** (#85).
  A LangGraph checkpoint is an error where the error channel holds one and none of the
  checkpoints it was built from did, but the check read only a checkpoint's causal parents, and
  three shapes hid the earlier error from it. A subgraph's `__start__` was compared only with
  the subgraph's input checkpoint, which holds its values as pending writes. A persistent
  subgraph (`checkpointer=True`) entered again was compared with its previous invocation, not
  with the outer checkpoint that launched it. And the parent-graph checkpoint after a subgraph
  was compared only with the subgraph's exit, so a subgraph whose state has no error channel, or
  that cleared its own copy, made the outer error look new. One failure was recorded two or more
  times: three with nested subgraphs, or when the subgraph cleared its copy. The check now also
  reads each checkpoint's declared parents, and for a `__start__`, its input checkpoint's
  declared parent and its launch parent (`metadata.parents`). A former duplicate written by a
  node that rewrote the still-set channel is disclosed as `langgraph.rewritten_error_channel`
  instead, with its stderr line, as for any such write since #47; `check` reads that gap as
  before.

- **A fork copy is no longer a second failure.** `update_state(..., as_node="__copy__")`
  copies a checkpoint and runs no node, but it is linked to the copied checkpoint's parent, so
  copying the checkpoint where an error first appeared (in the root graph, or a subgraph's
  `__start__`) recorded that error again. A fork now never records an error.

  For both: steps, edges, names and kinds are unchanged; what changes is which steps are errors
  (with their `error_msg`) and the disclosure above. An error first written inside a subgraph, a
  `Send` payload error below a clean state, and an error a caller's `update_state` writes are
  recorded as before. Artifacts change only for histories that held such a duplicate. The
  schema stays v7, and the committed fixtures are byte-identical. One shape still records the
  error twice: a fork copy of a subgraph's *input* checkpoint, then resumed (#90).

### Known limitations

- Resuming after a fork copy of a subgraph's input checkpoint
  (`update_state(..., as_node="__copy__")` on it) records an error again at the subgraph's next
  start: one the parent graph held (#90) or one a `Send` payload carried (#96). Four ways of
  recognizing such a copy were tried. Three classified the fork by its content: an unread
  `__start__` value, a `__start__` version absent from `versions_seen`, and pending writes from
  LangGraph's `__start__` task. The fourth matched it to an input checkpoint with identical
  `channel_values`, `channel_versions` and `versions_seen`, and compared its run with the
  original's. Each hid a genuine failure on a plain `Pregel` graph, so none shipped. A
  `Pregel` input's first step can run a user node, and a no-op `update_state(...,
  as_node="__input__")` can write an input identical to a loop checkpoint.

## 0.5.0 - 2026-10-18

### Added

- **Dropped OTLP span links are disclosed** (#49). A link the OTLP importer could not record as a
  cause was skipped silently, while the header still read `causal_fidelity=declared_dag` and
  `links_preserved=true`. Each kind now prints one stderr line with its count and becomes a
  capture gap: `otlp.unresolved_span_link` (names this trace but no span in the export,
  possibly a partial export), `otlp.unscoped_span_link` (names no trace, missing or empty `traceId`; it may
  refer to a span of this trace, so it is no longer treated as foreign) and
  `otlp.foreign_trace_link` (names another trace). `check` treats the first two as gaps that may
  have left a step out (one-sided for the count rules, insufficient for topology) and the third
  as having no effect. The schema stays v7. Only artifacts ingested from exports with such links
  change, and with them the digests of reports made from them; every committed fixture resolves
  all its links. An artifact holding none of these codes reads and checks as before.
- **`--strict` for `query` and `export-review-candidates`** (#50). A `*.json` file in a directory
  that is not a valid artifact is skipped with a warning, so a truncated artifact holding the
  failure let a "pattern must not appear" gate pass with exit 1 ("no matches"), and let
  `export-review-candidates` write a report without it. With `--strict`, any skipped directory
  file fails the command with exit 2 (`--strict: N discovered file(s) could not be loaded as
  tracegraph artifacts`) after the usual skip warning, and no report is written. An explicitly named bad
  file still fails first, as before; `--strict` is checked before "no valid artifacts" and
  duplicate trace ids. Without the flag nothing changes, and with it and no strays the output
  is byte-identical.
- **`tracegraph check`, an explicit regression verdict** (#59): one current run, one baseline the
  caller names, and rules the caller names (`failures-not-increased`,
  `pattern-not-increased:<preset>`, `topology-unchanged[:shape]`; no default set). Exits 0 PASS,
  1 FAIL, 3 INSUFFICIENT_EVIDENCE, 2 invalid input (no report written). Rules compare counts
  per step `(kind, name)`; unknown evidence the selected rule depends on — an `unset` status, a
  substituted value or missing name on a step it could count, a capture gap that may have
  left a step out of the artifact — blocks PASS on the current side and
  FAIL on the baseline side instead of counting as zero (a step-omitting gap on either side makes
  the topology rule insufficient, since it compares trees, not counts). Pairs from different
  sources, or with either artifact lacking capture provenance (written before artifact v7),
  are insufficient for every rule, and pairs read with different causal settings for the
  rules that read causal structure. New contract `tracegraph.check` v1
  (`contracts/check-report.schema.json`), bound to both canonical artifact digests and each
  rule's id, version and parameters. `diff`, `query` and `analyze` keep their exit codes.
- **Capture provenance, artifact schema version 7** (groundwork for #59): every ingest now
  records in the artifact what the adapter could not observe, so a later comparison can tell
  "recorded, nothing disclosed" from "not recorded". `trace.capture.gaps` keeps one fixed code and count
  per ingest disclosure (the stderr text is unchanged and stays out of the artifact; the code
  carries no checkpoint id, task id or producer value), and `capture` is present with an empty
  list when the read raised no disclosure; values substituted on one step are recorded
  separately. `steps[].unknown_fields` names values an adapter substituted:
  `kind` and `name` (a span without a recognized OpenInference kind or a usable name, including
  Phoenix names dropped as non-identifiers; an unattributed LangGraph task; and every LangGraph
  checkpoint step except the input checkpoint and the loop step that applies it, named or not,
  because a checkpoint does not record its writer and its name is inferred from the parent's
  routing) and `error_msg` (OTLP's `"error"` fallback, withheld messages, LangGraph's
  unserialized-exception label). Task, interrupt and scheduled steps named from LangGraph's own
  task ids are not flagged. Two OTLP parent resolutions that were silent are now recorded too: a `graph.node.parent_id` resolved by start time among
  several spans, and one not honored in favour of `parentSpanId`; each also prints a new stderr
  warning. Every newly ingested artifact declares 7 and 0.4.x refuses it by name; an artifact
  written by an earlier release re-serializes exactly as the previous release wrote it (v2–v6
  byte for byte, v1 migrated to v2 as before) and never gains provenance. Newly ingested
  artifacts therefore have new digests, as do analysis reports and incident memos built from
  them; report and memo shapes, other CLI output and exit codes are unchanged.
- **`analyze` reads OTLP span exports directly** (#57): a JSON or Collector JSONL export
  (`resourceSpans`) is diagnosed in memory with the same adapter settings as `ingest-otlp`, so
  the report is byte-identical to analysing the ingested artifact; `--trace` selects one trace of
  a multi-trace export and `--baseline` accepts an OTLP export too. Inputs are routed by shape
  before any adapter runs (`schema_version` → artifact, `resourceSpans` → OTLP, `spans` or a
  root `traces`/`data` list → Phoenix); record validity stays the adapter's call, so the direct
  path accepts and rejects what the explicit ingest commands do. A payload carrying both OTLP
  and Phoenix records exits 2: a single object names the ingest command that reads each format,
  and a list, JSONL or Phoenix envelope says to extract one format's records first. An unknown
  shape exits 2 listing the supported ones instead of failing as a Phoenix export.
  `ingest-otlp --file -` reads stdin, and stdin now goes through the same
  `TRACEGRAPH_MAX_ARTIFACT_BYTES` ceiling as files for `analyze`, `ingest-otlp` and
  `ingest-phoenix` (the stdin half of #53; the `px` output half is the "Phoenix `px` output is bounded" entry below);
  `ingest-phoenix` reads its input with the Phoenix adapter only (its accepted inputs are
  unchanged). Next investigation steps after a direct OTLP
  diagnosis print an `ingest-otlp` save template. No file is written without `--json-out`;
  analysis JSON schemas, exit codes and privacy boundaries are unchanged.
- **Guided investigation after `analyze` and `phoenix diagnose`** (#58): displayed failure
  candidates include full step IDs, with POSIX-quoted `explain` commands for existing artifacts
  or explicitly labelled save/replay templates for exports, stdin, and unsaved Phoenix traces.
  Evidence IDs, paths and commands preserve literal emoji-like tokens and are not forcibly
  line-wrapped; consumed pipe inputs and descriptor aliases (`/dev/stdin`, `/dev/fd/N`,
  `/proc/<pid>/fd/N`, or a symlink to one, including regular-file stdin redirects; a symlink
  to a directory descriptor is not detected) require replay. Nonprinting arguments are shown as
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
  `schedule_graph`, or a `scheduled` step) declares 6, and 0.4.x refuses it by name. The header
  field is omitted, not written as `null`. (A fresh ingest by this release declares 7 for its
  capture record, which outranks 6.)
- **A hands-on getting-started notebook in Korean**, `examples/getting_started_ko.ipynb`, for
  first-time users working from a repository checkout. It runs the example agent and the
  offline commands other than `check` and `export-review-candidates`, interprets each output, and works through two applied scenarios:
  checking a change against a baseline, and aggregating failures across many runs from
  `analyze --json-out` reports. Its outputs are committed, and `tests/test_notebooks.py`
  executes it so its commands and exit codes cannot drift from the CLI. `nbclient` and
  `ipykernel` join the development dependencies. Nothing ships in the wheel or sdist, and no
  artifact or report contract changes.

### Changed

- **`artifact.save()` and `artifact.save_atomic()` refuse a trace the readers would reject**
  (#67). Pydantic does not revalidate on attribute assignment, so a `NormalizedTrace` changed in
  place could be written in a form tracegraph's own readers refuse: `save()` wrote a mistyped
  `status`, and both wrote a `CAUSED_BY` edge to a missing step (which `artifact.load` accepted
  and only `validate` / `analyze` rejected), a trace status that disagrees with its steps, a
  wrong `projection_lossy` flag, or a missing `TREE_PARENT`. Both writers now round-trip the
  model and run `validate_normalized`, the check the stores and `validate` apply on load
  (legacy projections included), before touching the destination, and raise `ValueError`
  (pydantic's `ValidationError` is one), leaving an existing file untouched. A mistyped `seq` used to escape
  every writer as a raw `TypeError`; it is now a `ValidationError` too. A valid trace is written
  byte for byte as before, from the validated copy, so a value the model coerces (`"0"` for a
  `seq`) is written as it would be read. Cost on a 6000-step chain: `save()` 0.16 s → 0.29 s,
  `save_atomic()` 0.19 s → 0.30 s; `ingest` already validated before saving, so it now checks
  twice. Because the model now checks first, a capture-provenance
  violation reaches a writer's caller as pydantic's `ValidationError` rather than the
  `check_provenance` message (`artifact.dumps()` still raises the latter). `artifact.dumps()`
  stays a serializer without the model and structure checks (its `TypeError` on a mistyped
  `seq` is #82).
- `inspect` marks a step paused by `interrupt()` as `⏸ interrupted`, in yellow rather than
  the green used for finished steps, and the summary line gains `· N interrupted` when there
  is one. The output for every trace with neither interrupted nor scheduled steps is unchanged.

### Fixed

- **A LangGraph node writing the error channel while it is still set is disclosed** (#47). A
  checkpoint is an error only where the error channel is set and every causal parent's is
  empty, so a node failing again before the key was cleared stayed `ok`, silently, and the
  retry presets could not match. The rule is unchanged: a checkpoint does not record which
  node raised, and both a value-change rule and the issue's version rule were measured to
  invent failures (a node returning the whole state, a reducer that grows the value, a
  handler rewording the error). Instead, a node-written checkpoint whose error channel was
  written again while set (its `channel_versions` entry moved, or without versions its value
  changed) counts toward a stderr disclosure and the capture gap
  `langgraph.rewritten_error_channel`. `check` reads it for the count rules (one-sided on that
  side) and for success-dependent patterns such as `gate-failure-after-success` (insufficient
  on either side), not for topology. Input, `__start__`, update and fork-copy checkpoints, and
  parents in another namespace (a subgraph's entry or exit), are not compared, so propagation
  and a caller's input disclose nothing; a propagated error recorded again at a subgraph's
  `__start__` is pre-existing and filed as #85. Measured on LangGraph 1.2.2 and 1.2.15 with
  InMemorySaver and SqliteSaver. Artifacts change only for histories with such writes; the
  committed fixture has none. Schema stays v7.
- **`analyze --trace` and `--baseline-trace` no longer ignore the selector on an artifact**
  (#64). An artifact holds one trace, and a selector naming a different one was accepted: the
  command exited 0 with a report on the artifact's trace, not the one requested. It now exits 2
  with `Invalid value for --trace: <input> holds trace '<id>', not '<selector>'` (or
  `--baseline-trace`) and writes no report, as `check` already did and as a missing trace in an
  OTLP or Phoenix export does. A selector naming the artifact's own trace is still accepted, and
  the report is byte-identical to the one without it.
- **`ingest-phoenix --trace` no longer ignores the selector on a Phoenix-sourced artifact** (#69).
  Re-ingesting such an artifact with a selector naming another trace exited 0 and re-saved the
  artifact's own trace. It now exits 2 with the same message as `analyze` and writes nothing, to
  `--out` or to the default path. A matching selector re-saves the artifact byte for byte, as
  before. An artifact from any other source keeps its existing error, `--file must contain a
  Phoenix CLI trace export`.
- **`phoenix doctor` and `phoenix diagnose` no longer crash when `px` cannot be run** (#56). A
  `px` on PATH without the exec bit, a directory named `px`, or a file the OS cannot execute
  raised `PermissionError` or `OSError` with a traceback. They now report that `px` failed with
  an OS error, naming the operating system's text for the error number and never the exception
  message: `doctor` prints `FAIL` and exits 1, `diagnose` exits 2, as for their other `px`
  failures. When the OS refused to launch `px`, the message adds a hint to check that `px` on
  PATH is an executable file.
- **Phoenix `px` output is bounded** (#53, the half #63 left open). `phoenix doctor` and
  `phoenix diagnose` held all of `px`'s stdout and stderr in memory with only a time limit. Stdout
  now goes to an unlinked temporary file held to the same `TRACEGRAPH_MAX_ARTIFACT_BYTES` ceiling
  as file and stdin input (default 256 MiB, `0` disables it). `px` is killed and reaped once the
  file passes the limit, and the command reports `... output exceeded the N-byte limit` without
  echoing the output: doctor `FAIL` exit 1, diagnose exit 2. Stderr is discarded; it was never
  shown. Two smaller changes come with it. Output that is not UTF-8 is now reported as such:
  before, `doctor` printed the codec error and `diagnose` crashed with a traceback. And a
  descendant of `px` that keeps stdout open no longer holds the call until the timeout, and
  the capture stops at the size sampled once `px` has exited, read by position on POSIX so a
  descendant's later writes cannot overwrite it. Such descendants are not
  stopped: `px` is trusted like any installed executable. Timeout, nonzero-exit and
  launch-error messages are unchanged.
- **`SECURITY.md` now states the `ingest --graph` trust boundary** (#55). It said tracegraph
  "does not execute graphs" and called a checkpoint database the one input that can run code,
  but `--graph` imports the module you name, running its top-level code, and then runs the
  graph's state callbacks while reading. A new section says what runs (no node does), that
  checkpoint payloads stay under the empty serializer allowlist while the named code itself is
  unsandboxed, and that the graph is never inferred. A table at the top separates JSON parsing,
  hardened checkpoint deserialization and explicitly supplied graph code. The same file no
  longer says piped stdin is read without a size cap: since direct OTLP analysis (#63), `-` goes
  through the same `TRACEGRAPH_MAX_ARTIFACT_BYTES` ceiling as a named file. Documentation only.
- **`query` no longer keeps every trace and every match in memory** (part of #54). It loaded
  and validated all artifacts, matched them all, and only then sliced the result to `--limit`,
  so `--limit 1` over 200 artifacts of 2000 steps (248 MB on disk) peaked at 1374 MiB. It now
  loads, validates and matches one artifact after another, keeps only the matches it prints
  (and, with `--explain`, the traces they belong to), and builds labels only for those. With
  `--limit`, peak memory grows with the number of artifacts only by a small per-file record:
  70 MiB for the same query. Without `--limit` it holds the matches it prints: 105 MiB. Output, exit codes and error precedence are
  unchanged: every input is still validated, a later file's load error or a duplicate
  trace_id still wins over a matching failure, and the footer's total stays exact. Matches past
  `--limit` are counted but no longer labelled, so a labelling fault there (an internal bug:
  labels look up step ids the matcher just returned) would no longer surface. One trace
  with very many matches still enumerates them all (3.1M matches: 971 MiB, down from 986 MiB
  but 5.4 s instead of 17 s); bounding that needs a decision about the `--limit` total and is
  tracked as #79. An interrupt or `MemoryError` now surfaces at the artifact being read,
  after the skip warnings collected so far, rather than after every file has loaded.
- **`repeated-agent-failure` no longer takes quadratic time when no names can match** (#52).
  The unbounded two-step matcher it shares with `tool-repeat-failure-heuristic`, and which
  `analyze` runs on every trace, walked and sorted every failing endpoint's whole causal
  history before checking the name back-reference: a 2000-step chain of failing agents with distinct names
  took 1.3s for zero matches, 4x per doubling. It now skips an endpoint no earlier candidate
  can pair with and walks back only as far as the earliest one (2000 steps: 0.001s; a chain
  whose only repeats are disjoint adjacent pairs is linear too). The longest-path pass also stopped rescanning a
  high-fan-out cause once per endpoint. Matches, their order and every report are unchanged.

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
