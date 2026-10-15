"""Performance budgets on the documented bench seed (N=300), part of `make test`.

The three budgets are the ones in docs/OVERNIGHT.md: generate+load under 5 s, `find_cycles(3)`
under 1 s, `flow_bound` under 100 ms. Timings are printed so a regression is visible in the
pytest output. The `perf` marker runs under `make test`; it is only deselected with `-m "not perf"`.
"""

from __future__ import annotations

import time

import pytest
from falkordb import FalkorDB
from redis.exceptions import RedisError

from swap.config import settings
from swap.generator import generate_pool
from swap.graph import connect, drop_graph, load_pool
from swap.tools import SwapTools

pytestmark = pytest.mark.perf

BENCH_N, BENCH_SEED = 300, 20261015
GRAPH = "swap_perf_bench"
LOAD_BUDGET_S = 5.0
CYCLES_BUDGET_S = 1.0
FLOW_BUDGET_S = 0.1


@pytest.fixture(scope="module")
def db() -> FalkorDB:
    cfg = settings()
    try:
        handle = connect(cfg)
        handle.list_graphs()
    except RedisError as exc:
        pytest.skip(f"FalkorDB unreachable at {cfg.where()} ({exc}); start it with `make up`")
    return handle


def test_n300_budgets_print_their_timings(db: FalkorDB) -> None:
    started = time.perf_counter()
    pool = generate_pool(BENCH_N, BENCH_SEED)
    stats = load_pool(pool, GRAPH, db=db)
    load_s = time.perf_counter() - started
    try:
        tools = SwapTools(GRAPH, db=db)

        started = time.perf_counter()
        cycles = tools.find_cycles(3)
        cycles_s = time.perf_counter() - started

        started = time.perf_counter()
        flow = tools.flow_bound()
        flow_s = time.perf_counter() - started

        print(
            f"\nN={BENCH_N} seed {BENCH_SEED}: generate+load {load_s:.2f}s "
            f"({stats.pairs} pairs, {stats.edges} edges) | find_cycles(3) {cycles_s:.3f}s "
            f"({cycles['result']['total']} cycles) | flow_bound {flow_s * 1000:.0f}ms "
            f"(bound {flow['result']['flow_bound']})"
        )

        assert load_s < LOAD_BUDGET_S, f"generate+load took {load_s:.2f}s (budget {LOAD_BUDGET_S}s)"
        assert cycles_s < CYCLES_BUDGET_S, f"find_cycles(3) took {cycles_s:.3f}s (budget 1s)"
        assert flow_s < FLOW_BUDGET_S, f"flow_bound took {flow_s * 1000:.0f}ms (budget 100ms)"
    finally:
        drop_graph(db, GRAPH)
