"""M5: what-if questions answered on a modified copy of the pool, in a scratch FalkorDB graph.

`what_if(kind, args)` copies the active pool into a scratch graph named `<name>__whatif`, mutates
the copy, re-runs the same graph tools there, diffs the chosen cycles/paths against the original,
and deletes the scratch graph. Every product answer still comes from FalkorDB; the scratch graph is
built with `graph.load_pool` from a `Pool` read back out of the active graph.

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
from dataclasses import replace
from typing import Any

from swap.envelope import Outcome, Timer, UnknownEntity, build_trace, envelope, resolve_pair
from swap.generator import ABO_TYPES, Pool
from swap.graph import attach_altruist, drop_graph, load_pool, pool_from_graph
from swap.tools import EMPTY_NOTE, SwapTools

SCRATCH_SUFFIX = "__whatif"
DEFAULT_MAX_LEN = 3
DEFAULT_PRIORITY_WEIGHT = 2.0
ALTRUIST_ID = -1  # donor-only nodes take negative ids, so they never collide with a real pair id

WHAT_IF_TOOL = "what_if"
KINDS = ("add_altruistic_donor", "remove_pair", "set_priority")


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


@contextmanager
def _scratch(
    tools: SwapTools, pool: Pool, altruists: Sequence[tuple[int, str]] = ()
) -> Iterator[SwapTools]:
    """Load a modified pool into the scratch graph, yield tools bound to it, always drop it."""
    name = scratch_name(tools.graph_name)
    load_pool(pool, name, db=tools.db)
    try:
        scratch = SwapTools(name, db=tools.db)
        for altruist_id, donor_abo in altruists:
            attach_altruist(scratch.graph, altruist_id, donor_abo)
        yield scratch
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


def _without_pair(pool: Pool, pair_id: int) -> Pool:
    """Copy of `pool` without one pair and every edge touching it (ids of the rest preserved)."""
    keep = [i for i, pid in enumerate(pool.ids) if pid != pair_id]
    return Pool(
        n=len(keep),
        patient_abo=[pool.patient_abo[i] for i in keep],
        donor_abo=[pool.donor_abo[i] for i in keep],
        pra=[pool.pra[i] for i in keep],
        ids=[pool.ids[i] for i in keep],
        hard=[pool.hard[i] for i in keep],
        seed=pool.seed,
        edges=[(a, b) for a, b in pool.edges if a != pair_id and b != pair_id],
    )


def _prioritise(pool: Pool, pair_id: int) -> Pool:
    """Copy of `pool` with one pair marked hard (PRA 0.90-like), so the priority weight hits it."""
    hard = [
        True if pid == pair_id else is_hard
        for pid, is_hard in zip(pool.ids, pool.hard, strict=True)
    ]
    return replace(pool, hard=hard)


def _add_altruistic_donor(
    tools: SwapTools, args: dict[str, Any], timer: Timer, max_len: int
) -> dict[str, Any]:
    donor_abo = args.get("donor_abo")
    if donor_abo not in ABO_TYPES:
        raise ValueError(f"donor_abo must be one of {list(ABO_TYPES)}, got {donor_abo!r}")

    pool = pool_from_graph(tools.graph)
    before = tools.best_set(max_len)["result"]
    flow_before = tools.flow_bound()["result"]["flow_bound"]
    with _scratch(tools, pool, [(ALTRUIST_ID, str(donor_abo))]) as scratch:
        after = scratch.best_set(max_len)["result"]
        flow_after = scratch.flow_bound()["result"]["flow_bound"]

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

    modified = _without_pair(pool_from_graph(tools.graph), pair.id)
    before = tools.best_set(max_len)["result"]
    with _scratch(tools, modified) as scratch:
        after = scratch.best_set(max_len)["result"]
        pairs_after = scratch.n_pairs()

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

    modified = _prioritise(pool_from_graph(tools.graph), pair.id)
    before = tools.best_set(max_len, priority=weight)["result"]
    with _scratch(tools, modified) as scratch:
        after = scratch.best_set(max_len, priority=weight)["result"]

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
    }
    trace = build_trace(WHAT_IF_TOOL, args, timer)
    return envelope(_outcome(after), result, added, trace=trace)
