"""Ingest LangGraph checkpoint history into a :class:`RawTrace`.

Reads any ``BaseCheckpointSaver`` via ``.list()`` and reconstructs the causal chain from
each checkpoint's declared parentage. One checkpoint (super-step) becomes one ``Step``;
the parent-checkpoint links become ``CAUSED_BY`` edges (effect → cause). One kind of edge is
reconstructed rather than declared: the return from a subgraph (below), which runs through the
subgraph invocation's chronologically last checkpoint and is not marked as such.

All checkpoint namespaces for the requested thread are ingested. Root-namespace
checkpoints keep their checkpoint id as ``step_id``; non-root checkpoints use
``"<checkpoint_ns>:<checkpoint_id>"`` so subgraph checkpoint ids cannot collide with root
ids. Causality comes from ``parent_config`` first; subgraph namespace roots also use the
closest LangGraph-declared ``metadata.parents`` entry when there is no direct
``parent_config``. When a parent namespace continues after a subgraph, LangGraph records
the parent namespace checkpoint as its ``parent_config``; this adapter expands that
namespace-level shortcut through the terminal checkpoint inside the subgraph namespace.

A few facts about the checkpoint stream this relies on (verified against
langgraph-checkpoint 4.x):

* ``checkpoint["id"]`` is a monotonic UUIDv6. Root-namespace ``metadata["step"]`` values
  increase with the parent chain; subgraph namespaces can reuse local step numbers, so the
  adapter re-sequences globally when declared cross-namespace parentage needs it.
* ``metadata["writes"]`` is **not** reliably populated by ``list()``; instead we read the
  serialized ``channel_values``. A step "introduced" an error when a designated error
  channel (default ``"error"``) is truthy at that checkpoint but falsy at its parent. A node
  writing the channel again while it still holds an error may have failed again, or may only
  have passed the state through (a reducer can change the value either way); a checkpoint
  cannot tell which, so such a write is disclosed, not recorded (``_rewrote_error``).
* the producing node's name is recovered best-effort from the parent checkpoint's
  internal ``branch:to:<node>`` channel; absent that, ``name`` is left unset.

Native task failures
--------------------
A node that *raises* never reaches ``channel_values``: LangGraph records the exception as a
``(task_id, "__error__", repr(exc))`` entry in the checkpoint's **pending writes**, and the
Pregel loop explicitly skips those control channels when applying writes. Such a run also
writes no further checkpoint, so reading channels alone reports a crashed run as a clean
success. This adapter therefore derives one extra ``Step`` per errored task, as a
``CAUSED_BY`` child of the checkpoint that scheduled it — never by marking that checkpoint,
whose own name belongs to the node that *produced* it, not the one that failed.

Naming a derived step requires evidence, never a guess:

* a ``__error_source_node__`` write for the same task id names it outright (LangGraph emits
  this only for nodes with an ``on_error`` handler), otherwise
* the task id is **recomputed** for each candidate node and compared. LangGraph derives it as
  an xxh3-128 digest over the checkpoint id, task namespace, next step number, node name,
  ``__pregel_pull``, and the node's *configured* triggers (``langgraph.pregel._algo``); a
  ``Send`` task uses ``__pregel_push`` and the packet index instead. Candidates come from the
  checkpoint's own channels, so a node LangGraph never mentions can never be named.

When neither works the step keeps ``name=None`` rather than borrowing a neighbour's name.

Not every ``__error__`` write is a failure. When one task raises, LangGraph cancels the
siblings still running in that super-step and records a ``CancelledError`` for each through
the same channel. Those tasks did not fail — they were torn down — so they become ``UNSET``
steps carrying no message, and only the task that actually raised is an ``ERROR``.
Only the *latest* persisted error survives per task id — a fail/fail/succeed sequence leaves
one record — so attempts are never reconstructed. Errors are recorded wherever they appear;
this module does not infer anything from their *absence*.

Interrupted tasks
-----------------
A node that calls ``interrupt()`` leaves an ``__interrupt__`` pending write on the checkpoint
that scheduled it, and LangGraph resumes from the thread's newest root checkpoint. A task whose
*only* write on that checkpoint is ``__interrupt__`` therefore never received a resume or
produced a result, and becomes an ``UNSET`` step with ``source=INTERRUPT``. That is a statement
about the recorded history, not a prediction that LangGraph would resume it now.

Scheduled tasks
---------------
A task that was scheduled but recorded nothing (the recursion limit, a killed process, a static
breakpoint) leaves no write at all. The checkpoint alone cannot tell it from finished work, so
it is reported only when the caller supplies the compiled graph (``schedule_graph``). LangGraph
itself then lists the tasks the graph schedules at the newest root checkpoint, through the public
``get_state``, and each one that has no pending write there becomes an ``UNSET`` step with
``source=SCHEDULED``. ``get_state`` is used only to enumerate: whether a task finished, failed or
paused is still read from the checkpoint's own writes, so the paths above are unchanged and the
two kinds of step can never describe the same task.

The answer belongs to the supplied graph. A different graph can schedule different tasks from
the same checkpoint, and no check over one checkpoint can prove which graph wrote it, so the
trace records the graph's fingerprint rather than claiming it is the one. Two things LangGraph
silently drops from its own answer are disclosed instead of reported: a ``Command(goto=...)``
input saved on the newest checkpoint by a run that then died (``get_state`` lists tasks before
applying such writes), and ``Send`` packets addressed to a node the graph does not have.
"""

from __future__ import annotations

from binascii import Error as BinasciiError, unhexlify
from hashlib import sha1
import heapq
import re
from typing import Any, Iterable, Iterator, get_type_hints

from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata
from pydantic import TypeAdapter, ValidationError

from tracegraph._errors import describe, type_name
from tracegraph.adapters.base import Disclosures
from tracegraph.input_validation import object_field
from tracegraph.model import (
    CausalFidelity,
    Edge,
    EdgeOrigin,
    EdgeType,
    RawTrace,
    Step,
    StepKind,
    StepSource,
    ScheduleGraph,
    StepStatus,
    Trace,
    UnknownField,
)

_ROOT_NS = ""
_BRANCH_PREFIX = "branch:to:"
_JOIN_PREFIX = "join:"
_TOOL_TOKENS = {"tool", "tools"}

# The shape of every field this adapter reads, taken from the types LangGraph itself declares
# rather than restated here, so a LangGraph type change surfaces (in the upstream drift
# workflow) instead of hiding behind a stale copy. Only fields the adapter reads are checked:
# LangGraph declares ``counters_since_delta_snapshot`` as tuples, but both savers hand it back
# as lists, so checking everything would reject histories LangGraph itself wrote. ``source``
# is any string, not LangGraph's Literal, because an unknown source still reads as a loop.
_CHECKPOINT_FIELDS = {
    name: TypeAdapter(get_type_hints(Checkpoint)[name])
    for name in ("id", "ts", "v", "channel_values", "channel_versions", "versions_seen")
}
_METADATA_FIELDS = {
    **{name: TypeAdapter(get_type_hints(CheckpointMetadata)[name]) for name in ("step", "parents")},
    "source": TypeAdapter(str),
}
_CONFIG_FIELDS = {name: TypeAdapter(str) for name in ("thread_id", "checkpoint_ns", "checkpoint_id")}

# LangGraph's reserved pending-write channels and task-path markers, spelled out rather than
# imported: they live under ``langgraph._internal``, which is private and not a dependency of
# this package. They are stable on-disk names — a checkpoint written by any 4.x saver uses
# them — so copying them is safer than importing a private module that may move.
_ERROR = "__error__"
_ERROR_SOURCE_NODE = "__error_source_node__"
_INTERRUPT = "__interrupt__"
_RESUME = "__resume__"
#: Writes that can sit beside an ``__interrupt__`` without recording whether the task is still
#: paused. Any other write is a result, and the task is no longer waiting.
_UNDECIDED_WITH_INTERRUPT = frozenset({_INTERRUPT, _RESUME, _ERROR, _ERROR_SOURCE_NODE})
_START = "__start__"
_TASKS = "__pregel_tasks"
_PULL = "__pregel_pull"
_PUSH = "__pregel_push"
#: Writes LangGraph makes under this sentinel id belong to the graph, not to any node task.
_NULL_TASK_ID = "00000000-0000-0000-0000-000000000000"
#: Checkpoints at or below this version hash task ids with SHA-1; later ones use xxh3-128.
_MAX_LEGACY_CHECKPOINT_V = 1
#: When one task raises, LangGraph cancels its siblings and records a ``CancelledError`` for
#: each under the *same* ``__error__`` channel as a genuine failure (``pregel/_runner.commit``
#: handles cancellation in its own branch, before real exceptions). Reading them alike blames
#: healthy nodes for a neighbour's failure — the exact misattribution this module exists to
#: avoid — so cancellation is matched by exception class and reported as an outcome that never
#: happened, not as a failure. Both ``asyncio`` and ``concurrent.futures`` spellings repr this
#: way; a user exception of the same name would be under-reported, never over-reported.
_CANCELLED_PREFIX = "CancelledError("
#: Distinguishes "this task has no __error__ write" from "__error__ was persisted as None",
#: which a legacy serializer really does produce for a revived exception class.
_MISSING = object()


def _looks_like_tool_node(name: str) -> bool:
    """True iff ``name`` names a tool node — has ``tool``/``tools`` as a *whole token*.

    The old test ``"tool" in name.lower()`` matched any name *containing* the substring, so
    ``retool`` / ``toolbar`` / ``stool`` wrongly became tools and leaked ``kind=TOOL`` into
    pattern queries. We tokenize instead — split on non-alphanumeric separators, then on
    camelCase / acronym boundaries — and match ``tool`` / ``tools`` as a *whole token*:
    ``tool``, ``tools``, ``call_tool``, ``run_tools``, ``tool_node``, ``ToolNode`` and the
    all-caps ``TOOL`` / ``CALL_TOOL`` all classify; ``retool`` does not.

    This is deliberately stricter than the old substring test, so it does *not* match every
    name the old check did: a name where ``tool`` is glued into a larger all-lowercase word
    (``toolnode``, ``runtool``) or an all-caps run fused to a CamelWord (``TOOLs``) is not a
    tool here. That's an accepted limitation — real LangGraph tool nodes use separated or
    camelCase names (``tools``, ``call_tool``, ``ToolNode``). ASCII letters only.
    """
    tokens: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        # Split into tokens, handling: an acronym run before a CamelWord ("HTTPTool" ->
        # ["HTTP", "Tool"]); a Capitalized/lowercase word ("Tool", "tool"); an all-caps run
        # with no trailing lowercase ("TOOL", "CALL"); and digit runs.
        tokens.extend(re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", part))
    return any(t.lower() in _TOOL_TOKENS for t in tokens)


_SOURCE_MAP = {
    "input": StepSource.INPUT,
    "loop": StepSource.LOOP,
    "update": StepSource.UPDATE,
    "fork": StepSource.FORK,
}
_CheckpointKey = tuple[str, str]


class LangGraphCheckpointAdapter:
    def __init__(
        self,
        saver: Any,
        *,
        error_channel: str = "error",
        source_kind: str = "langgraph",
        schedule_graph: Any = None,
        include_error_messages: bool = True,
    ) -> None:
        self._saver = saver
        self._error_channel = error_channel
        self._source_kind = source_kind
        #: A compiled LangGraph graph to ask which tasks are scheduled at the head; see the
        #: module docstring. ``None`` leaves scheduled-but-unwritten tasks unreported.
        self._schedule_graph = schedule_graph
        #: Off, a failed step keeps its ERROR status but not the error channel's value or the
        #: exception text, and lists ``error_msg`` as unknown, as OTLP's option of that name does.
        self._include_error_messages = include_error_messages

    # --- TraceAdapter ---

    def discover(self) -> list[str]:
        """Distinct thread ids known to the saver (best effort; not all savers support it)."""
        list_fn = getattr(self._saver, "list", None)
        if list_fn is None:
            raise NotImplementedError(
                "this checkpointer does not support listing all threads"
            )
        try:
            listing = list_fn(None)
        except NotImplementedError as exc:
            # Only the documented capability signal becomes "unsupported", and only when the
            # call itself raises it. The same exception raised while iterating is the
            # serializer failing to decode a row, which `_decoded` reports as corrupt input.
            raise NotImplementedError(
                "this checkpointer does not support listing all threads"
            ) from exc
        seen: set[str] = set()
        ordered: list[str] = []
        for t in _decoded(listing):
            tid = _checked(t)[0]["thread_id"]
            if tid is not None and tid not in seen:
                seen.add(tid)
                ordered.append(tid)
        return ordered

    def ingest(self, trace_id: str) -> RawTrace:
        config = {"configurable": {"thread_id": trace_id}}
        tuples = [
            t
            for t in _decoded(self._saver.list(config))
            if _checked(t)[0]["thread_id"] == trace_id
        ]
        if not tuples:
            raise KeyError(f"no checkpoints for thread_id {trace_id!r}")

        checkpoints: dict[str, dict] = {}
        step_id_by_key: dict[_CheckpointKey, str] = {}
        key_by_step_id: dict[str, _CheckpointKey] = {}
        parent_keys: dict[str, list[_CheckpointKey]] = {}
        original_seq: dict[str, int] = {}
        steps: dict[str, Step] = {}
        writes_by_step: dict[str, dict[str, list[tuple[str, Any]]]] = {}
        launch_keys: dict[str, list[_CheckpointKey]] = {}
        malformed_writes = 0
        for t in tuples:
            cfg, cp, md, parent_cfg = _checked(t)
            ns = cfg.get("checkpoint_ns", _ROOT_NS) or _ROOT_NS
            cid = cp["id"]
            key = (ns, cid)
            if key in step_id_by_key:
                raise ValueError(f"duplicate checkpoint {cid!r} in namespace {ns!r}")
            step_id = self._step_id(ns, cid)
            if step_id in steps:
                raise ValueError(
                    f"checkpoint namespace/id collision produced duplicate step_id {step_id!r}"
                )
            step_id_by_key[key] = step_id
            key_by_step_id[step_id] = key
            checkpoints[step_id] = cp
            original_seq[step_id] = md.get("step", 0)
            parent_keys[step_id] = self._declared_parent_keys(parent_cfg, md, ns)
            launch_keys[step_id] = self._launch_parent_keys(md, ns)
            writes_by_step[step_id], malformed = self._writes_by_task(
                getattr(t, "pending_writes", None)
            )
            malformed_writes += malformed
            steps[step_id] = Step(
                step_id=step_id,
                trace_id=trace_id,
                seq=original_seq[step_id],
                ts=cp.get("ts"),
                source=_SOURCE_MAP.get(md.get("source", "loop"), StepSource.LOOP),
                kind=StepKind.CHAIN,
                status=StepStatus.OK,
            )

        chrono_rank = self._chrono_rank(checkpoints)
        causal_parent_keys = self._with_subgraph_exit_edges(
            parent_keys,
            key_by_step_id,
            step_id_by_key,
            chrono_rank,
        )

        parents_by_step: dict[str, list[str]] = {}
        display_parent_by_step: dict[str, str] = {}
        for step_id, refs in causal_parent_keys.items():
            for parent_key in refs:
                parent_id = step_id_by_key.get(parent_key)
                if parent_id is None:
                    ns, cid = parent_key
                    child_ns, child_cid = key_by_step_id[step_id]
                    raise ValueError(
                        f"checkpoint {child_cid!r} in namespace {child_ns!r} declares "
                        f"missing parent checkpoint {cid!r} in namespace {ns!r}; the "
                        "checkpoint history is partial/corrupt"
                    )
                parents_by_step.setdefault(step_id, []).append(parent_id)

        for step_id, refs in parent_keys.items():
            if refs:
                display_parent_id = step_id_by_key.get(refs[0])
                if display_parent_id is not None:
                    display_parent_by_step[step_id] = display_parent_id

        if not self._metadata_seq_is_valid(original_seq, parents_by_step):
            global_seq = self._topo_seq(steps, parents_by_step, original_seq)
            for step_id, seq in global_seq.items():
                steps[step_id].seq = seq

        edges = [
            Edge(
                type=EdgeType.CAUSED_BY,
                src=step_id,
                dst=parent_id,
                origin=EdgeOrigin.CHECKPOINT_PARENT,
            )
            for step_id, parents in parents_by_step.items()
            for parent_id in parents
        ]

        rewritten_errors = 0
        for step_id, step in steps.items():
            display_parent_id = display_parent_by_step.get(step_id)
            parent_cp = checkpoints.get(display_parent_id) if display_parent_id else None
            display_parent_key = key_by_step_id.get(display_parent_id) if display_parent_id else None
            branch_target = self._branch_target(display_parent_key, key_by_step_id[step_id])
            name = self._producing_node(parent_cp, branch_target)
            if name:
                step.name = name
                if _looks_like_tool_node(name):
                    step.kind = StepKind.TOOL
            no_producer = self._has_no_producing_node(step, steps.get(display_parent_id or ""))
            if not no_producer:
                # A checkpoint does not record which node wrote it. The name is read off the
                # parent's routing channels — the first of several when nodes ran in parallel,
                # none for a Send or an update — and the kind is inferred from that name, so
                # both stay display labels and are recorded as unknown, named or not. Task,
                # interrupt and scheduled steps are named by LangGraph's own task ids instead.
                step.unknown_fields = ["kind", "name"]
            # An error is new here only if none of the states this checkpoint was built from held
            # one, and the causal parents alone can miss them: exit-edge expansion replaces a
            # same-namespace predecessor with a subgraph's exit, and the input checkpoint a
            # ``__start__`` applies holds its values as pending writes, so that input's own
            # parents (the namespace's previous invocation, and the checkpoint that launched the
            # subgraph) stand in. A fork (``update_state(..., as_node="__copy__")``) copies an
            # existing checkpoint and runs no node, so whatever error it holds is not new; it is
            # linked to the copied checkpoint's parent, which need not hold that error.
            upstream_keys = list(parent_keys.get(step_id, []))
            if no_producer and step.source is StepSource.LOOP and display_parent_id is not None:
                upstream_keys += [*parent_keys.get(display_parent_id, []),
                                  *launch_keys.get(display_parent_id, [])]
            upstream_ids = [*parents_by_step.get(step_id, []),
                            *(pid for key in upstream_keys if (pid := step_id_by_key.get(key)))]
            error_parent_cps = [checkpoints[pid] for pid in dict.fromkeys(upstream_ids)]
            err = (None if step.source is StepSource.FORK
                   else self._error_introduced(checkpoints[step_id], error_parent_cps))
            # Only a checkpoint a node wrote can disclose a rewrite: an input, an update and a
            # fork (``update_state(..., as_node="__copy__")`` copies state, linked to the copied
            # checkpoint's parent) carry what the caller supplied. The channel's own history is
            # its declared parents in the same namespace, not the expanded causal parents, which
            # replace a predecessor with the exits of subgraphs that ran alongside.
            if err is None and step.source is StepSource.LOOP and not no_producer:
                ns = key_by_step_id[step_id][0]
                history = [checkpoints[pid] for key in parent_keys.get(step_id, [])
                           if key[0] == ns and (pid := step_id_by_key.get(key)) is not None]
                rewritten_errors += self._rewrote_error(checkpoints[step_id], history)
            if err is not None:
                step.status = StepStatus.ERROR
                if self._include_error_messages:
                    step.error_msg = err
                else:
                    step.unknown_fields.insert(0, "error_msg")  # sorts before kind and name

        # Derived task steps come last: everything above indexes `steps` by checkpoint id
        # (`checkpoints[step_id]`, `key_by_step_id[step_id]`), and a derived id has no entry
        # in either. Building them here also means they inherit each carrying step's *final*
        # seq, after `_topo_seq` may have re-sequenced the whole thread.
        disclosures = self._derive_task_steps(
            trace_id, steps, checkpoints, key_by_step_id, original_seq, writes_by_step, edges,
            include_error_messages=self._include_error_messages,
        )
        disclosures.extend(
            self._derive_interrupt_steps(
                trace_id, steps, checkpoints, key_by_step_id, original_seq, writes_by_step, edges
            )
        )
        schedule_graph = None
        if self._schedule_graph is not None:
            from tracegraph.graph_ref import fingerprint

            disclosures.extend(
                self._derive_scheduled_steps(
                    trace_id, steps, checkpoints, key_by_step_id, writes_by_step, edges
                )
            )
            schedule_graph = ScheduleGraph(fingerprint=fingerprint(self._schedule_graph))
        if malformed_writes:
            disclosures.add(
                "langgraph.malformed_pending_writes",
                f"{malformed_writes} pending write(s) are not a (task id, channel, value) "
                "triple and were ignored; a task failure recorded in one would not be reported.",
                malformed_writes,
            )
        if rewritten_errors:
            disclosures.add(
                "langgraph.rewritten_error_channel",
                f"{rewritten_errors} checkpoint(s) wrote the error channel while it already held "
                "an error; a repeated failure there cannot be told from a node passing the state "
                "through, so none was recorded.",
                rewritten_errors,
            )

        any_error = any(s.status is StepStatus.ERROR for s in steps.values())
        trace = Trace(
            trace_id=trace_id,
            source_kind=self._source_kind,
            thread_id=trace_id,
            status=StepStatus.ERROR if any_error else StepStatus.OK,
            causal_fidelity=CausalFidelity.DECLARED_DAG,
            schedule_graph=schedule_graph,
            capture=disclosures.capture(),
        )
        return RawTrace(
            trace=trace,
            ingest_warnings=list(disclosures.texts),
            steps=sorted(steps.values(), key=lambda s: (s.seq, s.step_id)),
            causal_edges=edges,
        )

    # --- helpers ---

    @staticmethod
    def _step_id(ns: str, cid: str) -> str:
        return cid if ns == _ROOT_NS else f"{ns}:{cid}"

    # --- native task failures (pending writes) ---

    @classmethod
    def _derive_task_steps(
        cls,
        trace_id: str,
        steps: dict[str, Step],
        checkpoints: dict[str, dict],
        key_by_step_id: dict[str, _CheckpointKey],
        original_seq: dict[str, int],
        writes_by_step: dict[str, dict[str, list[tuple[str, Any]]]],
        edges: list[Edge],
        *,
        include_error_messages: bool,
    ) -> Disclosures:
        """Add one ERROR step per failed task and return any ingestion disclosures.

        The failing task becomes a child of the checkpoint that scheduled it. Marking that
        checkpoint instead would blame the wrong node: its name is the node that *produced*
        it, so a ``call_tool`` exception would be reported against ``plan``.
        """
        disclosures = Disclosures()
        derived: list[tuple[str, Step]] = []
        derived_ids: set[str] = set()
        for step_id in sorted(steps, key=lambda sid: (steps[sid].seq, sid)):
            writes = writes_by_step.get(step_id) or {}
            cp = checkpoints[step_id]
            ns, cid = key_by_step_id[step_id]
            undecodable = cls._undecodable_sends(cp)
            if undecodable:
                disclosures.add(
                    "langgraph.undecodable_send_packets",
                    f"checkpoint {cid}: {undecodable} Send packet(s) could not be decoded "
                    "(langgraph is not installed), so a failure in one cannot be named.",
                    undecodable,
                )
            for task_id, task_writes in writes.items():
                if task_id == _NULL_TASK_ID:
                    continue  # a graph-level write (e.g. a top-level resume), not a node task
                error = next(
                    (value for channel, value in task_writes if channel == _ERROR), _MISSING
                )
                if error is _MISSING:
                    continue
                name = cls._resolve_task_name(
                    cp, ns, original_seq[step_id], task_id, task_writes
                )
                message = cls._error_message(error)
                cancelled = message.startswith(_CANCELLED_PREFIX)
                if name is None and not cancelled:
                    disclosures.add(
                        "langgraph.unattributed_failed_task",
                        f"checkpoint {cid}: failed task {task_id} could not be attributed to a "
                        "node; no candidate reproduced its task id, which is also what a change "
                        "in LangGraph's task-id algorithm would look like.",
                    )
                unknown: list[UnknownField] = [] if name else ["kind", "name"]
                if (error is None or not include_error_messages) and not cancelled:
                    # _error_message's placeholder (the checkpointer kept no exception text),
                    # or a message withheld on request.
                    unknown.insert(0, "error_msg")
                derived_id = f"{step_id}#task:{task_id}"
                if derived_id in steps or derived_id in derived_ids:
                    # Checkpoint ids are unrestricted strings, so this is conceivable. The
                    # duplicate guard above only covers real checkpoints, and silently
                    # overwriting one would delete a genuine step.
                    raise ValueError(
                        f"derived task step id {derived_id!r} collides with a checkpoint id "
                        "or another derived step"
                    )
                derived_ids.add(derived_id)
                derived.append((
                    step_id,
                    Step(
                        step_id=derived_id,
                        trace_id=trace_id,
                        seq=steps[step_id].seq + 1,
                        ts=steps[step_id].ts,
                        kind=StepKind.TOOL if name and _looks_like_tool_node(name) else StepKind.CHAIN,
                        source=StepSource.TASK,
                        name=name,
                        # A cancelled task produced no outcome, good or bad. UNSET says that
                        # without claiming it failed, and leaves the trace status to the task
                        # that actually raised.
                        status=StepStatus.UNSET if cancelled else StepStatus.ERROR,
                        error_msg=None if cancelled or not include_error_messages else message,
                        unknown_fields=unknown,
                    ),
                ))
        for parent_step_id, step in derived:
            steps[step.step_id] = step
            edges.append(
                Edge(
                    type=EdgeType.CAUSED_BY,
                    src=step.step_id,
                    dst=parent_step_id,
                    # The write is keyed to that checkpoint's id by LangGraph itself, so the
                    # link is declared, not inferred from timing or namespace membership.
                    origin=EdgeOrigin.CHECKPOINT_PARENT,
                )
            )
        return disclosures

    @classmethod
    def _derive_interrupt_steps(
        cls,
        trace_id: str,
        steps: dict[str, Step],
        checkpoints: dict[str, dict],
        key_by_step_id: dict[str, _CheckpointKey],
        original_seq: dict[str, int],
        writes_by_step: dict[str, dict[str, list[tuple[str, Any]]]],
        edges: list[Edge],
    ) -> Disclosures:
        """Add one UNSET step per task the head checkpoint records as interrupted, never resumed.

        Only the thread's head — the newest root checkpoint, which is what LangGraph itself
        resumes from — is read. An ``__interrupt__`` write on any earlier checkpoint was resumed,
        forked past, or abandoned, and is history rather than an open pause.

        A task is reported only when ``__interrupt__`` is its *only* write. Anything else that
        accompanies it leaves the task's state unrecorded, and it is disclosed rather than
        guessed: under LangGraph 1.2.2 a resumed task that returned nothing leaves
        ``{__interrupt__, __resume__}``, exactly like one that reached a second ``interrupt()``
        (LangGraph overwrites the interrupt in place; 1.2.15 adds ``__no_writes__`` to the
        first, which then reads as finished); a failure followed by a retry that interrupts leaves the same
        ``{__error__, __interrupt__}`` as the reverse order, because savers keep no order.
        """
        head_id = cls._head_step_id(checkpoints, key_by_step_id)
        if head_id is None:
            return Disclosures()
        head = steps[head_id]
        cp = checkpoints[head_id]
        _, cid = key_by_step_id[head_id]
        disclosures = Disclosures()
        undecided = 0
        for task_id, task_writes in (writes_by_step.get(head_id) or {}).items():
            if task_id == _NULL_TASK_ID:
                continue
            channels = {channel for channel, _ in task_writes}
            if _INTERRUPT not in channels:
                continue
            if channels != {_INTERRUPT}:
                if channels <= _UNDECIDED_WITH_INTERRUPT:
                    undecided += 1
                continue
            name = cls._resolve_task_name(cp, _ROOT_NS, original_seq[head_id], task_id, task_writes)
            if name is None:
                disclosures.add(
                    "langgraph.unattributed_interrupted_task",
                    f"checkpoint {cid}: interrupted task {task_id} could not be attributed to a "
                    "node; no candidate reproduced its task id, which is also what a change in "
                    "LangGraph's task-id algorithm would look like.",
                )
            derived_id = f"{head_id}#interrupt:{task_id}"
            if derived_id in steps:
                raise ValueError(
                    f"derived interrupt step id {derived_id!r} collides with a checkpoint id "
                    "or another derived step"
                )
            steps[derived_id] = Step(
                step_id=derived_id,
                trace_id=trace_id,
                seq=head.seq + 1,
                ts=head.ts,
                kind=StepKind.TOOL if name and _looks_like_tool_node(name) else StepKind.CHAIN,
                source=StepSource.INTERRUPT,
                name=name,
                # The task has no outcome yet. The interrupt's value is whatever the node asked
                # its caller, which is user content, so it is not carried as a message.
                status=StepStatus.UNSET,
                unknown_fields=[] if name else ["kind", "name"],
            )
            edges.append(
                Edge(
                    type=EdgeType.CAUSED_BY,
                    src=derived_id,
                    dst=head_id,
                    # LangGraph keys the write to the head checkpoint itself.
                    origin=EdgeOrigin.CHECKPOINT_PARENT,
                )
            )
        if undecided:
            disclosures.add(
                "langgraph.undecided_interrupt",
                f"checkpoint {cid}: {undecided} task(s) recorded an interrupt together with a "
                "resume or an error and no result; the checkpoint does not record whether each "
                "is still paused, so none is reported as interrupted.",
                undecided,
            )
        return disclosures

    @staticmethod
    def _head_step_id(
        checkpoints: dict[str, dict], key_by_step_id: dict[str, _CheckpointKey]
    ) -> str | None:
        """The newest root-namespace checkpoint: what LangGraph resumes from and ``get_state``
        reads. ``None`` for a history with no root checkpoint."""
        root_ids = [sid for sid, (ns, _) in key_by_step_id.items() if ns == _ROOT_NS]
        if not root_ids:
            return None
        return max(root_ids, key=lambda sid: checkpoints[sid]["id"])

    # --- scheduled tasks (a supplied compiled graph) ---

    def _derive_scheduled_steps(
        self,
        trace_id: str,
        steps: dict[str, Step],
        checkpoints: dict[str, dict],
        key_by_step_id: dict[str, _CheckpointKey],
        writes_by_step: dict[str, dict[str, list[tuple[str, Any]]]],
        edges: list[Edge],
    ) -> Disclosures:
        """Add one UNSET step per task the supplied graph schedules at the head with no write.

        The graph is bound to this adapter's saver with its own config and store cleared, and
        asked for exactly the head checkpoint: a graph carrying ``with_config`` for an older
        checkpoint would otherwise read history instead. Whatever the graph's callbacks raise
        while LangGraph reads the checkpoint, ``SystemExit`` included, refuses the ingest as a
        ``ValueError``; ``KeyboardInterrupt`` propagates.
        """
        head_id = self._head_step_id(checkpoints, key_by_step_id)
        if head_id is None:
            return Disclosures()
        head = steps[head_id]
        cp = checkpoints[head_id]
        _, cid = key_by_step_id[head_id]
        graph = self._schedule_graph
        bound = graph.copy(update={"checkpointer": self._saver, "store": None, "config": None})
        config = {
            "configurable": {"thread_id": trace_id, "checkpoint_ns": _ROOT_NS, "checkpoint_id": cid}
        }
        try:
            snapshot = bound.get_state(config)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            raise ValueError(
                f"the supplied graph could not read checkpoint {cid}: {describe(exc)}"
            ) from exc
        read = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
        if read != cid:
            raise ValueError(
                f"the supplied graph read checkpoint {read!r} instead of the head {cid!r}"
            )

        head_writes = writes_by_step.get(head_id) or {}
        for task in snapshot.tasks:
            if task.id in head_writes:
                continue  # finished, failed or paused: the paths above read its writes
            derived_id = f"{head_id}#scheduled:{task.id}"
            if derived_id in steps:
                raise ValueError(
                    f"derived scheduled step id {derived_id!r} collides with a checkpoint id "
                    "or another derived step"
                )
            steps[derived_id] = Step(
                step_id=derived_id,
                trace_id=trace_id,
                seq=head.seq + 1,
                ts=head.ts,
                kind=StepKind.TOOL if _looks_like_tool_node(task.name) else StepKind.CHAIN,
                source=StepSource.SCHEDULED,
                name=task.name,
                status=StepStatus.UNSET,
            )
            edges.append(
                Edge(
                    type=EdgeType.CAUSED_BY,
                    src=derived_id,
                    dst=head_id,
                    # LangGraph schedules the task from the head checkpoint itself.
                    origin=EdgeOrigin.CHECKPOINT_PARENT,
                )
            )

        disclosures = Disclosures()
        triggers = {str(t) for node in graph.nodes.values() for t in node.triggers} | {_TASKS}
        unapplied = sum(
            1 for channel, _ in head_writes.get(_NULL_TASK_ID, ()) if channel in triggers
        )
        if unapplied:
            disclosures.add(
                "langgraph.unapplied_command_goto",
                f"checkpoint {cid}: {unapplied} input write(s) (a Command goto) wait to be "
                "applied when a run resumes from this checkpoint; LangGraph does not list the "
                "tasks they would start, so none is reported as scheduled.",
                unapplied,
            )
        packets = self._send_packets(graph, cp)
        unknown = sorted(
            {
                node
                for node in (getattr(packet, "node", None) for packet in packets)
                if isinstance(node, str) and node not in graph.nodes
            }
        )
        if unknown:
            count = sum(1 for packet in packets if getattr(packet, "node", None) in unknown)
            disclosures.add(
                "langgraph.unknown_send_target",
                f"checkpoint {cid}: {count} Send packet(s) address node(s) the supplied graph "
                f"does not have ({', '.join(unknown)}); LangGraph schedules nothing for them, "
                "so none is reported as scheduled.",
                count,
            )
        return disclosures

    @staticmethod
    def _send_packets(graph: Any, cp: dict) -> list[Any]:
        """The checkpoint's pending ``Send`` packets, decoded by the graph's own tasks channel.

        LangGraph's channel knows every layout it has written (its ``from_checkpoint`` still
        accepts an older tuple encoding), so it decodes them here rather than a copy of that
        knowledge. A checkpoint older than version 4 kept them in ``pending_sends`` instead.
        """
        packets = list(cp.get("pending_sends") or ())
        channel = graph.channels.get(_TASKS)
        values = cp.get("channel_values", {})
        if channel is not None and _TASKS in values:
            restored = channel.from_checkpoint(values[_TASKS])
            if restored.is_available():
                packets.extend(restored.get())
        return packets

    @staticmethod
    def _writes_by_task(pending_writes: Any) -> tuple[dict[str, list[tuple[str, Any]]], int]:
        """Group a checkpoint's pending writes by task id, in first-seen order.

        Savers are free to order writes as they like — the SQLite saver sorts by task id and
        index, the in-memory one keeps insertion order — so nothing here may depend on
        position. ``pending_writes`` is also optional on ``CheckpointTuple``; a third-party
        saver that omits it reads as "no writes", never as an error. Returns the grouping and
        how many entries were not a ``(str, str, value)`` triple: those are not ours to
        interpret, but the caller discloses them because one could have carried a failure.
        """
        grouped: dict[str, list[tuple[str, Any]]] = {}
        malformed = 0
        for write in pending_writes or ():
            if not isinstance(write, (tuple, list)) or len(write) != 3:
                malformed += 1
                continue
            task_id, channel, value = write
            if isinstance(task_id, str) and isinstance(channel, str):
                grouped.setdefault(task_id, []).append((channel, value))
            else:
                malformed += 1
        return grouped, malformed

    @staticmethod
    def _hash_task_id(cid: str, legacy: bool, *parts: str) -> str | None:
        """Recompute a LangGraph task id, or ``None`` if this checkpoint id cannot carry one.

        Mirrors ``_xxhash_str`` / ``_uuid5_str`` in ``langgraph.pregel._algo``. A checkpoint id
        that is not hex (a hand-built test stub, a third-party saver) simply yields ``None``:
        the caller then leaves the step unnamed instead of comparing against a value LangGraph
        could not have produced.
        """
        try:
            seed = unhexlify(cid.replace("-", ""))
        except (BinasciiError, AttributeError, ValueError):
            return None
        try:
            joined = b"".join(part.encode() for part in parts)
        except UnicodeEncodeError:
            # A lone surrogate has no UTF-8 encoding, so LangGraph could not have hashed this
            # candidate into any recorded task id. It names nothing; it must not end the ingest.
            return None
        if legacy:
            digest = sha1(seed, usedforsecurity=False)
            digest.update(joined)
            hexed = digest.hexdigest()
        else:
            from xxhash import xxh3_128_hexdigest

            hexed = xxh3_128_hexdigest(seed + joined)
        return f"{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:32]}"

    @staticmethod
    def _trigger_targets(cp: dict) -> dict[str, set[str]]:
        """Node name -> every trigger channel in this checkpoint that targets it.

        Both ``channel_values`` and ``channel_versions`` are read: a consumed ``branch:to:``
        channel disappears from the values but keeps its version, and a barrier node's
        implicit branch channel may be absent from both (it is added by the caller).
        """
        targets: dict[str, set[str]] = {}
        for channel in (*cp.get("channel_values", {}), *cp.get("channel_versions", {})):
            if not isinstance(channel, str):
                continue
            if channel.startswith(_BRANCH_PREFIX):
                name = channel[len(_BRANCH_PREFIX):]
            elif channel.startswith(_JOIN_PREFIX):
                # ``join:<src1>+<src2>:<target>`` — the target is the last segment.
                name = channel.rpartition(":")[2]
            else:
                continue
            if name:
                targets.setdefault(name, set()).add(channel)
        return targets

    @classmethod
    def _candidate_names(cls, cp: dict) -> list[str]:
        """Node names this checkpoint mentions, as naming candidates.

        Only names LangGraph itself recorded are considered. A name that never appears cannot
        be proposed, and a name that appears but does not hash to the recorded task id is
        rejected — so the candidate list only ever affects *coverage*, never correctness.
        """
        names = set(cls._trigger_targets(cp))
        names.update(
            name for name in cp.get("versions_seen", {}) if isinstance(name, str)
        )
        names.add(_START)
        return sorted(names)

    @classmethod
    def _pull_task_ids(cls, cp: dict, ns: str, md_step: int, name: str) -> list[str]:
        """Every task id node ``name`` could have at this checkpoint, by trigger set.

        LangGraph hashes a node's *configured* triggers, which are not always visible here: a
        fan-in node is triggered by both its barrier channel and an implicit ``branch:to:``
        channel that the checkpoint may never mention. Rather than reconstruct the compiled
        graph, this tries the plausible sets. Trying several is safe because a match is the
        evidence — a trigger set that is not the node's real one simply does not match.
        """
        legacy = cp.get("v", 0) <= _MAX_LEGACY_CHECKPOINT_V
        task_ns = f"{ns}|{name}" if ns else name
        step = str(md_step + 1)
        if name == _START:
            trigger_sets: list[tuple[str, ...]] = [(_START,)]
        else:
            branch = f"{_BRANCH_PREFIX}{name}"
            observed = cls._trigger_targets(cp).get(name, set())
            configured = tuple(sorted(observed | {branch}))
            trigger_sets = [configured]
            if observed and configured != tuple(sorted(observed)):
                trigger_sets.append(tuple(sorted(observed)))
            if (branch,) not in trigger_sets:
                trigger_sets.append((branch,))
        ids = []
        for triggers in trigger_sets:
            task_id = cls._hash_task_id(cp["id"], legacy, task_ns, step, name, _PULL, *triggers)
            if task_id is not None:
                ids.append(task_id)
        return ids

    @classmethod
    def _push_task_ids(cls, cp: dict, ns: str, md_step: int) -> dict[str, str]:
        """Task id -> node name for this checkpoint's ``Send`` packets.

        The packet index is part of the identity, so two ``Send``s to one node are two tasks.
        Packets only decode when ``langgraph.types`` is importable: the checkpoint serializer
        revives them through it and yields ``None`` otherwise. An ordinary install has only
        the ``langgraph.checkpoint`` half of that namespace, so seeing ``None`` here is the
        normal case, not a corrupt checkpoint — it is disclosed by the caller, never guessed
        around, because a ``Send`` whose node is unknown cannot name anything.
        """
        packets = cp.get("channel_values", {}).get(_TASKS)
        if not isinstance(packets, (list, tuple)):
            return {}
        legacy = cp.get("v", 0) <= _MAX_LEGACY_CHECKPOINT_V
        out: dict[str, str] = {}
        for idx, packet in enumerate(packets):
            node = getattr(packet, "node", None)
            if not isinstance(node, str):
                continue  # undecodable packet; _undecodable_sends reports it
            task_ns = f"{ns}|{node}" if ns else node
            task_id = cls._hash_task_id(
                cp["id"], legacy, task_ns, str(md_step + 1), node, _PUSH, str(idx)
            )
            if task_id is not None:
                out[task_id] = node
        return out

    @staticmethod
    def _undecodable_sends(cp: dict) -> int:
        """How many ``Send`` packets this checkpoint holds that did not deserialize."""
        packets = cp.get("channel_values", {}).get(_TASKS)
        if not isinstance(packets, (list, tuple)):
            return 0
        return sum(1 for packet in packets if not isinstance(getattr(packet, "node", None), str))

    @classmethod
    def _resolve_task_name(
        cls, cp: dict, ns: str, md_step: int, task_id: str, writes: list[tuple[str, Any]]
    ) -> str | None:
        """Name the node that ran as ``task_id``, or ``None`` when nothing proves which it was."""
        for channel, value in writes:
            if channel == _ERROR_SOURCE_NODE and isinstance(value, str) and value:
                return value
        for name in cls._candidate_names(cp):
            if task_id in cls._pull_task_ids(cp, ns, md_step, name):
                return name
        return cls._push_task_ids(cp, ns, md_step).get(task_id)

    @staticmethod
    def _error_message(value: Any) -> str:
        """The exception text LangGraph persisted, as a string.

        The serializer stores ``repr(exc)`` for an exception; a legacy ``lc:2`` envelope
        instead revives the class as ``None``. Neither is a body — it is the exception text
        the run itself would have printed — but "None" would read as a message, so an absent
        value is labelled as absent.
        """
        if value is None:
            return "<exception not serialized by the checkpointer>"
        return str(value)

    @staticmethod
    def _declared_parent_keys(parent_cfg: dict, md: dict, ns: str) -> list[_CheckpointKey]:
        parent_id = parent_cfg.get("checkpoint_id")
        if parent_id is not None:
            parent_ns = parent_cfg.get("checkpoint_ns", ns) or _ROOT_NS
            return [(parent_ns, parent_id)]

        return LangGraphCheckpointAdapter._launch_parent_keys(md, ns)

    @staticmethod
    def _launch_parent_keys(md: dict, ns: str) -> list[_CheckpointKey]:
        """The closest enclosing-graph checkpoint named in ``metadata.parents``: for a subgraph,
        the checkpoint it was launched from, recorded even when ``parent_config`` points at
        the namespace's previous invocation instead."""
        # `_checked` has already established `parents` as a str -> str mapping.
        parents = md.get("parents") or {}
        closest = LangGraphCheckpointAdapter._closest_parent_ns(ns, parents)
        if closest is None:
            return []
        return [(closest or _ROOT_NS, parents[closest])]

    @staticmethod
    def _closest_parent_ns(ns: str, parents: dict[str, str]) -> str | None:
        candidates = [
            parent_ns or _ROOT_NS
            for parent_ns in parents
            if parent_ns == _ROOT_NS or ns.startswith(f"{parent_ns}|")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda parent_ns: (parent_ns.count("|"), len(parent_ns)))

    @staticmethod
    def _chrono_rank(checkpoints: dict[str, dict]) -> dict[str, int]:
        return {
            step_id: i
            for i, step_id in enumerate(
                sorted(
                    checkpoints,
                    key=lambda sid: (
                        checkpoints[sid].get("id") or sid,
                        checkpoints[sid].get("ts") or "",
                        sid,
                    ),
                )
            )
        }

    @staticmethod
    def _with_subgraph_exit_edges(
        parent_keys: dict[str, list[_CheckpointKey]],
        key_by_step_id: dict[str, _CheckpointKey],
        step_id_by_key: dict[_CheckpointKey, str],
        chrono_rank: dict[str, int],
    ) -> dict[str, list[_CheckpointKey]]:
        # Group each namespace's checkpoints into *invocations*, in chronological order.
        #
        # A namespace can be entered more than once (a loop re-running the same subgraph
        # node). Pairing one entry with one terminal per namespace is then wrong in both
        # directions: keeping the last entry (which is what raw saver.list() order happened
        # to give, since it pages newest-first) or the first entry both leave the terminal
        # belonging to a *different* invocation, and the parent graph's continuation edge for
        # every other invocation is dropped. So an entry and the terminal it resumes from
        # must come from the same invocation.
        #
        # A step whose parent lies outside the namespace *is* an entry, and so begins a new
        # invocation; the invocation's terminal is its chronologically last member.
        members_by_ns: dict[str, list[str]] = {}
        for step_id, key in sorted(
            key_by_step_id.items(), key=lambda item: (chrono_rank[item[0]], item[0])
        ):
            ns, _ = key
            if ns != _ROOT_NS:
                members_by_ns.setdefault(ns, []).append(step_id)

        exits_by_entry_parent: dict[_CheckpointKey, list[_CheckpointKey]] = {}
        for ns, member_ids in members_by_ns.items():
            invocations: list[tuple[_CheckpointKey, list[str]]] = []
            pending: list[str] = []
            for step_id in member_ids:
                # Only a parent *outside and above* this namespace is an entry. A parent in a
                # descendant namespace is the opposite: the subgraph this namespace launched
                # handing control back, which continues the invocation already in progress.
                # Treating that return as a new entry would close the invocation early, and
                # the parent graph would then resume from a checkpoint before the nested work
                # instead of after it, dropping that whole execution from the ancestry.
                entry_parent = next(
                    (
                        key
                        for key in parent_keys.get(step_id, [])
                        if key[0] != ns and not key[0].startswith(f"{ns}|")
                    ),
                    None,
                )
                if entry_parent is None:
                    # Not an entry: it continues the invocation in progress. Anything seen
                    # before the first entry is held over and joins it.
                    (invocations[-1][1] if invocations else pending).append(step_id)
                else:
                    invocations.append((entry_parent, [*pending, step_id]))
                    pending = []
            for entry_parent, members in invocations:
                terminal = max(members, key=lambda sid: (chrono_rank[sid], sid))
                exits_by_entry_parent.setdefault(entry_parent, []).append(
                    key_by_step_id[terminal]
                )

        out: dict[str, list[_CheckpointKey]] = {}
        for step_id, refs in parent_keys.items():
            step_ns, _ = key_by_step_id[step_id]
            expanded: list[_CheckpointKey] = []
            seen: set[_CheckpointKey] = set()
            for ref in refs:
                exit_refs = [
                    exit_ref
                    for exit_ref in exits_by_entry_parent.get(ref, [])
                    if ref[0] == step_ns
                    and chrono_rank[step_id_by_key[exit_ref]] < chrono_rank[step_id]
                ]
                replacements = sorted(
                    exit_refs,
                    key=lambda exit_ref: chrono_rank[step_id_by_key[exit_ref]],
                ) or [ref]
                for replacement in replacements:
                    if replacement not in seen:
                        seen.add(replacement)
                        expanded.append(replacement)
            out[step_id] = expanded
        return out

    @staticmethod
    def _metadata_seq_is_valid(
        seq: dict[str, int],
        parents_by_step: dict[str, list[str]],
    ) -> bool:
        return all(
            seq[parent_id] < seq[step_id]
            for step_id, parents in parents_by_step.items()
            for parent_id in parents
        )

    @staticmethod
    def _topo_seq(
        steps: dict[str, Step],
        parents_by_step: dict[str, list[str]],
        original_seq: dict[str, int],
    ) -> dict[str, int]:
        effects_of: dict[str, list[str]] = {sid: [] for sid in steps}
        indegree: dict[str, int] = dict.fromkeys(steps, 0)
        for effect, parents in parents_by_step.items():
            for cause in parents:
                effects_of[cause].append(effect)
                indegree[effect] += 1

        rank = {
            sid: (original_seq[sid], steps[sid].ts or "", sid)
            for sid in steps
        }
        ready = [(rank[sid], sid) for sid in steps if indegree[sid] == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            _, sid = heapq.heappop(ready)
            order.append(sid)
            for effect in sorted(effects_of[sid], key=lambda item: rank[item]):
                indegree[effect] -= 1
                if indegree[effect] == 0:
                    heapq.heappush(ready, (rank[effect], effect))

        if len(order) != len(steps):
            stuck = sorted(s for s in steps if indegree[s] > 0)
            raise ValueError(
                f"cycle in LangGraph checkpoint parentage involving {stuck[:5]}; "
                "checkpoint causality must be acyclic"
            )
        return {sid: i for i, sid in enumerate(order)}

    @staticmethod
    def _branch_target(parent_key: _CheckpointKey | None, child_key: _CheckpointKey) -> str | None:
        if parent_key is None:
            return None
        parent_ns, _ = parent_key
        child_ns, _ = child_key
        if parent_ns == child_ns:
            return None
        if parent_ns == _ROOT_NS:
            relative_ns = child_ns
        elif child_ns.startswith(f"{parent_ns}|"):
            relative_ns = child_ns[len(parent_ns) + 1:]
        else:
            return None
        next_ns = relative_ns.split("|", 1)[0]
        return next_ns.split(":", 1)[0] or None

    @staticmethod
    def _has_no_producing_node(step: Step, parent: Step | None) -> bool:
        """True for the checkpoints no graph node writes: the caller's input checkpoint, and
        the loop checkpoint that applies it (LangGraph's ``__start__``). Their missing name is a
        known absence. An update or fork written from the input checkpoint is not exempt."""
        return step.source is StepSource.INPUT or (
            step.source is StepSource.LOOP
            and parent is not None
            and parent.source is StepSource.INPUT
        )

    @staticmethod
    def _producing_node(parent_cp: dict | None, branch_target: str | None = None) -> str | None:
        if not parent_cp:
            return None
        if branch_target is not None:
            channel = f"{_BRANCH_PREFIX}{branch_target}"
            if channel in parent_cp.get("channel_values", {}):
                return branch_target
            return None
        for channel in parent_cp.get("channel_values", {}):
            if channel.startswith(_BRANCH_PREFIX):
                return channel[len(_BRANCH_PREFIX):]
        return None

    def _error_introduced(self, cp: dict, parent_cps: list[dict]) -> str | None:
        """Return the error message iff this checkpoint is where the error first appears."""
        current = cp.get("channel_values", {}).get(self._error_channel)
        if not current:
            return None
        previous = any(
            parent_cp.get("channel_values", {}).get(self._error_channel)
            for parent_cp in parent_cps
        )
        if previous:  # error was already present upstream -> not introduced here
            return None
        return str(current)

    def _rewrote_error(self, cp: dict, history: list[dict]) -> bool:
        """Whether a node wrote the error channel here while it already held an error.

        That node may have failed again or only have passed the state through, and a reducer
        can change the value in either case, so the write is disclosed, never recorded as a
        failure. ``history`` is the checkpoint's declared parents in its own namespace; a write
        shows as a moved ``channel_versions`` entry, or, without versions, a different value.
        """
        channel = self._error_channel
        current = cp.get("channel_values", {}).get(channel)
        if not current:
            return False
        version = cp.get("channel_versions", {}).get(channel)
        for parent in history:
            before = parent.get("channel_values", {}).get(channel)
            if not before:
                continue
            before_version = parent.get("channel_versions", {}).get(channel)
            if version is not None and before_version is not None:
                if before_version != version:
                    return True
            elif not _same_error(before, current):
                return True
        return False


def _same_error(a: Any, b: Any) -> bool:
    """Whether two error-channel values are the same error, leaning toward "same".

    Used only where a checkpoint has no channel versions. Revived values are distinct objects,
    so equality falls back to the rendered text, and a value that can be neither compared nor
    rendered counts as the same, so a persisted error is not disclosed as a write at every
    checkpoint of such a history.
    """
    try:
        if (a == b) is True:
            return True
    except Exception:
        pass
    try:
        return str(a) == str(b)
    except Exception:
        return True


def _decoded(listing: Iterable[Any]) -> Iterator[Any]:
    """Iterate a saver's listing, turning a failure to decode a row into a usage error.

    Only saver code runs while the next tuple is produced: the serializer reviving a blob,
    ``json.loads`` on the metadata column, the database driver. Whatever it raises there
    (an unknown serialization type, JSON nested past the recursion limit, a non-string column)
    means the history cannot be read, which is a property of the input, not a crash.
    """
    iterator = iter(listing)
    while True:
        try:
            item = next(iterator)
        except StopIteration:
            return
        except Exception as exc:
            raise ValueError(
                f"checkpoint history cannot be decoded ({type_name(exc)})"
            ) from exc
        yield item


def _checked(t: Any) -> tuple[dict, dict, dict, dict]:
    """The tuple's ``configurable``, checkpoint and metadata, each checked against the shape
    LangGraph declares for the fields this adapter reads (strictly: ``True`` is not ``1``).

    A field is checked only when present; ``checkpoint.id`` is required, and when the config
    also names the checkpoint the two must agree, because step identity comes from one and
    chronology and task hashing from the other.
    """
    # A config without a thread id is not "empty": `ingest` filters on the thread id, so it
    # would silently drop the checkpoint, and with it any failure it recorded.
    config = getattr(t, "config", None)
    if not isinstance(config, dict) or not isinstance(config.get("configurable"), dict):
        raise ValueError("checkpoint config must be an object with a configurable object")
    cfg = config["configurable"]
    if "thread_id" not in cfg:
        raise ValueError("checkpoint config has no thread_id")
    cp = getattr(t, "checkpoint", None)
    if not isinstance(cp, dict):
        raise ValueError("checkpoint must be an object")
    cid = cp.get("id")
    label = f"checkpoint {cid!r}" if isinstance(cid, str) else "a checkpoint"
    if "id" not in cp:
        raise ValueError(f"{label} has no id")
    md = object_field(getattr(t, "metadata", None), f"{label} metadata")
    # No parent config is legitimate (a thread's first checkpoint). One that is present but
    # names no checkpoint is not: read as "no parent", it would make this checkpoint a root.
    parent = getattr(t, "parent_config", None)
    parent_cfg: dict = {}
    if parent is not None:
        if not isinstance(parent, dict) or not isinstance(parent.get("configurable"), dict):
            raise ValueError(f"{label} parent config must be an object with a configurable object")
        parent_cfg = parent["configurable"]
        if "checkpoint_id" not in parent_cfg:
            raise ValueError(f"{label} parent config has no checkpoint_id")
    for where, fields, values in (
        ("checkpoint", _CHECKPOINT_FIELDS, cp),
        ("metadata", _METADATA_FIELDS, md),
        ("config", _CONFIG_FIELDS, cfg),
        ("parent config", _CONFIG_FIELDS, parent_cfg),
    ):
        for name, adapter in fields.items():
            if name not in values:
                continue
            try:
                adapter.validate_python(values[name], strict=True)
            except ValidationError as exc:
                reason = exc.errors()[0]["msg"]
                raise ValueError(f"{label} has a malformed {where} {name}: {reason}") from None
    if "checkpoint_id" in cfg and cfg["checkpoint_id"] != cid:
        raise ValueError(f"{label} disagrees with its config checkpoint_id")
    writes = getattr(t, "pending_writes", None)
    if writes is not None and not isinstance(writes, (list, tuple)):
        raise ValueError(f"{label} has pending writes that are not a list")
    return cfg, cp, md, parent_cfg
