"""`make failure-story`: replay the headline loop on a fixed pool and print what the graph did.

Synthetic pool N=200, seed 1000. The agent drafts a plan, a person approves it, the lab reports a
positive crossmatch on the first arc of the first approved 3-way swap, the graph reacts, the agent
drafts a repair, a person approves it. Every number the README tells about this loop comes from here.
"""

from __future__ import annotations

import argparse

from swap.generator import generate_pool
from swap.graph import connect, drop_graph, load_pool
from swap.tools import SwapTools

GRAPH = "swap_failure_story"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--seed", type=int, default=1000)
    options = parser.parse_args(argv)

    db = connect()
    load_pool(generate_pool(options.n, options.seed), GRAPH, db=db)
    tools = SwapTools(GRAPH, db=db)
    try:
        draft = tools.draft_plan(3)["result"]
        plan = draft["plan"]
        print(f"1. draft plan {plan['id']}: {plan['swaps']} swaps, {plan['matched']} patients; "
              f"FalkorDB guard: {draft['guard']}")
        approved = tools.decide_plan(plan["id"], "approve", "Coordinator")["result"]
        print(f"2. approved by a person: {approved['effects']}")
        swap = next(s for s in approved["swaps"] if s["size"] == 3)
        step = swap["steps"][0]
        recorded = tools.record_crossmatch(
            step["donor_pair"], step["patient_pair"], "positive", "Lab"
        )["result"]
        impact = recorded["impact"]
        print(f"3. positive crossmatch: donor of pair {step['donor_pair']} -> patient of pair "
              f"{step['patient_pair']}")
        print(f"   swaps killed: {impact['swaps_killed']}; planned swaps broken: "
              f"{[b['key'] for b in impact['broken_planned_swaps']]}; stranded: "
              f"{[p['id'] for p in impact['stranded']]}")
        print(f"   2-way swap still alive inside it: {[r['rescue'] for r in impact['rescue']]}; "
              f"live options left: {impact['options_left']}")
        print(f"   graph work: {[(w['name'], w['ms']) for w in recorded['graph_work']]}")
        repair = tools.draft_repair(recorded["crossmatch"]["id"])
        if repair["outcome"] != "answered_with_path":
            print(f"4. no repair: {repair['result'].get('note')}")
            return 0
        found = repair["result"]
        print(f"4. repair plan {found['plan']['id']} drafted: {found['repair']}")
        done = tools.decide_plan(found["plan"]["id"], "approve", "Coordinator")["result"]
        print(f"5. repair approved by a person: {done['effects']}")
    finally:
        drop_graph(db, GRAPH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
