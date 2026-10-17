"""M5: what-if questions answered on a copy of the graph that FalkorDB makes itself.

`what_if(kind, args)` asks FalkorDB to clone the active graph (`GRAPH.COPY`) into `<name>__whatif`,
changes the clone with human-written Cypher, re-runs the same graph tools there, diffs the chosen
swaps against the original and drops the clone. The pool never leaves the database: the clone keeps
the pairs, the flow network, the stored swaps, the plans and any failed crossmatch.

Kinds:
  * `add_altruistic_donor(donor_abo)` - one donor with no patient: a `:Altruist` node that can give
    to any compatible patient, directly or as a two-hop chain (altruist -> p1 -> p2, p1's donor
    giving to p2). Chains are produced BY FalkorDB and packed with the cycles (one chain per donor).
  * `remove_pair(pair_id)` - drop one pair and every edge touching it.
  * `set_priority(pair_id, weight)` - mark one pair as high priority (hard) and re-pack with that
    priority weight.

An id the graph does not hold answers `unknown_entity`; an altruist that could give to nobody
answers `not_in_graph` (an empty traversal means "the graph does not know", never "safe").
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from time import perf_counter
from typing import Any

from swap.envelope import Outcome, Timer, UnknownEntity, build_trace, envelope, resolve_pair
from swap.generator import ABO_TYPES
from swap.graph import attach_altruist, drop_graph
from swap.tools import EMPTY_NOTE, SwapTools

SCRATCH_SUFFIX = "__whatif"
DEFAULT_MAX_LEN = 3
DEFAULT_PRIORITY_WEIGHT = 2.0
ALTRUIST_ID = -1  # donor-only nodes take negative ids, so they never collide with a real pair id

WHAT_IF_TOOL = "what_if"
KINDS = ("add_altruistic_donor", "remove_pair", "set_priority")

# Changes made to the clone, never to the active graph. Removing a pair drops its swaps first.
REMOVE_PAIR_CYCLES = "MATCH (c:Cycle) WHERE $id IN c.members DETACH DELETE c RETURN count(c)"
REMOVE_PAIR = "MATCH (p:Pair {id: $id}) DETACH DELETE p RETURN count(*)"
REMOVE_PAIR_DONOR = "MATCH (d:Donor {id: $id}) DETACH DELETE d RETURN count(*)"
REMOVE_PAIR_PATIENT = "MATCH (pt:Patient {id: $id}) DETACH DELETE pt RETURN count(*)"
SET_HARD = "MATCH (p:Pair {id: $id}) WHERE NOT p.hard SET p.hard = true RETURN count(p)"
BUMP_CYCLE_HARD = "MATCH (c:Cycle) WHERE $id IN c.members SET c.hard = c.hard + 1 RETURN count(c)"

WHATIF_CYPHER: dict[str, str] = {
    "whatif_remove_pair_cycles": REMOVE_PAIR_CYCLES,
    "whatif_remove_pair": REMOVE_PAIR,
    "whatif_remove_pair_donor": REMOVE_PAIR_DONOR,
    "whatif_remove_pair_patient": REMOVE_PAIR_PATIENT,
    "whatif_set_hard": SET_HARD,
    "whatif_bump_cycle_hard": BUMP_CYCLE_HARD,
}


def scratch_name(graph_name: str) -> str:
    """The scratch graph a what-if run is computed in (and dropped at the end)."""
    return f"{graph_name}{SCRATCH_SUFFIX}"


def run_what_if(tools: SwapTools, kind: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """Dispatch one what-if question. `tools` is bound to the active graph."""
    if kind not in KINDS:
        raise ValueError(f"unknown what-if kind {kind!r}; expected one of {list(KINDS)}")
    timer = Timer()
    max_len = int(args.get("max_len", DEFAULT_MAX_LEN))
    if kind == "add_altruistic_donor":
        return _add_altruistic_donor(tools, dict(args), timer, max_len)
    if kind == "remove_pair":
        return _remove_pair(tools, dict(args), timer, max_len)
    return _set_priority(tools, dict(args), timer, max_len)


class _Clone:
    """A FalkorDB-made copy of the active graph, with a log of what was changed in it."""

    def __init__(self, tools: SwapTools, copy_ms: float) -> None:
        self.tools = tools
        self.copy_ms = copy_ms
        self.mutations: list[dict[str, Any]] = []

    def run(self, name: str, params: Mapping[str, Any]) -> int:
        started = perf_counter()
        rows = self.tools.graph.query(WHATIF_CYPHER[name], dict(params)).result_set
        count = int(rows[0][0]) if rows else 0
        self.mutations.append(
            {"name": name, "ms": round((perf_counter() - started) * 1000, 1), "rows": count}
        )
        return count

    def report(self) -> dict[str, Any]:
        return {
            "method": "GRAPH.COPY",
            "graph": self.tools.graph_name,
            "copy_ms": self.copy_ms,
            "mutations": self.mutations,
            "dropped": True,
        }


@contextmanager
def _clone(tools: SwapTools) -> Iterator[_Clone]:
    """Ask FalkorDB to copy the active graph, yield tools bound to the copy, always drop it."""
    name = scratch_name(tools.graph_name)
    drop_graph(tools.db, name)
    started = perf_counter()
    tools.graph.copy(name)
    copy_ms = round((perf_counter() - started) * 1000, 1)
    try:
        yield _Clone(SwapTools(name, db=tools.db), copy_ms)
    finally:
        drop_graph(tools.db, name)


def _canon(cycle: Sequence[int]) -> tuple[int, ...]:
    at = cycle.index(min(cycle))
    return tuple(cycle[at:] + cycle[:at])


def _added_removed(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[list[list[int]], list[list[int]]]:
    """Cycles/paths present after the change but not before, and vice versa (the path evidence)."""
    before_set = {_canon(c) for c in before["chosen"]}
    after_set = {_canon(c) for c in after["chosen"]}
    added = [list(c) for c in sorted(after_set - before_set)]
    removed = [list(c) for c in sorted(before_set - after_set)]
    return added, removed


def _outcome(result: Mapping[str, Any]) -> Outcome:
    return Outcome.ANSWERED_WITH_PATH if result["chosen"] else Outcome.NOT_IN_GRAPH


def _unknown(timer: Timer, args: Mapping[str, Any], exc: UnknownEntity) -> dict[str, Any]:
    timer.stop(0)
    result = {"message": str(exc), "entity": {"kind": exc.kind, "id": exc.entity_id}}
    return envelope(
        Outcome.UNKNOWN_ENTITY, result, (), trace=build_trace(WHAT_IF_TOOL, dict(args), timer)
    )


def _resolve(tools: SwapTools, pair_id: Any) -> Any:
    """Return the resolved pair, or the `UnknownEntity` to answer `unknown_entity` with."""
    try:
        return resolve_pair(tools.graph, pair_id)
    except UnknownEntity as exc:
        return exc


def _add_altruistic_donor(
    tools: SwapTools, args: dict[str, Any], timer: Timer, max_len: int
) -> dict[str, Any]:
    donor_abo = args.get("donor_abo")
    if donor_abo not in ABO_TYPES:
        raise ValueError(f"donor_abo must be one of {list(ABO_TYPES)}, got {donor_abo!r}")

    before = tools.best_set(max_len)["result"]
    flow_before = tools.flow_bound()["result"]["flow_bound"]
    with _clone(tools) as clone:
        started = perf_counter()
        links = attach_altruist(clone.tools.graph, ALTRUIST_ID, str(donor_abo))
        clone.mutations.append(
            {
                "name": "loader_altruist (+ edges, donor, flow arcs)",
                "ms": round((perf_counter() - started) * 1000, 1),
                "rows": links,
            }
        )
        after = clone.tools.best_set(max_len)["result"]
        flow_after = clone.tools.flow_bound()["result"]["flow_bound"]
        scratch = clone.report()

    added, removed = _added_removed(before, after)
    timer.stop(after["candidate_chains"])
    result = {
        "kind": "add_altruistic_donor",
        "args": args,
        "graph": tools.graph_name,
        "donor_abo": donor_abo,
        "before": {"matched": before["matched"], "cycles": len(before["cycles"])},
        "after": {
            "matched": after["matched"],
            "cycles": len(after["cycles"]),
            "chains": after["chains"],
        },
        "gain": after["matched"] - before["matched"],
        "flow_bound": {"before": flow_before, "after": flow_after},
        "changed": {"added": added, "removed": removed},
        "scratch": scratch,
    }
    if not after["candidate_chains"]:
        result["note"] = f"{EMPTY_NOTE} patient this donor can give to"
    trace = build_trace(WHAT_IF_TOOL, args, timer)
    return envelope(_outcome(after), result, added, trace=trace)


def _remove_pair(
    tools: SwapTools, args: dict[str, Any], timer: Timer, max_len: int
) -> dict[str, Any]:
    pair = _resolve(tools, args.get("pair_id"))
    if isinstance(pair, UnknownEntity):
        return _unknown(timer, args, pair)

    before = tools.best_set(max_len)["result"]
    with _clone(tools) as clone:
        clone.run("whatif_remove_pair_cycles", {"id": pair.id})
        if clone.run("whatif_remove_pair", {"id": pair.id}) != 1:
            raise RuntimeError(f"what-if copy: pair {pair.id} was not removed exactly once")
        clone.run("whatif_remove_pair_donor", {"id": pair.id})
        clone.run("whatif_remove_pair_patient", {"id": pair.id})
        after = clone.tools.best_set(max_len)["result"]
        pairs_after = clone.tools.n_pairs()
        scratch = clone.report()

    added, removed = _added_removed(before, after)
    timer.stop(after["candidate_cycles"])
    result = {
        "kind": "remove_pair",
        "args": args,
        "graph": tools.graph_name,
        "removed_pair": {
            "id": pair.id,
            "patient_abo": pair.patient_abo,
            "donor_abo": pair.donor_abo,
            "pra": pair.pra,
            "hard": pair.hard,
        },
        "before": {"matched": before["matched"], "pairs": tools.n_pairs()},
        "after": {"matched": after["matched"], "pairs": pairs_after},
        "delta": after["matched"] - before["matched"],
        "changed": {"added": added, "removed": removed},
        "scratch": scratch,
    }
    trace = build_trace(WHAT_IF_TOOL, args, timer)
    return envelope(_outcome(after), result, added, trace=trace)


def _set_priority(
    tools: SwapTools, args: dict[str, Any], timer: Timer, max_len: int
) -> dict[str, Any]:
    pair = _resolve(tools, args.get("pair_id"))
    if isinstance(pair, UnknownEntity):
        return _unknown(timer, args, pair)
    weight = float(args.get("weight", DEFAULT_PRIORITY_WEIGHT))

    before = tools.best_set(max_len, priority=weight)["result"]
    with _clone(tools) as clone:
        if clone.run("whatif_set_hard", {"id": pair.id}):
            clone.run("whatif_bump_cycle_hard", {"id": pair.id})
        after = clone.tools.best_set(max_len, priority=weight)["result"]
        scratch = clone.report()

    added, removed = _added_removed(before, after)
    covered_after = {patient for cycle in after["chosen"] for patient in cycle}
    timer.stop(after["candidate_cycles"])
    result = {
        "kind": "set_priority",
        "args": args,
        "graph": tools.graph_name,
        "pair": {"id": pair.id, "patient_abo": pair.patient_abo, "donor_abo": pair.donor_abo},
        "weight": weight,
        "before": {"matched": before["matched"], "cycles": before["chosen"]},
        "after": {"matched": after["matched"], "cycles": after["chosen"]},
        "delta": after["matched"] - before["matched"],
        "prioritised_pair_matched": pair.id in covered_after,
        "changed": {"added": added, "removed": removed},
        "scratch": scratch,
    }
    trace = build_trace(WHAT_IF_TOOL, args, timer)
    return envelope(_outcome(after), result, added, trace=trace)
