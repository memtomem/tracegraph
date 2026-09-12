"""Regenerate tests/fixtures/langgraph/native-failure.sqlite (needs langgraph installed)."""
from pathlib import Path
import sys
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

target = Path(sys.argv[1])
target.unlink(missing_ok=True)


class S(TypedDict):
    x: int


def call_tool(state):
    raise RuntimeError("REVIEW_SYNTHETIC_NODE_FAILURE")


graph = StateGraph(S)
graph.add_node("plan", lambda state: {"x": 1})
graph.add_node("call_tool", call_tool)
graph.add_edge(START, "plan")
graph.add_edge("plan", "call_tool")
graph.add_edge("call_tool", END)

with SqliteSaver.from_conn_string(str(target)) as saver:
    try:
        graph.compile(checkpointer=saver).invoke({"x": 0}, {"configurable": {"thread_id": "t"}})
    except RuntimeError:
        pass
    # Fold the write-ahead log into the database file. Without this the checkpoints live in a
    # `-wal` sidecar, and committing the `.sqlite` alone would ship an empty fixture.
    saver.conn.execute("PRAGMA journal_mode=DELETE")

for sidecar in (f"{target}-wal", f"{target}-shm"):
    Path(sidecar).unlink(missing_ok=True)
print("wrote", target)
