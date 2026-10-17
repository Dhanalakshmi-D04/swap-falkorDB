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

from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from falkordb import FalkorDB, Graph

from swap import cache
from swap.config import DEFAULT_GRAPH, Settings
from swap.envelope import (
    Outcome,
    Timer,
    UnknownEntity,
    build_trace,
    envelope,
    resolve_pair,
)
from swap.explain import EXPLAIN_CYPHER
from swap.failure import FAILURE_CYPHER
from swap.graph import (
    COUNT_FLOW_EDGES,
    can_give_edges,
    connect,
    ensure_result_set_size,
    ensure_swap_objects,
)
from swap.solve import cycle_weight, pack_cycles, unbounded_cycle_cover

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

# Swap objects (graph.MATERIALISE_2/3 stores every 2- and 3-way swap as a :Cycle). The planner reads
# its candidates from them, in the same order as FIND_CYCLES_2/3 return the rings, scored by what the
# graph stored: `hard` (highly sensitised members) and `back_arcs`. `live` = no failed crossmatch on
# any arc; `taken` = a member is committed by an approved plan.
CANDIDATES_ALL = (
    "MATCH (c:Cycle) WHERE c.live AND c.len <= $max_len "
    "RETURN c.key, c.members, c.hard, c.back_arcs ORDER BY c.len, c.members"
)
CANDIDATES_AVAILABLE = (
    "MATCH (c:Cycle) WHERE c.live AND NOT c.taken AND c.len <= $max_len "
    "RETURN c.key, c.members, c.hard, c.back_arcs ORDER BY c.len, c.members"
)
COUNT_SWAP_OBJECTS = "MATCH (c:Cycle) RETURN count(c)"
# A fingerprint of everything the packing reads (cheap next to the integer program it saves).
SWAP_FINGERPRINT = (
    "MATCH (c:Cycle) RETURN count(c), sum(CASE WHEN c.live THEN 1 ELSE 0 END), "
    "sum(CASE WHEN c.taken THEN 1 ELSE 0 END), sum(c.hard), sum(c.members[0] * 1000 + c.members[1])"
)
POOL_FINGERPRINT = (
    "MATCH (p:Pair) OPTIONAL MATCH (a:Altruist) RETURN count(DISTINCT p), count(DISTINCT a)"
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
# A plain MATCH (not OPTIONAL) keeps this honest when the pool holds no other pair: the aggregate
# over zero rows is 0/0, whereas OPTIONAL MATCH would count the null row as incompatible.
EXPLAIN_ABO_SPLIT = (
    "MATCH (p:Pair {id: $id}) MATCH (q:Pair) WHERE q.id <> p.id "
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

# The action: a drafted plan is written back into the graph as a :Plan node that PROPOSES the pairs
# of each chosen swap (swap = index of the cycle, pos = position in it). A human decision sets the
# status; approved plans commit their pairs, and the next draft skips them.
NEXT_PLAN_ID = "MATCH (pl:Plan) RETURN coalesce(max(pl.id), 0) + 1"
CREATE_PLAN = (
    "CREATE (pl:Plan {id: $id, kind: $kind, status: 'draft', created_at: $created_at, "
    "max_len: $max_len, priority: $priority, matched: $matched, swaps: $swaps}) RETURN pl.id"
)
# Each plan points at the swap objects it chose, so a later crossmatch failure can find it.
ADD_SELECTS = (
    "MATCH (pl:Plan {id: $plan}) UNWIND $keys AS k MATCH (c:Cycle {key: k}) "
    "CREATE (pl)-[:SELECTS]->(c) RETURN count(*)"
)
# The guard FalkorDB runs before a draft is written: the chosen swaps are distinct, share no pair,
# are all alive and not taken, and every donor -> patient arc in them still exists.
GUARD_PAIRS = (
    "UNWIND $keys AS k MATCH (c:Cycle {key: k}) UNWIND c.members AS m "
    "RETURN count(DISTINCT c), count(m), count(DISTINCT m), "
    "sum(CASE WHEN c.live THEN 0 ELSE 1 END), sum(CASE WHEN c.taken THEN 1 ELSE 0 END)"
)
GUARD_ARCS = (
    "UNWIND $keys AS k MATCH (c:Cycle {key: k}) UNWIND range(0, c.len - 1) AS i "
    "WITH c.members[i] AS a, c.members[(i + 1) % c.len] AS b "
    "MATCH (x:Pair {id: a}), (y:Pair {id: b}) OPTIONAL MATCH (x)-[r:CAN_GIVE]->(y) "
    "RETURN count(*), sum(CASE WHEN r IS NULL THEN 1 ELSE 0 END)"
)
ADD_PLAN_STEPS = (
    "UNWIND $rows AS r MATCH (pl:Plan {id: $plan}), (p:Pair {id: r[2]}) "
    "CREATE (pl)-[:PROPOSES {swap: r[0], pos: r[1]}]->(p) RETURN count(*)"
)
READ_PLAN = (
    "MATCH (pl:Plan {id: $id}) RETURN pl.id, pl.status, pl.created_at, pl.max_len, pl.priority, "
    "pl.matched, pl.swaps, pl.decided_by, pl.decided_at, pl.kind"
)
READ_PLAN_CYCLES = (
    "MATCH (pl:Plan {id: $id})-[:SELECTS]->(c:Cycle) OPTIONAL MATCH (t:Crossmatch)-[:BROKE]->(c) "
    "RETURN c.key, c.members, c.live, collect(t.id)"
)
READ_PLAN_STEPS = (
    "MATCH (pl:Plan {id: $id})-[r:PROPOSES]->(p:Pair) "
    "RETURN r.swap, r.pos, p.id, p.patient_abo, p.donor_abo, p.pra, p.hard ORDER BY r.swap, r.pos"
)
LIST_PLANS = (
    "MATCH (pl:Plan) RETURN pl.id, pl.status, pl.created_at, pl.max_len, pl.matched, pl.swaps, "
    "pl.decided_by, pl.kind ORDER BY pl.id"
)
DECIDE_PLAN = (
    "MATCH (pl:Plan {id: $id}) SET pl.status = $status, pl.decided_by = $by, pl.decided_at = $at "
    "RETURN pl.id"
)
# A plan can only be approved while every swap it chose is still alive and untaken.
PLAN_STALE_CHECK = (
    "MATCH (:Plan {id: $id})-[:SELECTS]->(c:Cycle) RETURN count(c), "
    "sum(CASE WHEN c.live THEN 0 ELSE 1 END), sum(CASE WHEN c.taken THEN 1 ELSE 0 END)"
)
# Approval commits the pairs, then marks every swap that touches a committed pair as taken (a scan
# of the stored member lists: one pass over the swap objects, not one traversal per pair).
COMMIT_PAIRS = (
    "MATCH (:Plan {id: $id})-[:SELECTS]->(c:Cycle) UNWIND c.members AS m MATCH (p:Pair {id: m}) "
    "SET p.committed = $id RETURN collect(p.id)"
)
MARK_TAKEN = (
    "MATCH (c:Cycle) WHERE NOT c.taken AND any(m IN c.members WHERE m IN $ids) "
    "SET c.taken = true RETURN count(c)"
)
COMMITTED_PAIR_IDS = "MATCH (p:Pair) WHERE p.committed IS NOT NULL RETURN p.id ORDER BY p.id"

CYPHER: dict[str, str] = {
    "count_pairs": COUNT_PAIRS,
    "hard_pair_ids": HARD_PAIR_IDS,
    "find_cycles_2": FIND_CYCLES_2,
    "find_cycles_3": FIND_CYCLES_3,
    "find_cycles_4": FIND_CYCLES_4,
    "candidates_all": CANDIDATES_ALL,
    "candidates_available": CANDIDATES_AVAILABLE,
    "count_swap_objects": COUNT_SWAP_OBJECTS,
    "swap_fingerprint": SWAP_FINGERPRINT,
    "pool_fingerprint": POOL_FINGERPRINT,
    "flow_bound": FLOW_BOUND,
    "explain_incoming": EXPLAIN_INCOMING,
    "explain_outgoing": EXPLAIN_OUTGOING,
    "explain_abo_split": EXPLAIN_ABO_SPLIT,
    "cycles_through_2": CYCLES_THROUGH_2,
    "cycles_through_3": CYCLES_THROUGH_3,
    "altruist_ids": ALTRUIST_IDS,
    "chain_direct": CHAIN_DIRECT,
    "chain_two": CHAIN_TWO,
    "next_plan_id": NEXT_PLAN_ID,
    "create_plan": CREATE_PLAN,
    "add_plan_steps": ADD_PLAN_STEPS,
    "add_selects": ADD_SELECTS,
    "guard_pairs": GUARD_PAIRS,
    "guard_arcs": GUARD_ARCS,
    "read_plan": READ_PLAN,
    "read_plan_cycles": READ_PLAN_CYCLES,
    "read_plan_steps": READ_PLAN_STEPS,
    "list_plans": LIST_PLANS,
    "decide_plan": DECIDE_PLAN,
    "plan_stale_check": PLAN_STALE_CHECK,
    "commit_pairs": COMMIT_PAIRS,
    "mark_taken": MARK_TAKEN,
    "committed_pair_ids": COMMITTED_PAIR_IDS,
    **FAILURE_CYPHER,
    **EXPLAIN_CYPHER,
}

CYCLES_BY_LENGTH = {2: FIND_CYCLES_2, 3: FIND_CYCLES_3, 4: FIND_CYCLES_4}

FLOW_BOUND_CAVEAT = (
    "upper bound that also allows chains (a matching relaxation), NOT the cycle optimum"
)
EMPTY_NOTE = "the graph does not know of any"

PLAN_NOTICE = (
    "Synthetic patients only. Decision support, not medical advice. A person must approve this "
    "plan; Swap cannot."
)
DECISIONS = {"approve": "approved", "reject": "rejected"}


class SolverError(RuntimeError):
    """The packing solver gave up (time limit) with swaps on the table: not the same as "no swap"."""


class PlanStateError(ValueError):
    """A plan decision that is not allowed from the plan's current status (the API answers 409)."""


class PlanGuardError(RuntimeError):
    """FalkorDB's pre-write check rejected a drafted set of swaps; nothing was written."""


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
        self.work: list[dict[str, Any]] = []
        self._swap_objects_checked = False

    def _run(self, name: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Run one named statement and note what the graph did (name, ms, rows) in `self.work`."""
        started = perf_counter()
        rows = self.graph.query(CYPHER[name], params or {}).result_set
        step = {"name": name, "ms": round((perf_counter() - started) * 1000, 1), "rows": len(rows)}
        if len(rows) == 1 and rows[0]:
            first = rows[0][0]
            if isinstance(first, list):
                step["value"] = len(first)
            elif isinstance(first, (int, float)) and not isinstance(first, bool):
                step["value"] = int(first)
        self.work.append(step)
        return rows

    def _ensure_swap_objects(self) -> None:
        if not self._swap_objects_checked:
            ensure_swap_objects(self.graph)
            self._swap_objects_checked = True

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
        # `algo.maxFlow` errors when the graph holds no `:F` relationship at all (an empty graph, or
        # a pool of zero pairs, which has S and T nodes but no flow arc). That is an empty
        # traversal, not a failure: answer `not_in_graph` with a zero bound.
        if self.n_flow_edges() == 0:
            timer.stop(0)
            result = {
                "flow_bound": 0,
                "matched_count": 0,
                "matched": [],
                "description": FLOW_BOUND_CAVEAT,
                "note": f"{EMPTY_NOTE} flow arc in this graph (no donor/patient network to match)",
            }
            return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=build_trace("flow_bound", {}, timer))

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

    def explain_unmatched(self, pair_id: Any, plan_id: int | None = None) -> dict[str, Any]:
        """Why this patient is (not) in a swap, walked in the graph, against a plan or the best set.

        Who could give to this patient and why others cannot, the cycles through the pair, and the
        verdict with its evidence (swaps/explain.py): the chosen swaps that block it, the shortest
        ring back to it, or a donor who can give to nobody.
        """
        from swap.explain import against, reasons

        timer = Timer()
        args = {"pair_id": pair_id, "plan_id": plan_id}
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
        self._ensure_swap_objects()
        self.work = []
        why = reasons(self, pair.id, against(self, plan_id))
        why["graph_work"] = list(self.work)
        timer.stop(len(incoming) + len(outgoing) + len(self.work))

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
            **why,
        }
        evidence = [list(c) for c in cycles_through]
        evidence += [b["members"] for b in why["blocked_by"] if b["members"] not in evidence]
        if why["shortest_ring"]["ring"]:
            evidence.append(why["shortest_ring"]["ring"][:-1])
        trace = build_trace("explain_unmatched", args, timer)
        if not incoming:
            result["note"] = (
                f"{EMPTY_NOTE} donor that can give to this patient (empty traversal: the graph "
                "does not know, which is not the same as safe)"
            )
            return envelope(Outcome.NOT_IN_GRAPH, result, evidence, trace=trace)
        return envelope(Outcome.ANSWERED_WITH_PATH, result, evidence, trace=trace)

    # -- M4 ---------------------------------------------------------------------------------

    def best_set(
        self,
        max_len: int = 3,
        priority: float | None = None,
        exclude: Collection[int] = (),
        available_only: bool = False,
    ) -> dict[str, Any]:
        """Vertex-disjoint packing of the graph's swaps (and altruist chains).

        For 2- and 3-way swaps the candidates are the graph's swap objects, scored by what the graph
        stored: weight = size + (priority - 1) x highly sensitised members. `available_only` skips
        swaps a failed crossmatch killed or an approved plan took (the planner's view); otherwise
        this is the pool-wide optimum. `exclude` drops every candidate touching those pair ids.
        """
        timer = Timer()
        self.work = []
        weight = 1.0 if priority is None else float(priority)
        args = {"max_len": max_len, "priority": priority}
        memo = None
        if max_len in (2, 3):
            self._ensure_swap_objects()
            swaps = tuple(self._run("swap_fingerprint")[0])
            # With nothing dead or taken, "available" is every swap: the same answer.
            same_as_all = swaps[1] == swaps[0] and not swaps[2]
            memo = (
                self.graph_name,
                max_len,
                weight,
                available_only and not same_as_all,
                tuple(sorted(int(v) for v in exclude)),
                *swaps,
                *self._run("pool_fingerprint")[0],
            )
            hit = cache.get(memo)
            if hit is not None:
                result = {
                    **hit["result"],
                    "available_only": available_only,
                    "graph_work": list(self.work),
                    "memo": True,
                }
                timer.stop(len(self.work))
                trace = build_trace("best_set", args, timer)
                return envelope(Outcome(hit["outcome"]), result, hit["path"], trace=trace)

        keys: list[str] = []
        back_arcs: list[int] = []
        storage_note = None
        if max_len == 4:
            found = self.find_cycles(max_len)
            cycles = found["result"]["cycles"]
            weights = [cycle_weight(c, self.hard_pair_ids(), weight) for c in cycles]
            storage_note = "4-way swaps are counted by the graph but not stored as swap objects"
        elif max_len in (2, 3):
            statement = "candidates_available" if available_only else "candidates_all"
            rows = self._run(statement, {"max_len": max_len})
            cycles = [[int(m) for m in row[1]] for row in rows]
            keys = [str(row[0]) for row in rows]
            back_arcs = [int(row[3]) for row in rows]
            weights = [len(row[1]) + (weight - 1.0) * int(row[2]) for row in rows]
        else:
            raise ValueError(f"max_len must be one of {sorted(CYCLES_BY_LENGTH)}, got {max_len!r}")
        if exclude:
            skip = {int(value) for value in exclude}
            kept = [i for i, cycle in enumerate(cycles) if skip.isdisjoint(cycle)]
            cycles = [cycles[i] for i in kept]
            weights = [weights[i] for i in kept]
            keys = [keys[i] for i in kept] if keys else keys
            back_arcs = [back_arcs[i] for i in kept] if back_arcs else back_arcs
        chains, chain_groups = self._chain_candidates()
        if chains:
            hard_ids = self.hard_pair_ids()
            weights = weights + [cycle_weight(chain, hard_ids, weight) for chain in chains]
        # Chains are columns len(cycles).. in the combined candidate list; shift their groups.
        groups = [[column + len(cycles) for column in group] for group in chain_groups]
        pack = pack_cycles(cycles + chains, weights=weights, at_most_one_groups=groups)
        timer.stop(len(cycles) + len(chains) + 1)
        if not pack.chosen and not pack.success and (cycles or chains):
            raise SolverError(
                f"the solver could not finish ({pack.status}) with {len(cycles)} candidate swaps; "
                "try a shorter swap length or a smaller pool"
            )

        cycle_columns = [i for i in pack.indices if i < len(cycles)]
        chosen_cycles = [cycles[i] for i in cycle_columns]
        chosen_chains = [c for i, c in zip(pack.indices, pack.chosen, strict=True) if i >= len(cycles)]
        result = {
            "chosen": pack.chosen,
            "cycles": chosen_cycles,
            "chains": chosen_chains,
            "cycle_keys": [keys[i] for i in cycle_columns] if keys else [],
            "chosen_back_arcs": [back_arcs[i] for i in cycle_columns] if back_arcs else [],
            "counts": {
                "cycles": len(chosen_cycles),
                "chains": len(chosen_chains),
                "patients": pack.covered,
            },
            "matched": pack.covered,
            "candidate_cycles": len(cycles),
            "candidate_chains": len(chains),
            "candidates_from": "cycle pattern match" if max_len == 4 else "swap objects",
            "available_only": available_only,
            "priority_weight": weight,
            "max_len": max_len,
            "solver": {
                "status": pack.status,
                "success": pack.success,
                "objective": pack.objective,
            },
            "graph_work": list(self.work),
        }
        if storage_note:
            result["note_storage"] = storage_note
        trace = build_trace("best_set", args, timer)
        if pack.chosen and not pack.success:
            result["note"] = (
                "the solver stopped at its time limit: this is the best set it found, "
                "not proven to be the best possible"
            )
        if not pack.chosen:
            result["note"] = f"{EMPTY_NOTE} vertex-disjoint swap cycle in this graph"
            answer = envelope(Outcome.NOT_IN_GRAPH, result, (), trace=trace)
        else:
            answer = envelope(Outcome.ANSWERED_WITH_PATH, result, pack.chosen, trace=trace)
        if memo is not None and pack.success:
            cache.put(memo, {"outcome": answer["outcome"], "result": result, "path": answer["path"]})
        return answer

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

    # -- when a crossmatch fails (swap/failure.py) --------------------------------------------

    def record_crossmatch(
        self, donor_pair: Any, patient_pair: Any, result: str, recorded_by: str
    ) -> dict[str, Any]:
        """HUMAN-ONLY: record a lab crossmatch. Not in the agent catalogue or the MCP server."""
        from swap.failure import record_crossmatch

        return record_crossmatch(self, donor_pair, patient_pair, result, recorded_by)

    def failure_impact(self, crossmatch_id: Any) -> dict[str, Any]:
        """What a crossmatch result broke, who it stranded, and what is still alive to save them."""
        from swap.failure import failure_impact

        return failure_impact(self, crossmatch_id)

    def draft_repair(self, crossmatch_id: Any, max_len: int = 3) -> dict[str, Any]:
        """Draft a repair over the swaps still alive, re-matching the stranded patients first."""
        from swap.failure import draft_repair

        return draft_repair(self, crossmatch_id, max_len)

    def list_crossmatches(self) -> dict[str, Any]:
        """Every crossmatch result recorded in the graph."""
        from swap.failure import list_crossmatches

        return list_crossmatches(self)

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

    # -- the action: draft a plan, a human decides --------------------------------------------

    def committed_pair_ids(self) -> list[int]:
        """Pairs that an approved plan has committed (read from the graph)."""
        return [int(row[0]) for row in self.graph.query(COMMITTED_PAIR_IDS).result_set]

    def draft_plan(self, max_len: int = 3, priority: float | None = None) -> dict[str, Any]:
        """Draft a swap plan from the swaps still available and record it in the graph as `draft`.

        Swaps a failed crossmatch killed or an approved plan took are not candidates. FalkorDB checks
        the chosen set before anything is written. The plan stays a draft until a person approves
        it; there is no tool for that, on purpose.
        """
        if max_len not in (2, 3):
            raise ValueError(f"a plan uses 2- or 3-way swaps; max_len must be 2 or 3, got {max_len!r}")
        timer = Timer()
        args = {"max_len": max_len, "priority": priority}
        committed = self.committed_pair_ids()
        best = self.best_set(max_len, priority, available_only=True)["result"]
        work = list(best["graph_work"])
        if not best["cycles"]:
            timer.stop(0)
            result = {
                "plan": None,
                "note": f"{EMPTY_NOTE} swap to plan among the pairs not already committed",
                "excluded_committed": len(committed),
                "graph_work": work,
            }
            return envelope(
                Outcome.NOT_IN_GRAPH, result, (), trace=build_trace("draft_plan", args, timer)
            )

        plan_id, guard = self._write_plan(
            "plan", best["cycles"], best["cycle_keys"], best["matched"], max_len, priority
        )
        work += self.work
        timer.stop(len(best["cycles"]) + 3)
        packet = self._packet(plan_id, excluded_committed=len(committed))
        packet["guard"] = guard
        packet["graph_work"] = work
        packet["note"] = "the plan is a draft in the graph; a person has to approve it"
        return envelope(
            Outcome.ANSWERED_WITH_PATH,
            packet,
            best["cycles"],
            trace=build_trace("draft_plan", args, timer),
        )

    def _write_plan(
        self,
        kind: str,
        cycles: list[list[int]],
        keys: list[str],
        matched: int,
        max_len: int,
        priority: float | None,
    ) -> tuple[int, dict[str, Any]]:
        """Guard the chosen swaps in FalkorDB, then write the Plan, its PROPOSES steps and SELECTS."""
        self.work = []
        guard = self._guard(keys)
        if not guard["ok"]:
            raise PlanGuardError(f"FalkorDB rejected the drafted swaps: {guard}")
        plan_id = int(self._run("next_plan_id")[0][0])
        self._run(
            "create_plan",
            {
                "id": plan_id,
                "kind": kind,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "max_len": max_len,
                "priority": 1.0 if priority is None else float(priority),
                "matched": matched,
                "swaps": len(cycles),
            },
        )
        rows = [[i, pos, pair] for i, cycle in enumerate(cycles) for pos, pair in enumerate(cycle)]
        written = self._run("add_plan_steps", {"plan": plan_id, "rows": rows})
        if int(written[0][0]) != len(rows):
            raise RuntimeError(f"plan {plan_id}: wrote {written[0][0]} of {len(rows)} steps")
        selected = self._run("add_selects", {"plan": plan_id, "keys": keys})
        if int(selected[0][0]) != len(keys):
            raise RuntimeError(f"plan {plan_id}: linked {selected[0][0]} of {len(keys)} swaps")
        return plan_id, guard

    def _guard(self, keys: list[str]) -> dict[str, Any]:
        """FalkorDB's check of a set of swaps: distinct, disjoint, alive, untaken, every arc present."""
        cycles, members, distinct, dead, taken = self._run("guard_pairs", {"keys": keys})[0]
        arcs, missing = self._run("guard_arcs", {"keys": keys})[0]
        guard = {
            "cycles": int(cycles),
            "pairs": int(distinct),
            "arcs_checked": int(arcs),
            "missing_arcs": int(missing or 0),
            "shared_pairs": int(members) - int(distinct),
            "dead": int(dead or 0),
            "taken": int(taken or 0),
        }
        guard["ok"] = (
            guard["cycles"] == len(keys)
            and guard["missing_arcs"] == 0
            and guard["shared_pairs"] == 0
            and guard["dead"] == 0
            and guard["taken"] == 0
        )
        return guard

    def get_plan(self, plan_id: Any) -> dict[str, Any]:
        """Read a plan (and its swaps) back out of the graph; an unknown id is `unknown_entity`."""
        timer = Timer()
        args = {"plan_id": plan_id}
        try:
            packet = self._packet(self._plan_id(plan_id))
        except UnknownEntity as exc:
            return self._unknown_plan(timer, args, exc, "get_plan")
        timer.stop(sum(len(swap["pairs"]) for swap in packet["swaps"]) + 1)
        cycles = [swap["pairs"] for swap in packet["swaps"]]
        return envelope(
            Outcome.ANSWERED_WITH_PATH, packet, cycles, trace=build_trace("get_plan", args, timer)
        )

    def list_plans(self) -> dict[str, Any]:
        """Every plan in the graph with its status, plus the pairs approved plans have committed."""
        timer = Timer()
        rows = self.graph.query(LIST_PLANS).result_set
        plans = [
            {
                "id": int(r[0]),
                "status": str(r[1]),
                "created_at": str(r[2]),
                "max_len": int(r[3]),
                "matched": int(r[4]),
                "swaps": int(r[5]),
                "decided_by": r[6],
                "kind": r[7] or "plan",
            }
            for r in rows
        ]
        committed = self.committed_pair_ids()
        timer.stop(len(plans))
        result: dict[str, Any] = {"plans": plans, "committed": committed}
        trace = build_trace("list_plans", {}, timer)
        if not plans:
            result["note"] = f"{EMPTY_NOTE} plan in this graph yet"
            return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=trace)
        return envelope(Outcome.ANSWERED_WITH_PATH, result, (), trace=trace)

    def decide_plan(self, plan_id: Any, decision: str, decided_by: str) -> dict[str, Any]:
        """HUMAN-ONLY: approve or reject a draft plan. Not in the agent catalogue or the MCP server.

        Records who decided and when. Only a `draft` can be decided, once.
        """
        timer = Timer()
        args = {"plan_id": plan_id, "decision": decision, "decided_by": decided_by}
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {sorted(DECISIONS)}, got {decision!r}")
        who = " ".join(str(decided_by).split())
        if not who or len(who) > 80:
            raise ValueError("decided_by must be a name of 1 to 80 characters")
        try:
            pid = self._plan_id(plan_id)
            status = str(self._read_plan_row(pid)[1])
        except UnknownEntity as exc:
            return self._unknown_plan(timer, args, exc, "decide_plan")
        if status != "draft":
            raise PlanStateError(f"plan {pid} is already {status}; only a draft can be decided")
        self.work = []
        effects: dict[str, Any] = {}
        if decision == "approve":
            swaps, dead, taken = self._run("plan_stale_check", {"id": pid})[0]
            if int(dead or 0) or int(taken or 0):
                raise PlanStateError(
                    f"plan {pid} is stale: {int(dead or 0)} of its {int(swaps)} swaps were broken by a "
                    f"failed crossmatch and {int(taken or 0)} share a pair with an approved plan; "
                    "draft a new plan"
                )
        self._run(
            "decide_plan",
            {
                "id": pid,
                "status": DECISIONS[decision],
                "by": who,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
        if decision == "approve":
            committed = [int(v) for v in self._run("commit_pairs", {"id": pid})[0][0]]
            unavailable = int(self._run("mark_taken", {"ids": committed})[0][0])
            effects = {"committed_pairs": len(committed), "swaps_now_unavailable": unavailable}
        work = list(self.work)
        packet = self._packet(pid)
        packet["effects"] = effects
        packet["graph_work"] = work
        timer.stop(len(work))
        cycles = [swap["pairs"] for swap in packet["swaps"]]
        trace = build_trace("decide_plan", {**args, "decided_by": who}, timer)
        return envelope(Outcome.ANSWERED_WITH_PATH, packet, cycles, trace=trace)

    def _plan_id(self, plan_id: Any) -> int:
        try:
            return int(plan_id)
        except (TypeError, ValueError):
            raise UnknownEntity("Plan", plan_id) from None

    def _read_plan_row(self, plan_id: int) -> list[Any]:
        rows = self.graph.query(READ_PLAN, {"id": plan_id}).result_set
        if not rows:
            raise UnknownEntity("Plan", plan_id)
        return list(rows[0])

    def _unknown_plan(
        self, timer: Timer, args: dict[str, Any], exc: UnknownEntity, tool: str
    ) -> dict[str, Any]:
        timer.stop(0)
        result = {"message": str(exc), "entity": {"kind": exc.kind, "id": exc.entity_id}}
        return envelope(
            Outcome.UNKNOWN_ENTITY, result, (), trace=build_trace(tool, args, timer)
        )

    def _packet(self, plan_id: int, excluded_committed: int | None = None) -> dict[str, Any]:
        """The coordinator packet for one plan, rebuilt from what the graph stores."""
        row = self._read_plan_row(plan_id)
        steps = self.graph.query(READ_PLAN_STEPS, {"id": plan_id}).result_set
        by_swap: dict[int, list[Any]] = {}
        for swap, _pos, pair, patient_abo, donor_abo, pra, hard in steps:
            by_swap.setdefault(int(swap), []).append((int(pair), patient_abo, donor_abo, pra, hard))

        state = {
            tuple(int(m) for m in members): (str(key), bool(live), sorted(int(t) for t in tests))
            for key, members, live, tests in self.graph.query(
                READ_PLAN_CYCLES, {"id": plan_id}
            ).result_set
        }
        swaps = []
        hard_matched = 0
        for index in sorted(by_swap):
            members = by_swap[index]
            hard_matched += sum(1 for member in members if member[4])
            pairs = [member[0] for member in members]
            key, live, broken_by = state.get(tuple(pairs), (None, True, []))
            swaps.append(
                {
                    "index": index + 1,
                    "size": len(members),
                    "pairs": pairs,
                    "key": key,
                    "live": live,
                    "broken_by": broken_by,
                    "steps": [
                        {
                            "donor_pair": members[i][0],
                            "donor_abo": members[i][2],
                            "patient_pair": members[(i + 1) % len(members)][0],
                            "patient_abo": members[(i + 1) % len(members)][1],
                        }
                        for i in range(len(members))
                    ],
                }
            )
        n_pairs = self.n_pairs()
        matched = int(row[5])
        status = str(row[1])
        plan = {
            "id": int(row[0]),
            "status": status,
            "created_at": str(row[2]),
            "max_len": int(row[3]),
            "priority": float(row[4]),
            "matched": matched,
            "swaps": int(row[6]),
            "decided_by": row[7],
            "decided_at": row[8],
            "kind": row[9] or "plan",
        }
        summary = {
            "pairs_in_pool": n_pairs,
            "matched": matched,
            "not_in_plan": n_pairs - matched,
            "hard_total": len(self.hard_pair_ids()),
            "hard_matched": hard_matched,
        }
        if excluded_committed is not None:
            summary["excluded_committed"] = excluded_committed
        return {
            "plan": plan,
            "swaps": swaps,
            "summary": summary,
            "requires_human_approval": status == "draft",
            "notice": PLAN_NOTICE,
        }

    # -- graph reads the tools need ---------------------------------------------------------

    def n_pairs(self) -> int:
        return int(self.graph.query(COUNT_PAIRS).result_set[0][0])

    def n_flow_edges(self) -> int:
        """Count the `:F` arcs; `algo.maxFlow` needs at least one or it raises server-side."""
        return int(self.graph.query(COUNT_FLOW_EDGES).result_set[0][0])

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
