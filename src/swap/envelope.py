"""M6: the four-outcome envelope every tool returns, plus pair-id resolution against the graph.

The outcome is never a claim of safety: an empty traversal is `not_in_graph` ("the graph does
not know"), and an id the graph does not hold is `unknown_entity`. Every envelope carries a
trace `{tool, args, ms, rows}` and the path (evidence) the answer walked.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from time import perf_counter
from typing import Any

#: Entity ids are resolved against the graph before any other query runs.
RESOLVE_PAIR = "MATCH (p:Pair {id: $id}) RETURN p.id, p.patient_abo, p.donor_abo, p.pra, p.hard"


class Outcome(str, Enum):
    """The four answers a tool may give."""

    ANSWERED_WITH_PATH = "answered_with_path"
    FROM_MEMORY = "from_memory"
    NOT_IN_GRAPH = "not_in_graph"
    UNKNOWN_ENTITY = "unknown_entity"


@dataclass(frozen=True, slots=True)
class ResolvedPair:
    """The stored attributes of a `:Pair`, read straight from the graph."""

    id: int
    patient_abo: str
    donor_abo: str
    pra: float
    hard: bool


class UnknownEntity(LookupError):
    """An id the caller named is not in the graph; the tool then answers `unknown_entity`."""

    def __init__(self, kind: str, entity_id: Any) -> None:
        self.kind = kind
        self.entity_id = entity_id
        super().__init__(f"{kind} {entity_id!r} is not in the graph")


class Timer:
    """Times one tool call in milliseconds; `rows` records the rows the graph returned."""

    __slots__ = ("_started", "ms", "rows")

    def __init__(self) -> None:
        self._started = perf_counter()
        self.ms = 0.0
        self.rows = 0

    def stop(self, rows: int | None = None) -> Timer:
        self.ms = (perf_counter() - self._started) * 1000.0
        if rows is not None:
            self.rows = int(rows)
        return self


@contextmanager
def timed() -> Iterator[Timer]:
    """Time a block; the timer's `ms` is filled in on exit (even if the block raises)."""
    timer = Timer()
    try:
        yield timer
    finally:
        timer.stop()


def build_trace(tool: str, args: dict[str, Any], timer: Timer) -> dict[str, Any]:
    """The `{tool, args, ms, rows}` record every envelope carries."""
    return {"tool": tool, "args": dict(args), "ms": round(timer.ms, 3), "rows": int(timer.rows)}


def envelope(
    outcome: Outcome | str,
    result: Any,
    path: Iterable[Sequence[int]] = (),
    *,
    trace: dict[str, Any] | None = None,
    tool: str = "",
    args: dict[str, Any] | None = None,
    timer: Timer | None = None,
) -> dict[str, Any]:
    """Assemble the four-outcome envelope; `path` is the evidence the answer walked."""
    if trace is None:
        if timer is None:
            timer = Timer().stop()
        trace = build_trace(tool, args or {}, timer)
    return {
        "outcome": outcome.value if isinstance(outcome, Outcome) else str(outcome),
        "result": result,
        "path": [list(step) for step in path],
        "trace": trace,
    }


def resolve_pair(graph: Any, pair_id: Any) -> ResolvedPair:
    """Read a pair from the graph, or raise `UnknownEntity` when the id is not there."""
    try:
        pid = int(pair_id)
    except (TypeError, ValueError):
        raise UnknownEntity("Pair", pair_id) from None
    rows = graph.query(RESOLVE_PAIR, {"id": pid}).result_set
    if not rows:
        raise UnknownEntity("Pair", pid)
    stored_id, patient_abo, donor_abo, pra, hard = rows[0]
    return ResolvedPair(int(stored_id), str(patient_abo), str(donor_abo), float(pra), bool(hard))
