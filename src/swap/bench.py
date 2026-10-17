"""`make graph-bench`: time the graph work behind the headline loop, through the real tools.

Loads a pool (default N=300, seed 1000) and plays the loop a coordinator would: best set, draft,
approve, a positive crossmatch on an approved 3-way swap, its impact, a repair, an explanation and
a what-if. Every tool reports the statements it ran (`graph_work`); this prints the median time of
each over `--runs` fresh loads, as the Markdown table the README quotes.
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from time import perf_counter
from typing import Any

from swap.generator import generate_pool
from swap.graph import connect, drop_graph, load_pool
from swap.tools import SwapTools

GRAPH = "swap_graph_bench"

#: (statement name as the tools report it, what the graph does there), in loop order.
ROWS: tuple[tuple[str, str], ...] = (
    ("materialise_3", "store every 3-way swap as a `:Cycle`, counting its back-arcs"),
    ("materialise_2", "store every 2-way swap as a `:Cycle`"),
    ("candidates_all", "read the live swaps the integer program chooses from"),
    ("guard_pairs", "guard a draft: swaps distinct, no pair twice, none dead or taken"),
    ("guard_arcs", "guard a draft: every donor -> patient arc still exists"),
    ("add_selects", "link the plan to the swaps it chose"),
    ("commit_pairs", "approval: commit the plan's pairs"),
    ("mark_taken", "approval: mark every swap touching a committed pair as taken"),
    ("break_cycles", "positive crossmatch: kill every swap that used that arc, link the test to each"),
    ("release_stranded", "release the pairs of the approved swap it broke"),
    ("retake", "re-open swaps through the released pairs where nothing else holds them"),
    ("rescue_embedded_2way", "find the 2-way swap still alive inside the broken 3-way swap"),
    ("repair_candidates", "read the live swaps that can re-match the stranded patients"),
    ("explain_blocked_by", "why unmatched: the chosen swaps that block this patient (4 hops)"),
    ("explain_shortest_ring", "why unmatched: the shortest ring back to this patient"),
    ("whatif_copy", "what-if: `GRAPH.COPY` of the whole graph inside FalkorDB"),
    ("flow_bound", "`algo.maxFlow` over the donor -> patient network"),
)


def _play(tools: SwapTools, n: int, seed: int) -> dict[str, tuple[float, int]]:
    """One pass of the loop on a fresh load; returns {statement: (ms, rows)}."""
    seen: dict[str, tuple[float, int]] = {}

    def note(work: list[dict[str, Any]]) -> None:
        for step in work:
            seen.setdefault(step["name"], (step["ms"], step["rows"]))

    stats = load_pool(generate_pool(n, seed), GRAPH, db=tools.db)
    for length in (3, 2):
        seen[f"materialise_{length}"] = (stats.cycle_ms[length], stats.cycles_by_len[length])

    note(tools.best_set(3)["result"]["graph_work"])
    draft = tools.draft_plan(3)["result"]
    note(draft["graph_work"])
    note(tools.decide_plan(draft["plan"]["id"], "approve", "Bench")["result"]["graph_work"])

    keys = [swap["key"] for swap in draft["swaps"] if swap["size"] == 3]
    members = tools.graph.query(
        "UNWIND $keys AS k MATCH (c:Cycle {key: k}) WHERE c.back_arcs > 0 RETURN c.members LIMIT 1",
        {"keys": keys},
    ).result_set[0][0]
    recorded = tools.record_crossmatch(members[0], members[1], "positive", "Bench")["result"]
    note(recorded["graph_work"])
    note(recorded["impact"]["graph_work"])
    note(tools.draft_repair(recorded["crossmatch"]["id"])["result"]["graph_work"])

    waiting = tools.graph.query(
        "MATCH (p:Pair) WHERE p.committed IS NULL MATCH (p)<-[:HAS]-(c:Cycle) WHERE c.live "
        "RETURN p.id LIMIT 1"
    ).result_set
    if waiting:
        note(tools.explain_unmatched(int(waiting[0][0]))["result"]["graph_work"])

    copied = tools.what_if("remove_pair", {"pair_id": members[2]})["result"]["scratch"]
    seen["whatif_copy"] = (copied["copy_ms"], 1)
    started = perf_counter()
    bound = tools.flow_bound()["result"]
    seen["flow_bound"] = ((perf_counter() - started) * 1000, bound["flow_bound"])
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=300)
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument("--runs", type=int, default=5)
    options = parser.parse_args(argv)

    db = connect()
    tools = SwapTools(GRAPH, db=db)
    timings: dict[str, list[float]] = defaultdict(list)
    rows: dict[str, int] = {}
    try:
        for _ in range(options.runs):
            tools = SwapTools(GRAPH, db=db)
            for name, (ms, count) in _play(tools, options.n, options.seed).items():
                timings[name].append(ms)
                rows[name] = count
    finally:
        drop_graph(db, GRAPH)

    print(f"N={options.n} pairs, seed {options.seed}, median of {options.runs} fresh loads\n")
    print("| step | what FalkorDB does | ms | rows |")
    print("|---|---|---:|---:|")
    for name, what in ROWS:
        if name in timings:
            ms = statistics.median(timings[name])
            print(f"| `{name}` | {what} | {ms:.1f} | {rows[name]:,} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
