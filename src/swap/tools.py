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
from swap.graph import COUNT_FLOW_EDGES, can_give_edges, connect, ensure_result_set_size
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
    "CREATE (pl:Plan {id: $id, status: 'draft', created_at: $created_at, max_len: $max_len, "
    "priority: $priority, matched: $matched, swaps: $swaps}) RETURN pl.id"
)
ADD_PLAN_STEPS = (
    "UNWIND $rows AS r MATCH (pl:Plan {id: $plan}), (p:Pair {id: r[2]}) "
    "CREATE (pl)-[:PROPOSES {swap: r[0], pos: r[1]}]->(p) RETURN count(*)"
)
READ_PLAN = (
    "MATCH (pl:Plan {id: $id}) RETURN pl.id, pl.status, pl.created_at, pl.max_len, pl.priority, "
    "pl.matched, pl.swaps, pl.decided_by, pl.decided_at"
)
READ_PLAN_STEPS = (
    "MATCH (pl:Plan {id: $id})-[r:PROPOSES]->(p:Pair) "
    "RETURN r.swap, r.pos, p.id, p.patient_abo, p.donor_abo, p.pra, p.hard ORDER BY r.swap, r.pos"
)
LIST_PLANS = (
    "MATCH (pl:Plan) RETURN pl.id, pl.status, pl.created_at, pl.max_len, pl.matched, pl.swaps, "
    "pl.decided_by ORDER BY pl.id"
)
DECIDE_PLAN = (
    "MATCH (pl:Plan {id: $id}) SET pl.status = $status, pl.decided_by = $by, pl.decided_at = $at "
    "RETURN pl.id"
)
COMMITTED_PAIR_IDS = (
    "MATCH (pl:Plan {status: 'approved'})-[:PROPOSES]->(p:Pair) RETURN DISTINCT p.id ORDER BY p.id"
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
    "next_plan_id": NEXT_PLAN_ID,
    "create_plan": CREATE_PLAN,
    "add_plan_steps": ADD_PLAN_STEPS,
    "read_plan": READ_PLAN,
    "read_plan_steps": READ_PLAN_STEPS,
    "list_plans": LIST_PLANS,
    "decide_plan": DECIDE_PLAN,
    "committed_pair_ids": COMMITTED_PAIR_IDS,
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


class PlanStateError(ValueError):
    """A plan decision that is not allowed from the plan's current status (the API answers 409)."""


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

    def best_set(
        self,
        max_len: int = 3,
        priority: float | None = None,
        exclude: Collection[int] = (),
    ) -> dict[str, Any]:
        """Vertex-disjoint packing of the graph's candidate cycles (and altruist chains).

        `exclude` drops every candidate cycle that touches one of those pair ids (used to plan
        around pairs already committed by an approved plan).
        """
        timer = Timer()
        weight = 1.0 if priority is None else float(priority)
        args = {"max_len": max_len, "priority": priority}

        found = self.find_cycles(max_len)
        cycles = found["result"]["cycles"]
        if exclude:
            skip = {int(value) for value in exclude}
            cycles = [cycle for cycle in cycles if skip.isdisjoint(cycle)]
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

    # -- the action: draft a plan, a human decides --------------------------------------------

    def committed_pair_ids(self) -> list[int]:
        """Pairs that an approved plan has committed (read from the graph)."""
        return [int(row[0]) for row in self.graph.query(COMMITTED_PAIR_IDS).result_set]

    def draft_plan(self, max_len: int = 3, priority: float | None = None) -> dict[str, Any]:
        """Draft a swap plan from the best set and record it in the graph with status `draft`.

        Pairs already committed by an approved plan are skipped. The plan stays a draft until a
        person approves it; there is no tool for that, on purpose.
        """
        timer = Timer()
        committed = self.committed_pair_ids()
        best = self.best_set(max_len, priority, exclude=committed)
        cycles = best["result"]["cycles"]
        args = {"max_len": max_len, "priority": priority}
        if not cycles:
            timer.stop(0)
            result = {
                "plan": None,
                "note": f"{EMPTY_NOTE} swap to plan among the pairs not already committed",
                "excluded_committed": len(committed),
            }
            return envelope(
                Outcome.NOT_IN_GRAPH, result, (), trace=build_trace("draft_plan", args, timer)
            )

        plan_id = int(self.graph.query(NEXT_PLAN_ID).result_set[0][0])
        self.graph.query(
            CREATE_PLAN,
            {
                "id": plan_id,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "max_len": max_len,
                "priority": 1.0 if priority is None else float(priority),
                "matched": best["result"]["matched"],
                "swaps": len(cycles),
            },
        )
        rows = [[i, pos, pair] for i, cycle in enumerate(cycles) for pos, pair in enumerate(cycle)]
        written = self.graph.query(ADD_PLAN_STEPS, {"plan": plan_id, "rows": rows}).result_set
        if int(written[0][0]) != len(rows):
            raise RuntimeError(f"plan {plan_id}: wrote {written[0][0]} of {len(rows)} steps")
        timer.stop(len(rows) + 2)
        packet = self._packet(plan_id, excluded_committed=len(committed))
        packet["note"] = "the plan is a draft in the graph; a person has to approve it"
        return envelope(
            Outcome.ANSWERED_WITH_PATH,
            packet,
            cycles,
            trace=build_trace("draft_plan", args, timer),
        )

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
        self.graph.query(
            DECIDE_PLAN,
            {
                "id": pid,
                "status": DECISIONS[decision],
                "by": who,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
        packet = self._packet(pid)
        timer.stop(2)
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

        swaps = []
        hard_matched = 0
        for index in sorted(by_swap):
            members = by_swap[index]
            hard_matched += sum(1 for member in members if member[4])
            pairs = [member[0] for member in members]
            swaps.append(
                {
                    "index": index + 1,
                    "size": len(members),
                    "pairs": pairs,
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
