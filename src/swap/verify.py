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

import numpy as np

from swap import crosscheck
from swap.config import settings
from swap.generator import Pool, generate_pool
from swap.graph import connect, drop_graph, load_pool
from swap.tools import SwapTools

SIZES: tuple[int, ...] = (100, 200, 300)
SEED_START = 1000
DEFAULT_SEEDS = 5


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
        "--seeds", type=int, default=DEFAULT_SEEDS, help="number of seeds (default 5; README uses 20)"
    )
    args = parser.parse_args(argv)
    return run(args.seeds)


if __name__ == "__main__":
    raise SystemExit(main())
