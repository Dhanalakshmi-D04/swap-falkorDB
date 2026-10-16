"""`make verify`: the reproducible numbers the README quotes, computed through the FalkorDB tools.

For N in (100, 200, 300) over K seeds (default 5; `--seeds 20` for the README's longer run) this
generates synthetic pools, loads each into FalkorDB, and reads four numbers per pool through the
graph tools: the 2-way and up-to-3-way optima (`best_set`), the true unbounded cycle cover
(`true_cycle_optimum`) and the `algo.maxFlow` bound (`flow_bound`). Per seed it also checks the
FalkorDB cycle counts and matching against `crosscheck.py`, a labelled independent plain-Python
computation from the same pool (no networkx in `src`). Ties and negatives are reported, not hidden.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

import numpy as np

from swap import crosscheck
from swap.config import settings
from swap.generator import Pool, generate_pool
from swap.graph import connect, drop_graph, load_pool
from swap.tools import SwapTools

SIZES: tuple[int, ...] = (100, 200, 300)
SEED_START = 1000
DEFAULT_SEEDS = 5

#: Priority-weighting measurement: N pairs, best_set max_len, comparison weight, default K.
FAIRNESS_N = 200
FAIRNESS_MAX_LEN = 3
FAIRNESS_WEIGHT = 3.0
DEFAULT_FAIRNESS_SEEDS = 10


@dataclass(frozen=True, slots=True)
class Row:
    """One pool's four numbers plus the per-seed FalkorDB-vs-independent checks."""

    n: int
    seed: int
    two: int
    three: int
    cover: int
    bound: int
    cycles_agree: dict[int, bool]
    bound_agrees: bool
    ordered: bool


def measure(pool: Pool, name: str, db: object) -> Row:
    """Load a pool into FalkorDB and read the four numbers and the checks back out of the graph."""
    load_pool(pool, name, db=db)
    try:
        tools = SwapTools(name, db=db)
        two = tools.best_set(2)["result"]["matched"]
        three = tools.best_set(3)["result"]["matched"]
        cover = tools.true_cycle_optimum()["result"]["unbounded_cycle_cover"]
        bound = tools.flow_bound()["result"]["flow_bound"]
        falkordb_cycles = tools.find_cycles(3)["result"]["counts"]
    finally:
        drop_graph(db, name)

    independent = crosscheck.cycle_counts(pool, 3)
    matching = crosscheck.matching_size(pool)
    agree = {
        length: falkordb_cycles.get(length, 0) == independent.get(length, 0)
        for length in (2, 3)
    }
    return Row(
        n=pool.n,
        seed=pool.seed,
        two=two,
        three=three,
        cover=cover,
        bound=bound,
        cycles_agree=agree,
        bound_agrees=bound == matching,
        ordered=two <= three <= cover <= bound,
    )


def _mean_sd(values: list[int]) -> tuple[float, float]:
    array = np.array(values, dtype=float)
    return float(array.mean()), float(array.std(ddof=1)) if len(array) > 1 else 0.0


def _cell(mean: float, sd: float) -> str:
    return f"{mean:.1f} ± {sd:.1f}"


def _table(rows_by_n: dict[int, list[Row]]) -> list[str]:
    lines = [
        (
            "| N | K | 2-way | up to 3 | unbounded cycle cover | max-flow bound (also allows chains) "
            "| 3-way gain | 2-vs-3, % of N | ≤3 share of unbounded gain |"
        ),
        "|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for n in sorted(rows_by_n):
        rows = rows_by_n[n]
        two_m, two_s = _mean_sd([r.two for r in rows])
        three_m, three_s = _mean_sd([r.three for r in rows])
        cover_m, cover_s = _mean_sd([r.cover for r in rows])
        bound_m, bound_s = _mean_sd([r.bound for r in rows])
        gain = three_m - two_m
        gain_pct = 100.0 * gain / n
        unbounded_gain = cover_m - two_m
        share = (gain / unbounded_gain * 100.0) if unbounded_gain > 0 else float("nan")
        lines.append(
            f"| {n} | {len(rows)} | {_cell(two_m, two_s)} | {_cell(three_m, three_s)} "
            f"| {_cell(cover_m, cover_s)} | {_cell(bound_m, bound_s)} "
            f"| +{gain:.1f} pts | {gain_pct:.1f}% | {share:.0f}% |"
        )
    return lines


def _check_table(rows_by_n: dict[int, list[Row]]) -> tuple[list[str], bool]:
    lines = [
        (
            "| N | K | 2-cycle counts agree | 3-cycle counts agree | flow bound == matching "
            "| 2 ≤ 3 ≤ cover ≤ bound |"
        ),
        "|--:|--:|:--:|:--:|:--:|:--:|",
    ]
    all_pass = True
    for n in sorted(rows_by_n):
        rows = rows_by_n[n]
        c2 = all(r.cycles_agree[2] for r in rows)
        c3 = all(r.cycles_agree[3] for r in rows)
        bound_ok = all(r.bound_agrees for r in rows)
        ordered_ok = all(r.ordered for r in rows)
        all_pass &= c2 and c3 and bound_ok and ordered_ok
        mark = lambda ok: "PASS" if ok else "FAIL"
        lines.append(
            f"| {n} | {len(rows)} | {mark(c2)} | {mark(c3)} | {mark(bound_ok)} | {mark(ordered_ok)} |"
        )
    return lines, all_pass


def _negatives(rows_by_n: dict[int, list[Row]]) -> list[str]:
    flat = [row for n in sorted(rows_by_n) for row in rows_by_n[n]]
    total = len(flat)
    three_adds_nothing = [r for r in flat if r.three == r.two]
    three_is_optimal = [r for r in flat if r.three == r.cover]
    bound_is_tight = [r for r in flat if r.cover == r.bound]
    lines = [
        f"- seeds where a 3-way swap adds no patient over 2-way (up-to-3 == 2-way): "
        f"{len(three_adds_nothing)} of {total}"
        + (f" ({_seeds(three_adds_nothing)})" if three_adds_nothing else ""),
        f"- seeds where 3-cycles already reach the unbounded cycle cover "
        f"(up-to-3 == true cover): {len(three_is_optimal)} of {total}"
        + (f" ({_seeds(three_is_optimal)})" if three_is_optimal else ""),
        (
            f"- seeds where the max-flow bound is tight (chain relaxation adds nothing here): "
            f"{len(bound_is_tight)} of {total}"
        ),
    ]
    if len(three_is_optimal) > 0:
        lines.append(
            "- note: up-to-3 reaching the cover means longer cycles bought nothing on those pools; "
            "the 3-to-4 step is expected to be under 1 patient per 100 pairs (Roth, Sönmez, Ünver 2007; not measured here)."
        )
    return lines


def _seeds(rows: list[Row], limit: int = 8) -> str:
    shown = ", ".join(f"N={row.n} seed {row.seed}" for row in rows[:limit])
    return shown if len(rows) <= limit else f"{shown}, … (+{len(rows) - limit} more)"


@dataclass(frozen=True, slots=True)
class FairRow:
    """One seed's priority-weighting comparison on a fixed pool (`w1` vs a higher weight)."""

    seed: int
    hard_total: int
    hard_w1: int
    hard_w3: int
    total_w1: int
    total_w3: int

    def share(self, hard_matched: int) -> float:
        return 100.0 * hard_matched / self.hard_total if self.hard_total else 0.0

    @property
    def share_w1(self) -> float:
        return self.share(self.hard_w1)

    @property
    def share_w3(self) -> float:
        return self.share(self.hard_w3)


def _matched_patients(best_set_env: dict[str, Any]) -> set[int]:
    """The patient ids covered by a `best_set` envelope's chosen cycles."""
    return {patient for cycle in best_set_env["result"]["chosen"] for patient in cycle}


def measure_fairness(pool: Pool, name: str, db: object, weight: float = FAIRNESS_WEIGHT) -> FairRow:
    """Load a pool and compare `best_set` at priority weight 1 vs `weight` on PRA 0.90 patients."""
    load_pool(pool, name, db=db)
    try:
        tools = SwapTools(name, db=db)
        hard_ids = set(tools.hard_pair_ids())
        matched_w1 = _matched_patients(tools.best_set(FAIRNESS_MAX_LEN, 1.0))
        matched_w3 = _matched_patients(tools.best_set(FAIRNESS_MAX_LEN, weight))
    finally:
        drop_graph(db, name)
    return FairRow(
        seed=pool.seed,
        hard_total=len(hard_ids),
        hard_w1=len(hard_ids & matched_w1),
        hard_w3=len(hard_ids & matched_w3),
        total_w1=len(matched_w1),
        total_w3=len(matched_w3),
    )


def _fair_seeds(rows: list[FairRow], limit: int = 8) -> str:
    shown = ", ".join(str(row.seed) for row in rows[:limit])
    return shown if len(rows) <= limit else f"{shown}, … (+{len(rows) - limit} more)"


def run_fairness(
    seeds: int,
    n: int = FAIRNESS_N,
    seed_start: int = SEED_START,
    weight: float = FAIRNESS_WEIGHT,
) -> int:
    """Measure whether priority weighting lifts the highly sensitised share, and its cost."""
    seed_list = list(range(seed_start, seed_start + seeds))
    cfg = settings()
    db = connect(cfg)
    db.list_graphs()  # fail fast (and loudly) if FalkorDB is not reachable

    print("# swap verify --fairness\n")
    print(
        f"Command: `python -m swap.verify --fairness --seeds {seeds}`  ·  FalkorDB at {cfg.where()}"
    )
    print(
        f"N={n} pairs  ·  seeds {seed_list[0]}..{seed_list[-1]} (K={seeds})  ·  "
        f"best_set(max_len={FAIRNESS_MAX_LEN})  ·  highly sensitised = PRA 0.90  ·  "
        f"priority weight 1 vs {weight:g}\n"
    )
    rows = [
        measure_fairness(generate_pool(n, seed), f"fair_{n}_{seed}", db, weight)
        for seed in seed_list
    ]

    w1_share_m, w1_share_s = _mean_sd([r.share_w1 for r in rows])
    w3_share_m, w3_share_s = _mean_sd([r.share_w3 for r in rows])
    d_m, d_s = _mean_sd([r.share_w3 - r.share_w1 for r in rows])
    w1_total_m, w1_total_s = _mean_sd([r.total_w1 for r in rows])
    w3_total_m, w3_total_s = _mean_sd([r.total_w3 for r in rows])

    print("## Share of highly sensitised (PRA 0.90) patients matched\n")
    print(f"| K | max_len | weight 1 | weight {weight:g} | mean Δ (percentage points) |")
    print("|--:|--:|--:|--:|--:|")
    print(
        f"| {len(rows)} | {FAIRNESS_MAX_LEN} | {w1_share_m:.1f}% ± {w1_share_s:.1f} "
        f"| {w3_share_m:.1f}% ± {w3_share_s:.1f} | {d_m:+.1f} ± {d_s:.1f} |"
    )

    print("\n## Total patients matched (all patients, same pools)\n")
    print(f"| K | weight 1 | weight {weight:g} | mean Δ |")
    print("|--:|--:|--:|--:|")
    print(
        f"| {len(rows)} | {w1_total_m:.1f} ± {w1_total_s:.1f} "
        f"| {w3_total_m:.1f} ± {w3_total_s:.1f} | {w3_total_m - w1_total_m:+.1f} |"
    )

    improved = [r for r in rows if r.hard_w3 > r.hard_w1]
    tied = [r for r in rows if r.hard_w3 == r.hard_w1]
    lower = [r for r in rows if r.hard_w3 < r.hard_w1]
    total_down = [r for r in rows if r.total_w3 < r.total_w1]
    total_up = [r for r in rows if r.total_w3 > r.total_w1]
    print("\n## Per-seed negatives and ties (reported honestly)\n")
    print(
        f"- highly sensitised matched: improved {len(improved)} of {len(rows)}, tied {len(tied)}, "
        f"lower {len(lower)}" + (f" (seeds {_fair_seeds(lower)})" if lower else "")
    )
    print(
        f"- total matched: lower under weight {weight:g} on {len(total_down)} of {len(rows)}, "
        f"higher on {len(total_up)}"
        + (f" (lower: seeds {_fair_seeds(total_down)})" if total_down else "")
    )
    print()
    return 0


def run(seeds: int, sizes: tuple[int, ...] = SIZES, seed_start: int = SEED_START) -> int:
    """Print the reproducible table and checks; return a process exit code (0 on all-pass)."""
    seed_list = list(range(seed_start, seed_start + seeds))
    cfg = settings()
    db = connect(cfg)
    db.list_graphs()  # fail fast (and loudly) if FalkorDB is not reachable

    print("# swap verify\n")
    print(f"Command: `python -m swap.verify --seeds {seeds}`  ·  FalkorDB at {cfg.where()}")
    print(
        f"Seeds: {seed_list[0]}..{seed_list[-1]} (K={seeds})  ·  N in {tuple(sizes)}  ·  "
        f"{seeds * len(sizes)} synthetic pools, every number read through the FalkorDB tools\n"
    )
    print("## Reproducible numbers (mean ± sd over the K seeds)\n")
    rows_by_n: dict[int, list[Row]] = {}
    for n in sizes:
        rows = []
        for seed in seed_list:
            pool = generate_pool(n, seed)
            rows.append(measure(pool, f"verify_{n}_{seed}", db))
        rows_by_n[n] = rows
    print("\n".join(_table(rows_by_n)))
    print("\nAll numbers above come from this run through the graph tools; the independent "
          "column below is `crosscheck.py`, a labelled plain-Python second opinion (no networkx "
          "in `src`).\n")

    print("## Per-seed checks: FalkorDB vs independent\n")
    check_lines, all_pass = _check_table(rows_by_n)
    print("\n".join(check_lines))
    print(f"\nOverall checks: {'PASS' if all_pass else 'FAIL'}\n")

    print("## Ties and negatives (reported honestly)\n")
    print("\n".join(_negatives(rows_by_n)))
    print()
    return 0 if all_pass else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m swap.verify")
    parser.add_argument(
        "--seeds",
        type=int,
        default=None,
        help="number of seeds (default 5; 10 for --fairness; README's numbers table uses 20)",
    )
    parser.add_argument(
        "--fairness",
        action="store_true",
        help="measure priority weighting on highly sensitised patients instead of the numbers table",
    )
    args = parser.parse_args(argv)
    if args.fairness:
        return run_fairness(args.seeds if args.seeds is not None else DEFAULT_FAIRNESS_SEEDS)
    return run(args.seeds if args.seeds is not None else DEFAULT_SEEDS)


if __name__ == "__main__":
    raise SystemExit(main())
