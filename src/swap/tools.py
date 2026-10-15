"""M3/M4 tools: cycle finding, the max-flow bound, per-pair explanation and best-set packing.

Every tool talks to FalkorDB first; the graph is on the request path of every answer. All Cypher
is human-written, parameterised and kept here as module-level constants (`CYPHER` maps a name to
the statement) so the README can quote it verbatim. The model never writes Cypher.

Honest framing, in the words the results use:
  * `flow_bound` is an upper bound that also allows chains (a matching relaxation), NOT the
    cycle optimum.
  * an empty traversal is `not_in_graph` ("the graph does not know"), never a claim of safety.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from falkordb import FalkorDB, Graph

from swap.config import DEFAULT_GRAPH, Settings
from swap.envelope import (
    Outcome,
    Timer,
    UnknownEntity,
    build_trace,
    envelope,
    resolve_pair,
)
from swap.graph import can_give_edges, connect, ensure_result_set_size
from swap.solve import pack_cycles, unbounded_cycle_cover

# --- Cypher (human-written, parameterised) --------------------------------------------------

COUNT_PAIRS = "MATCH (p:Pair) RETURN count(p)"
HARD_PAIR_IDS = "MATCH (p:Pair) WHERE p.hard RETURN p.id ORDER BY p.id"

# Cycles are produced BY FalkorDB. `a.id < b.id [+ a.id < c.id ...]` canonicalises the rotation
# to the minimum id (every directed cycle is counted once); in the 4-cycle `b.id <> d.id` rejects
# the walk a->b->c->b->a, which is a 2-cycle counted twice, not a 4-cycle.
FIND_CYCLES_2 = (
    "MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(a) "
    "WHERE a.id < b.id RETURN a.id, b.id ORDER BY a.id, b.id"
)
FIND_CYCLES_3 = (
    "MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(a) "
    "WHERE a.id < b.id AND a.id < c.id RETURN a.id, b.id, c.id ORDER BY a.id, b.id, c.id"
)
FIND_CYCLES_4 = (
    "MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(d:Pair)"
    "-[:CAN_GIVE]->(a) "
    "WHERE a.id < b.id AND a.id < c.id AND a.id < d.id AND b.id <> d.id "
    "RETURN a.id, b.id, c.id, d.id ORDER BY a.id, b.id, c.id, d.id"
)

# algo.maxFlow over the donor -> patient network built by graph.py. The figure is a maximum
# bipartite matching, i.e. an upper bound that additionally allows chains.
FLOW_BOUND = (
    "MATCH (s:S),(t:T) "
    "CALL algo.maxFlow({sourceNodes:[s],targetNodes:[t],relationshipTypes:['F'],"
    "capacityProperty:'cap'}) "
    "YIELD maxFlow, nodes, edges, edgeFlows RETURN maxFlow, nodes, edges, edgeFlows"
)

# Who can give to this patient, and who this pair's donor can give to.
EXPLAIN_INCOMING = "MATCH (p:Pair {id: $id})<-[:CAN_GIVE]-(d:Pair) RETURN d.id ORDER BY d.id"
EXPLAIN_OUTGOING = "MATCH (p:Pair {id: $id})-[:CAN_GIVE]->(r:Pair) RETURN r.id ORDER BY r.id"
# Split every other pair into donor-ABO-compatible vs -incompatible with THIS patient, from the
# stored node attributes alone (ABO compatibility: O gives to all, AB receives from all, else same).
EXPLAIN_ABO_SPLIT = (
    "MATCH (p:Pair {id: $id}) OPTIONAL MATCH (q:Pair) WHERE q.id <> p.id "
    "WITH p, q, (q.donor_abo = 'O' OR p.patient_abo = 'AB' OR q.donor_abo = p.patient_abo) AS ok "
    "RETURN sum(CASE WHEN ok THEN 1 ELSE 0 END), sum(CASE WHEN ok THEN 0 ELSE 1 END)"
)
# Cycles of length 2 and 3 through one pair (starting at that pair, so each directed cycle once).
CYCLES_THROUGH_2 = (
    "MATCH (p:Pair {id: $id})-[:CAN_GIVE]->(q:Pair)-[:CAN_GIVE]->(p) RETURN p.id, q.id"
)
CYCLES_THROUGH_3 = (
    "MATCH (p:Pair {id: $id})-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(p) "
    "RETURN p.id, b.id, c.id"
)

# Altruist chains (only in a what-if scratch graph). A donor-only :Altruist can give to one
# patient directly (altruist -> p1) or start a two-hop path (altruist -> p1 -> p2, p1's donor
# giving to p2). Both are produced BY FalkorDB; the packing treats each as one candidate set of
# patients, with one chain per altruist.
ALTRUIST_IDS = "MATCH (a:Altruist) RETURN a.id ORDER BY a.id"
CHAIN_DIRECT = "MATCH (a:Altruist {id: $id})-[:CAN_GIVE]->(p:Pair) RETURN p.id ORDER BY p.id"
CHAIN_TWO = (
    "MATCH (a:Altruist {id: $id})-[:CAN_GIVE]->(p1:Pair)-[:CAN_GIVE]->(p2:Pair) "
    "WHERE p1.id <> p2.id RETURN p1.id, p2.id ORDER BY p1.id, p2.id"
)

CYPHER: dict[str, str] = {
    "count_pairs": COUNT_PAIRS,
    "hard_pair_ids": HARD_PAIR_IDS,
    "find_cycles_2": FIND_CYCLES_2,
    "find_cycles_3": FIND_CYCLES_3,
    "find_cycles_4": FIND_CYCLES_4,
    "flow_bound": FLOW_BOUND,
    "explain_incoming": EXPLAIN_INCOMING,
    "explain_outgoing": EXPLAIN_OUTGOING,
    "explain_abo_split": EXPLAIN_ABO_SPLIT,
    "cycles_through_2": CYCLES_THROUGH_2,
    "cycles_through_3": CYCLES_THROUGH_3,
    "altruist_ids": ALTRUIST_IDS,
    "chain_direct": CHAIN_DIRECT,
    "chain_two": CHAIN_TWO,
}

CYCLES_BY_LENGTH = {2: FIND_CYCLES_2, 3: FIND_CYCLES_3, 4: FIND_CYCLES_4}

FLOW_BOUND_CAVEAT = (
    "upper bound that also allows chains (a matching relaxation), NOT the cycle optimum"
)
EMPTY_NOTE = "the graph does not know of any"


def _rotate_to_min(cycle: list[int]) -> list[int]:
    smallest = min(cycle)
    at = cycle.index(smallest)
    return cycle[at:] + cycle[:at]


def _flow_matched_edges(
    nodes: list[Any], edges: list[Any], edge_flows: list[Any]
) -> list[list[int]]:
    """Read the donor->patient arcs carrying flow out of the algo.maxFlow result."""
    labels: dict[int, str] = {}
    pair_of: dict[int, int] = {}
    for node in nodes:
        label = str(node.labels[0]) if node.labels else ""
        labels[int(node.id)] = label
        if label in ("Donor", "Patient"):
            pair_of[int(node.id)] = int(node.properties["id"])
    matched: list[list[int]] = []
    for edge, flow in zip(edges, edge_flows, strict=True):
        if not flow:
            continue
        if (
            labels.get(int(edge.src_node)) == "Donor"
            and labels.get(int(edge.dest_node)) == "Patient"
        ):
            matched.append([pair_of[int(edge.src_node)], pair_of[int(edge.dest_node)]])
    return sorted(matched)


class SwapTools:
    """The tool catalogue, bound to one FalkorDB graph name (one pool per run)."""

    def __init__(
        self,
        graph_name: str = DEFAULT_GRAPH,
        *,
        db: FalkorDB | None = None,
        cfg: Settings | None = None,
        graph: Graph | None = None,
    ) -> None:
        self.db = db if db is not None else connect(cfg)
        ensure_result_set_size(self.db)
        self.graph_name = graph_name
        self.graph = graph if graph is not None else self.db.select_graph(graph_name)

    # -- M3 ---------------------------------------------------------------------------------

    def find_cycles(self, max_len: int = 3) -> dict[str, Any]:
        """Cycles of length 2..max_len, produced by FalkorDB. Returns the cycles and counts."""
        if max_len not in CYCLES_BY_LENGTH:
            raise ValueError(f"max_len must be one of {sorted(CYCLES_BY_LENGTH)}, got {max_len!r}")

        timer = Timer()
        cycles: list[list[int]] = []
        counts: dict[int, int] = {}
        rows = 0
        for length in range(2, max_len + 1):
            rows_found = self.graph.query(CYCLES_BY_LENGTH[length]).result_set
            found = [[int(value) for value in row] for row in rows_found]
            counts[length] = len(found)
            cycles.extend(found)
            rows += len(found)
        timer.stop(rows)

        result = {"cycles": cycles, "counts": counts, "total": len(cycles), "max_len": max_len}
        trace = build_trace("find_cycles", {"max_len": max_len}, timer)
        if not cycles:
            result["note"] = f"{EMPTY_NOTE} 2- to {max_len}-cycles in this graph"
            return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=trace)
        return envelope(Outcome.ANSWERED_WITH_PATH, result, cycles, trace=trace)

    def flow_bound(self) -> dict[str, Any]:
        """The `algo.maxFlow` bound over the donor/patient network, and its matched arcs."""
        timer = Timer()
        max_flow, nodes, edges, edge_flows = self.graph.query(FLOW_BOUND).result_set[0]
        matched = _flow_matched_edges(nodes, edges, edge_flows)
        timer.stop(len(edges))

        result = {
            "flow_bound": int(max_flow),
            "matched_count": len(matched),
            "matched": matched,
            "description": FLOW_BOUND_CAVEAT,
        }
        trace = build_trace("flow_bound", {}, timer)
        if not matched:
            result["note"] = f"{EMPTY_NOTE} donor that can give to any patient in this graph"
            return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=trace)
        return envelope(Outcome.ANSWERED_WITH_PATH, result, matched, trace=trace)

    def explain_unmatched(self, pair_id: Any) -> dict[str, Any]:
        """Who could give to this patient and why others cannot; the cycles through this pair."""
        timer = Timer()
        args = {"pair_id": pair_id}
        try:
            pair = resolve_pair(self.graph, pair_id)
        except UnknownEntity as exc:
            timer.stop(0)
            result = {"message": str(exc), "entity": {"kind": exc.kind, "id": exc.entity_id}}
            return envelope(
                Outcome.UNKNOWN_ENTITY, result, (), trace=build_trace("explain_unmatched", args, timer)
            )

        rows = self.graph.query(EXPLAIN_INCOMING, {"id": pair.id}).result_set
        incoming = [int(r[0]) for r in rows]
        rows = self.graph.query(EXPLAIN_OUTGOING, {"id": pair.id}).result_set
        outgoing = [int(r[0]) for r in rows]
        abo_ok, abo_bad = self.graph.query(EXPLAIN_ABO_SPLIT, {"id": pair.id}).result_set[0]
        abo_compatible = int(abo_ok)
        abo_incompatible = int(abo_bad)
        cycles_through = self._cycles_through(pair.id)
        timer.stop(len(incoming) + len(outgoing) + 1)

        result = {
            "pair": {
                "id": pair.id,
                "patient_abo": pair.patient_abo,
                "donor_abo": pair.donor_abo,
                "pra": pair.pra,
                "hard": pair.hard,
            },
            "incoming_candidates": incoming,
            "outgoing_targets": outgoing,
            "reasoning": {
                "abo_incompatible_pairs": abo_incompatible,
                "abo_compatible_but_crossmatch_failed": abo_compatible - len(incoming),
                "note": (
                    "An absent edge from an ABO-compatible donor is a crossmatch failure at this "
                    "patient's PRA, not a missing fact."
                ),
            },
            "cycles_through": cycles_through,
        }
        trace = build_trace("explain_unmatched", args, timer)
        if not incoming:
            result["note"] = (
                f"{EMPTY_NOTE} donor that can give to this patient (empty traversal: the graph "
                "does not know, which is not the same as safe)"
            )
            return envelope(Outcome.NOT_IN_GRAPH, result, cycles_through, trace=trace)
        return envelope(Outcome.ANSWERED_WITH_PATH, result, cycles_through, trace=trace)

    # -- M4 ---------------------------------------------------------------------------------

    def best_set(self, max_len: int = 3, priority: float | None = None) -> dict[str, Any]:
        """Vertex-disjoint packing of the graph's candidate cycles (and altruist chains)."""
        timer = Timer()
        weight = 1.0 if priority is None else float(priority)
        args = {"max_len": max_len, "priority": priority}

        found = self.find_cycles(max_len)
        cycles = found["result"]["cycles"]
        chains, chain_groups = self._chain_candidates()
        hard_ids = self.hard_pair_ids()
        # Chains are columns len(cycles).. in the combined candidate list; shift their groups.
        groups = [[column + len(cycles) for column in group] for group in chain_groups]
        pack = pack_cycles(
            cycles + chains,
            hard_ids=hard_ids,
            priority_weight=weight,
            at_most_one_groups=groups,
        )
        timer.stop(found["trace"]["rows"] + len(chains) + 1)

        chosen_cycles = [c for i, c in zip(pack.indices, pack.chosen, strict=True) if i < len(cycles)]
        chosen_chains = [c for i, c in zip(pack.indices, pack.chosen, strict=True) if i >= len(cycles)]
        result = {
            "chosen": pack.chosen,
            "cycles": chosen_cycles,
            "chains": chosen_chains,
            "counts": {
                "cycles": len(chosen_cycles),
                "chains": len(chosen_chains),
                "patients": pack.covered,
            },
            "matched": pack.covered,
            "candidate_cycles": len(cycles),
            "candidate_chains": len(chains),
            "priority_weight": weight,
            "max_len": max_len,
            "solver": {
                "status": pack.status,
                "success": pack.success,
                "objective": pack.objective,
            },
        }
        trace = build_trace("best_set", args, timer)
        if not pack.chosen:
            result["note"] = f"{EMPTY_NOTE} vertex-disjoint swap cycle in this graph"
            return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=trace)
        return envelope(Outcome.ANSWERED_WITH_PATH, result, pack.chosen, trace=trace)

    def true_cycle_optimum(self) -> dict[str, Any]:
        """The unbounded cycle cover (any cycle length) from the graph's edge list, for the proof."""
        timer = Timer()
        edges = can_give_edges(self.graph)
        n_pairs = self.n_pairs()
        cover = unbounded_cycle_cover(edges)
        timer.stop(len(edges))

        result = {
            "unbounded_cycle_cover": cover.covered,
            "n_pairs": n_pairs,
            "edges": len(edges),
            "description": "maximum cycle cover, cycles of any length (assignment with self-loops)",
        }
        trace = build_trace("true_cycle_optimum", {}, timer)
        if not cover.covered:
            result["note"] = f"{EMPTY_NOTE} cycle of donors and patients in this graph"
            return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=trace)
        return envelope(
            Outcome.ANSWERED_WITH_PATH, result, [[a, b] for a, b in cover.arcs], trace=trace
        )

    # -- M5 ---------------------------------------------------------------------------------

    def what_if(self, kind: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Re-run the tools on a modified copy of the pool, in a scratch graph, and diff them."""
        from swap.whatif import run_what_if

        return run_what_if(self, kind, dict(args or {}))

    def altruist_ids(self) -> list[int]:
        """Donor-only altruist node ids in this graph (only a what-if scratch graph has any)."""
        return [int(row[0]) for row in self.graph.query(ALTRUIST_IDS).result_set]

    def _chain_candidates(self) -> tuple[list[list[int]], list[list[int]]]:
        """Chains from the graph's altruists as patient sets, with chain-local at-most-one groups."""
        chains: list[list[int]] = []
        groups: list[list[int]] = []
        for altruist in self.altruist_ids():
            start = len(chains)
            for row in self.graph.query(CHAIN_DIRECT, {"id": altruist}).result_set:
                chains.append([int(row[0])])
            for row in self.graph.query(CHAIN_TWO, {"id": altruist}).result_set:
                chains.append([int(row[0]), int(row[1])])
            if len(chains) > start:
                groups.append(list(range(start, len(chains))))
        return chains, groups

    # -- graph reads the tools need ---------------------------------------------------------

    def n_pairs(self) -> int:
        return int(self.graph.query(COUNT_PAIRS).result_set[0][0])

    def hard_pair_ids(self) -> list[int]:
        return [int(r[0]) for r in self.graph.query(HARD_PAIR_IDS).result_set]

    def _cycles_through(self, pair_id: int) -> list[list[int]]:
        seen: set[tuple[int, ...]] = set()
        cycles: list[list[int]] = []
        for statement in (CYCLES_THROUGH_2, CYCLES_THROUGH_3):
            for row in self.graph.query(statement, {"id": pair_id}).result_set:
                cycle = _rotate_to_min([int(v) for v in row])
                key = tuple(cycle)
                if key not in seen:
                    seen.add(key)
                    cycles.append(cycle)
        return sorted(cycles)
