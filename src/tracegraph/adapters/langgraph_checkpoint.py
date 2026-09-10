"""Ingest LangGraph checkpoint history into a :class:`RawTrace`.

Reads any ``BaseCheckpointSaver`` via ``.list()`` and reconstructs the causal chain from
each checkpoint's declared parentage. One checkpoint (super-step) becomes one ``Step``;
the parent-checkpoint links become ``CAUSED_BY`` edges (effect → cause).

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
  channel (default ``"error"``) is truthy at that checkpoint but falsy at its parent.
* the producing node's name is recovered best-effort from the parent checkpoint's
  internal ``branch:to:<node>`` channel; absent that, ``name`` is left unset.
"""

from __future__ import annotations

import heapq
import re
from typing import Any

from tracegraph.model import (
    CausalFidelity,
    Edge,
    EdgeOrigin,
    EdgeType,
    RawTrace,
    Step,
    StepKind,
    StepSource,
    StepStatus,
    Trace,
)

_ROOT_NS = ""
_BRANCH_PREFIX = "branch:to:"
_TOOL_TOKENS = {"tool", "tools"}


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
    ) -> None:
        self._saver = saver
        self._error_channel = error_channel
        self._source_kind = source_kind

    # --- TraceAdapter ---

    def discover(self) -> list[str]:
        """Distinct thread ids known to the saver (best effort; not all savers support it)."""
        list_fn = getattr(self._saver, "list", None)
        if list_fn is None:
            raise NotImplementedError(
                "this checkpointer does not support listing all threads"
            )
        try:
            tuples = list(list_fn(None))
        except NotImplementedError as exc:
            # Only the documented capability signal becomes "unsupported"; any other
            # failure (sqlite corruption, I/O error, a saver bug) must propagate, not
            # masquerade as a missing capability.
            raise NotImplementedError(
                "this checkpointer does not support listing all threads"
            ) from exc
        seen: set[str] = set()
        ordered: list[str] = []
        for t in tuples:
            tid = (t.config.get("configurable") or {}).get("thread_id")
            if tid is not None and tid not in seen:
                seen.add(tid)
                ordered.append(tid)
        return ordered

    def ingest(self, trace_id: str) -> RawTrace:
        config = {"configurable": {"thread_id": trace_id}}
        tuples = [
            t
            for t in self._saver.list(config)
            if (t.config.get("configurable") or {}).get("thread_id") == trace_id
        ]
        if not tuples:
            raise KeyError(f"no checkpoints for thread_id {trace_id!r}")

        checkpoints: dict[str, dict] = {}
        step_id_by_key: dict[_CheckpointKey, str] = {}
        key_by_step_id: dict[str, _CheckpointKey] = {}
        parent_keys: dict[str, list[_CheckpointKey]] = {}
        original_seq: dict[str, int] = {}
        steps: dict[str, Step] = {}
        for t in tuples:
            cfg = t.config.get("configurable") or {}
            cp = t.checkpoint
            md = t.metadata or {}
            ns = cfg.get("checkpoint_ns", _ROOT_NS) or _ROOT_NS
            cid = cfg.get("checkpoint_id") or cp["id"]
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
            try:
                original_seq[step_id] = int(md.get("step", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"checkpoint {cid!r} in thread {trace_id!r} has a non-integer "
                    f"metadata step: {md.get('step')!r}"
                ) from exc
            parent_keys[step_id] = self._declared_parent_keys(t, ns)
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

        for step_id, step in steps.items():
            display_parent_id = display_parent_by_step.get(step_id)
            parent_cp = checkpoints.get(display_parent_id) if display_parent_id else None
            parent_key = key_by_step_id.get(display_parent_id) if display_parent_id else None
            branch_target = self._branch_target(parent_key, key_by_step_id[step_id])
            name = self._producing_node(parent_cp, branch_target)
            if name:
                step.name = name
                if _looks_like_tool_node(name):
                    step.kind = StepKind.TOOL
            causal_parent_ids = parents_by_step.get(step_id, [])
            error_parent_cps = [checkpoints[parent_id] for parent_id in causal_parent_ids]
            err = self._error_introduced(checkpoints[step_id], error_parent_cps)
            if err is not None:
                step.status = StepStatus.ERROR
                step.error_msg = err

        any_error = any(s.status is StepStatus.ERROR for s in steps.values())
        trace = Trace(
            trace_id=trace_id,
            source_kind=self._source_kind,
            thread_id=trace_id,
            status=StepStatus.ERROR if any_error else StepStatus.OK,
            causal_fidelity=CausalFidelity.DECLARED_DAG,
        )
        return RawTrace(
            trace=trace,
            steps=sorted(steps.values(), key=lambda s: (s.seq, s.step_id)),
            causal_edges=edges,
        )

    # --- helpers ---

    @staticmethod
    def _step_id(ns: str, cid: str) -> str:
        return cid if ns == _ROOT_NS else f"{ns}:{cid}"

    @staticmethod
    def _declared_parent_keys(t: Any, ns: str) -> list[_CheckpointKey]:
        parent_cfg = (t.parent_config or {}).get("configurable") or {}
        parent_id = parent_cfg.get("checkpoint_id")
        if parent_id is not None:
            parent_ns = parent_cfg.get("checkpoint_ns", ns) or _ROOT_NS
            return [(parent_ns, parent_id)]

        md = t.metadata or {}
        parents = md.get("parents") or {}
        if not isinstance(parents, dict):
            return []
        closest = LangGraphCheckpointAdapter._closest_parent_ns(ns, parents)
        if closest is None:
            return []
        checkpoint_id = parents[closest]
        return [] if checkpoint_id is None else [(closest or _ROOT_NS, checkpoint_id)]

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
