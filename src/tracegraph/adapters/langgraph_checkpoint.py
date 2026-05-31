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
from typing import Any

from tracegraph.model import (
    Edge,
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
        try:
            tuples = list(self._saver.list(None))
        except Exception as exc:  # noqa: BLE001 - surface as a clear capability error
            raise NotImplementedError(
                "this checkpointer does not support listing all threads"
            ) from exc
        seen: list[str] = []
        for t in tuples:
            tid = (t.config.get("configurable") or {}).get("thread_id")
            if tid is not None and tid not in seen:
                seen.append(tid)
        return seen

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
            original_seq[step_id] = int(md.get("step", 0))
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
            Edge(type=EdgeType.CAUSED_BY, src=step_id, dst=parent_id)
            for step_id, parents in parents_by_step.items()
            for parent_id in parents
        ]

        for step_id, step in steps.items():
            display_parent_id = display_parent_by_step.get(step_id)
            parent_cp = checkpoints.get(display_parent_id) if display_parent_id else None
            name = self._producing_node(parent_cp)
            if name:
                step.name = name
                if "tool" in name.lower():
                    step.kind = StepKind.TOOL
            err = self._error_introduced(checkpoints[step_id], parent_cp)
            if err is not None:
                step.status = StepStatus.ERROR
                step.error_msg = err

        any_error = any(s.status is StepStatus.ERROR for s in steps.values())
        trace = Trace(
            trace_id=trace_id,
            source_kind=self._source_kind,
            thread_id=trace_id,
            status=StepStatus.ERROR if any_error else StepStatus.OK,
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
        entry_parent_by_ns: dict[str, _CheckpointKey] = {}
        terminal_step_by_ns: dict[str, str] = {}
        for step_id, key in key_by_step_id.items():
            ns, _ = key
            if ns == _ROOT_NS:
                continue
            terminal = terminal_step_by_ns.get(ns)
            if terminal is None or chrono_rank[terminal] < chrono_rank[step_id]:
                terminal_step_by_ns[ns] = step_id
            for parent_key in parent_keys.get(step_id, []):
                if parent_key[0] != ns:
                    entry_parent_by_ns.setdefault(ns, parent_key)

        exits_by_entry_parent: dict[_CheckpointKey, list[_CheckpointKey]] = {}
        for ns, entry_parent in entry_parent_by_ns.items():
            terminal_step = terminal_step_by_ns.get(ns)
            if terminal_step is None:
                continue
            exits_by_entry_parent.setdefault(entry_parent, []).append(key_by_step_id[terminal_step])

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
    def _producing_node(parent_cp: dict | None) -> str | None:
        if not parent_cp:
            return None
        for channel in parent_cp.get("channel_values", {}):
            if channel.startswith(_BRANCH_PREFIX):
                return channel[len(_BRANCH_PREFIX):]
        return None

    def _error_introduced(self, cp: dict, parent_cp: dict | None) -> str | None:
        """Return the error message iff this checkpoint is where the error first appears."""
        current = cp.get("channel_values", {}).get(self._error_channel)
        if not current:
            return None
        previous = (
            (parent_cp or {}).get("channel_values", {}).get(self._error_channel)
            if parent_cp
            else None
        )
        if previous:  # error was already present upstream -> not introduced here
            return None
        return str(current)
