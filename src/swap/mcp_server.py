"""M7: the MCP server exposing the fixed tool catalogue over stdio (`python -m swap.mcp_server`).

The catalogue is fixed and human-written: the model chooses a tool and typed arguments, never
Cypher. Every tool answers over one active FalkorDB graph (default `pool`), loads it with
`generate_pool`, and returns the four-outcome envelope
`{outcome, result, path, trace:{tool, args, ms, rows}}`. `path` is the evidence the answer walked;
an empty traversal is `not_in_graph` ("the graph does not know"), an id the graph lacks is
`unknown_entity`.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from swap.config import DEFAULT_ACTIVE_GRAPH
from swap.envelope import Outcome, envelope, timed
from swap.generator import generate_pool as draw_pool
from swap.graph import load_pool
from swap.tools import SwapTools

ACTIVE_GRAPH = DEFAULT_ACTIVE_GRAPH

INSTRUCTIONS = (
    "Kidney paired-exchange decision support over a FalkorDB graph of SYNTHETIC patients (not "
    "medical advice, no real data). Call generate_pool first to load a pool, then: find_cycles "
    "(swaps FalkorDB finds), flow_bound (an upper bound that also allows chains), best_set "
    "(vertex-disjoint packing of the graph's cycles), true_cycle_optimum (any cycle length), "
    "explain_unmatched (who could give to a patient, and why others cannot), what_if (add an "
    "altruistic donor, remove a pair, or set a priority), draft_plan (record a DRAFT plan in the graph; "
    "only a person can approve it, there is no tool for that), get_plan and list_plans. Every tool returns {outcome, result, "
    "path, trace}; an empty traversal means the graph does not know the answer, not that it is safe."
)

mcp = MCPServer("swap", instructions=INSTRUCTIONS, version="0.1.0", description="Kidney paired-exchange")


def _tools() -> SwapTools:
    return SwapTools(ACTIVE_GRAPH)


@mcp.tool()
def generate_pool(n: int = 100, seed: int = 1) -> dict[str, Any]:
    """Generate and load the active pool.

    Draws `n` synthetic incompatible donor/patient pairs deterministically from `seed`, loads them
    into the active FalkorDB graph, and returns its summary. Call this before any other tool; a
    later call replaces the pool.
    """
    with timed() as timer:
        pool = draw_pool(n, seed)
        stats = load_pool(pool, ACTIVE_GRAPH)
        timer.rows = pool.n
    result = {"graph": ACTIVE_GRAPH, "summary": pool.summary(), "load": stats.summary()}
    return envelope(
        Outcome.ANSWERED_WITH_PATH,
        result,
        (),
        tool="generate_pool",
        args={"n": n, "seed": seed},
        timer=timer,
    )


@mcp.tool()
def find_cycles(max_len: Literal[2, 3, 4] = 3) -> dict[str, Any]:
    """Cycles of length 2..max_len, produced BY FalkorDB.

    Returns the cycles as ordered pair-id lists, per-length counts, and the total. Each directed
    cycle appears once (rotated to its smallest id). Empty means the graph knows of no such cycle.
    """
    return _tools().find_cycles(int(max_len))


@mcp.tool()
def flow_bound() -> dict[str, Any]:
    """The `algo.maxFlow` matching bound over the graph's donor/patient network.

    This is an upper bound that also allows chains (a matching relaxation), NOT the cycle optimum.
    Returns the bound and the matched donor -> patient arcs FalkorDB reports.
    """
    return _tools().flow_bound()


@mcp.tool()
def best_set(max_len: Literal[2, 3, 4] = 3, priority: float | None = None) -> dict[str, Any]:
    """Choose vertex-disjoint swaps maximising matched patients.

    Packs the cycles FalkorDB found (lengths 2..max_len) with SciPy `milp`. `priority` > 1 weights
    highly sensitised (PRA 0.90) patients when choosing. Returns the chosen cycles (and altruist
    chains, if any), and the matched patient count.
    """
    return _tools().best_set(int(max_len), priority)


@mcp.tool()
def true_cycle_optimum() -> dict[str, Any]:
    """The unbounded cycle optimum, for the proof panel.

    Maximum cycle cover with cycles of any length, from the graph's edge list (a SciPy assignment
    with self-loops). Always >= best_set(max_len) and <= flow_bound.
    """
    return _tools().true_cycle_optimum()


@mcp.tool()
def explain_unmatched(pair_id: int) -> dict[str, Any]:
    """Explain one pair: who could give to its patient, why others cannot, and its cycles.

    Resolves the id against the graph first. Unknown id -> outcome `unknown_entity`. No incoming
    donor -> outcome `not_in_graph` (an empty traversal means the graph does not know, which is not
    the same as safe).
    """
    return _tools().explain_unmatched(pair_id)


@mcp.tool()
def what_if(
    kind: Literal["add_altruistic_donor", "remove_pair", "set_priority"],
    donor_abo: Literal["O", "A", "B", "AB"] | None = None,
    pair_id: int | None = None,
    weight: float | None = None,
    max_len: Literal[2, 3, 4] = 3,
) -> dict[str, Any]:
    """Re-run the tools on a modified copy of the pool and report what changes.

    `add_altruistic_donor` needs `donor_abo` (a donor with no patient, matched directly or as a
    two-hop chain); `remove_pair` and `set_priority` need `pair_id` (`set_priority` takes an
    optional `weight`, default 2.0). Returns before/after matched counts and the changed cycles or
    paths. A pair id the graph does not hold -> outcome `unknown_entity`.
    """
    args: dict[str, Any] = {"max_len": int(max_len)}
    if donor_abo is not None:
        args["donor_abo"] = donor_abo
    if pair_id is not None:
        args["pair_id"] = pair_id
    if weight is not None:
        args["weight"] = weight
    return _tools().what_if(kind, args)


@mcp.tool()
def draft_plan(max_len: Literal[2, 3, 4] = 3, priority: float | None = None) -> dict[str, Any]:
    """Draft a swap plan from the best set and record it in the graph as a DRAFT.

    Skips pairs already committed by an approved plan. Approving or rejecting a plan is a human
    action and is deliberately not available as a tool.
    """
    return _tools().draft_plan(int(max_len), priority)


@mcp.tool()
def get_plan(plan_id: int) -> dict[str, Any]:
    """Read one plan, its swaps and its approval record out of the graph. Unknown id -> `unknown_entity`."""
    return _tools().get_plan(plan_id)


@mcp.tool()
def list_plans() -> dict[str, Any]:
    """Every plan in the graph with its status, and the pairs approved plans have committed."""
    return _tools().list_plans()


def main() -> None:
    """Serve the catalogue over stdio (the transport an MCP client launches)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
