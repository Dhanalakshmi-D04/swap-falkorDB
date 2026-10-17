"""Why a patient is not in a swap, walked in the graph.

`explain_unmatched` (tools.py) reads who could give to a patient; this module adds the reasons:

  * blocked: the patient's live swaps each need a pair that a chosen swap already uses. A 4-hop walk
    (patient <- their swaps -> the other pairs <- the chosen swaps) names the chosen swaps that block
    the most of this patient's options.
  * ring too long: no 2- or 3-way swap exists, but the graph's shortest ring back to the patient
    (Cypher `shortestPath`) needs more pairs than the swap-length cap; `algo.SPpaths` lists the
    k shortest alternative routes.
  * no outgoing / no incoming: this pair's donor can give to nobody (an AB donor gives only to AB
    patients), or nobody can give to this patient. No swap of any length can include them.
  * options dead: every swap this patient had was killed by a failed crossmatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from redis.exceptions import ResponseError

if TYPE_CHECKING:
    from swap.tools import SwapTools

MAX_RING_HOPS = 6

OPTIONS_OF_PAIR = (
    "MATCH (x:Pair {id: $id})<-[:HAS]-(opt:Cycle) "
    "RETURN count(opt), sum(CASE WHEN opt.live THEN 1 ELSE 0 END), "
    "sum(CASE WHEN opt.live AND NOT opt.taken THEN 1 ELSE 0 END)"
)
IN_CHOSEN = "UNWIND $keys AS k MATCH (c:Cycle {key: k}) WHERE $id IN c.members RETURN c.key"
# The 4-hop walk: this patient's live swaps -> their other pairs -> the chosen swap holding that pair.
BLOCKED_BY = (
    "MATCH (x:Pair {id: $id})<-[:HAS]-(opt:Cycle) WHERE opt.live "
    "UNWIND opt.members AS m WITH opt, m WHERE m <> $id "
    "MATCH (chosen:Cycle) WHERE chosen.key IN $keys AND m IN chosen.members "
    "RETURN chosen.key, chosen.members, count(DISTINCT opt) AS blocked "
    "ORDER BY blocked DESC, chosen.key LIMIT 5"
)
DONOR_REACH = (
    "MATCH (p:Pair {id: $id}) OPTIONAL MATCH (p)-[o:CAN_GIVE]->(:Pair) WITH p, count(o) AS out "
    "OPTIONAL MATCH (:Pair)-[i:CAN_GIVE]->(p) RETURN out, count(i)"
)
# Shortest ring back to x over every pair x's donor can give to (exact; Cypher shortestPath).
SHORTEST_RING = (
    "MATCH (x:Pair {id: $id})-[:CAN_GIVE]->(y:Pair) "
    "WITH x, y, shortestPath((y)-[:CAN_GIVE*..6]->(x)) AS p WHERE p IS NOT NULL "
    "RETURN [x.id] + [n IN nodes(p) | n.id] AS ring ORDER BY size(ring), ring LIMIT 1"
)
# Up to 3 routes back through the ring's first step that are as short as the shortest ring
# (algo.SPpaths; maxLen is the shortest ring's own length, which keeps it fast on a dense pool).
ALTERNATIVE_ROUTES = (
    "MATCH (x:Pair {id: $id}), (y:Pair {id: $next}) "
    "CALL algo.SPpaths({sourceNode: y, targetNode: x, relTypes: ['CAN_GIVE'], maxLen: $max_hops, "
    "pathCount: 3}) YIELD path "
    "RETURN [x.id] + [n IN nodes(path) | n.id] AS ring ORDER BY size(ring), ring"
)
PLAN_KEYS = "MATCH (:Plan {id: $id})-[:SELECTS]->(c:Cycle) WHERE c.live RETURN c.key ORDER BY c.key"
LATEST_PLAN = (
    "MATCH (pl:Plan) WHERE pl.status IN ['draft', 'approved'] RETURN pl.id, pl.status "
    "ORDER BY pl.id DESC LIMIT 1"
)

EXPLAIN_CYPHER: dict[str, str] = {
    "explain_options": OPTIONS_OF_PAIR,
    "explain_in_chosen": IN_CHOSEN,
    "explain_blocked_by": BLOCKED_BY,
    "explain_donor_reach": DONOR_REACH,
    "explain_shortest_ring": SHORTEST_RING,
    "explain_alternative_routes": ALTERNATIVE_ROUTES,
    "explain_plan_keys": PLAN_KEYS,
    "explain_latest_plan": LATEST_PLAN,
}


def against(tools: SwapTools, plan_id: int | None) -> dict[str, Any]:
    """The set of chosen swaps a patient is explained against: a plan, the latest plan, or the best set."""
    if plan_id is None:
        latest = tools._run("explain_latest_plan")
        if latest:
            plan_id = int(latest[0][0])
    if plan_id is not None:
        keys = [str(row[0]) for row in tools._run("explain_plan_keys", {"id": plan_id})]
        return {"kind": "plan", "plan": plan_id, "keys": keys}
    best = tools.best_set(3)["result"]
    return {"kind": "best_set", "plan": None, "keys": best["cycle_keys"]}


def reasons(tools: SwapTools, pair_id: int, chosen: dict[str, Any]) -> dict[str, Any]:
    """The verdict and its evidence for one pair, against `chosen` (from `against`)."""
    keys = chosen["keys"]
    total, live, available = tools._run("explain_options", {"id": pair_id})[0]
    total, live, available = int(total), int(live or 0), int(available or 0)
    holding = tools._run("explain_in_chosen", {"keys": keys, "id": pair_id})
    out_degree, in_degree = (int(v) for v in tools._run("explain_donor_reach", {"id": pair_id})[0])
    blocked = [
        {"key": str(r[0]), "members": [int(m) for m in r[1]], "blocks": int(r[2])}
        for r in tools._run("explain_blocked_by", {"id": pair_id, "keys": keys})
    ] if not holding else []

    ring: list[int] = []
    routes: list[list[int]] = []
    if out_degree and in_degree:
        found = tools._run("explain_shortest_ring", {"id": pair_id})
        ring = [int(v) for v in found[0][0]] if found else []
        if len(ring) > 3:
            hops = len(ring) - 2  # from the ring's first step back to this pair
            try:
                routes = [
                    [int(v) for v in row[0]]
                    for row in tools._run(
                        "explain_alternative_routes",
                        {"id": pair_id, "next": ring[1], "max_hops": hops},
                    )
                ]
            except ResponseError:  # FalkorDB's query timeout: list no alternatives, keep the ring
                routes = []

    ring_pairs = len(ring) - 1 if ring else 0
    if holding:
        verdict = "in_swap"
    elif total and not live:
        verdict = "options_dead"
    elif not out_degree:
        verdict = "no_outgoing"
    elif not in_degree:
        verdict = "no_incoming"
    elif live:
        verdict = "blocked"
    elif ring:
        verdict = "ring_too_long"
    else:
        verdict = "no_ring"

    return {
        "verdict": verdict,
        "summary": _sentence(verdict, pair_id, chosen, live, blocked, ring_pairs, total),
        "against": {"kind": chosen["kind"], "plan": chosen["plan"], "swaps": len(keys)},
        "in_swap": str(holding[0][0]) if holding else None,
        "options": {"stored": total, "live": live, "available": available},
        "blocked_by": blocked,
        "donor_reach": {"can_give_to": out_degree, "can_receive_from": in_degree},
        "shortest_ring": {"ring": ring, "pairs": ring_pairs, "max_hops": MAX_RING_HOPS},
        "alternative_routes": routes,
    }


def _sentence(
    verdict: str,
    pair_id: int,
    chosen: dict[str, Any],
    live: int,
    blocked: list[dict[str, Any]],
    ring_pairs: int,
    total: int,
) -> str:
    where = f"plan {chosen['plan']}" if chosen["kind"] == "plan" else "the best set"
    if verdict == "in_swap":
        return f"Pair {pair_id} is in a swap of {where}."
    if verdict == "no_outgoing":
        return (
            f"Pair {pair_id}'s donor can give to no patient in the pool, so no swap of any length can "
            "include this pair; only a chain started by an altruistic donor could help."
        )
    if verdict == "no_incoming":
        return f"No donor in the pool can give to pair {pair_id}'s patient, so no swap can include them."
    if verdict == "options_dead":
        return f"All {total} of pair {pair_id}'s swaps were killed by failed crossmatches."
    if verdict == "blocked":
        top = blocked[0] if blocked else None
        lead = (
            f" The chosen swap {top['key']} blocks {top['blocks']} of them." if top else ""
        )
        return (
            f"Pair {pair_id} has {live} possible swap{'s' if live != 1 else ''}, but each one needs a "
            f"pair that {where} already uses.{lead}"
        )
    if verdict == "ring_too_long":
        return (
            f"Pair {pair_id} has no 2- or 3-way swap: the shortest ring back to this patient needs "
            f"{ring_pairs} pairs, and swaps are capped at 3."
        )
    return f"The graph finds no ring of up to {MAX_RING_HOPS} pairs back to pair {pair_id}."
