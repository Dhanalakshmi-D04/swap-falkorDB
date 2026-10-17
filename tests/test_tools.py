"""M3/M4/M6 tool tests. The graph tools are cross-checked against tests/oracle.py (Pool only).

Empty-traversal semantics (decided and pinned here): a tool whose traversal comes back empty
answers `not_in_graph` ("the graph does not know"), never `answered_with_path` with a zero count,
and never a claim of safety. That covers a zero-edge pool and a pair with no incoming candidate.

n=300 is the one expensive case: `best_set(3)` runs an integer program over ~50k candidate cycles
(seconds), so only the repo's documented bench seed is exercised there; the multiple-seed coverage
sits at n=30 and n=100.
"""

from __future__ import annotations

import time

import numpy as np
import oracle
import pytest
from falkordb import FalkorDB
from redis.exceptions import RedisError

from swap.config import settings
from swap.envelope import Outcome, UnknownEntity, envelope, resolve_pair, timed
from swap.generator import Pool, can_give_abo, generate_pool
from swap.graph import connect, load_pool
from swap.tools import SwapTools

# (n, seed) cases for the graph-vs-oracle equivalence and the bound chain.
CASES = [
    (30, 1),
    (30, 2),
    (30, 3),
    (100, 1),
    (100, 2),
    (100, 3),
    (300, 20261015),
    (300, 20261016),
]
BENCH = (300, 20261015)
SANITY_SEEDS = range(1000, 1020)
# Reference means over 20 seeds at n=100 (docs/EVIDENCE.md section 3).
REFERENCE_MEANS = (45.9, 55.0, 56.5, 60.3)


@pytest.fixture(scope="module")
def db() -> FalkorDB:
    cfg = settings()
    try:
        handle = connect(cfg)
        handle.list_graphs()
    except RedisError as exc:
        pytest.skip(f"FalkorDB unreachable at {cfg.where()} ({exc}); start it with `make up`")
    return handle


def _load(db: FalkorDB, n: int, seed: int, name: str) -> tuple[Pool, SwapTools]:
    pool = generate_pool(n, seed)
    load_pool(pool, name, db=db)
    return pool, SwapTools(name, db=db)


def _zero_edge_pool(n: int) -> Pool:
    """B donors cannot give to A patients, so this pool has no CAN_GIVE edge at all."""
    return Pool(
        n=n,
        patient_abo=["A"] * n,
        donor_abo=["B"] * n,
        pra=[0.9] * n,
        ids=list(range(n)),
        hard=[True] * n,
        seed=0,
        edges=[],
    )


def _rotate_to_min(cycle: tuple[int, ...]) -> tuple[int, ...]:
    at = cycle.index(min(cycle))
    return cycle[at:] + cycle[:at]


def test_envelope_helpers_need_no_graph() -> None:
    with timed() as timer:
        timer.rows = 7
    assert timer.ms >= 0.0 and timer.rows == 7
    env = envelope(
        Outcome.NOT_IN_GRAPH, {"why": "empty"}, [[1, 2]], tool="t", args={"a": 1}, timer=timer
    )
    assert env["outcome"] == "not_in_graph"
    assert env["path"] == [[1, 2]]
    assert env["trace"] == {"tool": "t", "args": {"a": 1}, "ms": env["trace"]["ms"], "rows": 7}


def test_envelope_shape_on_every_tool(db: FalkorDB) -> None:
    pool, tools = _load(db, 40, 11, "swap_tools_shape")
    calls = {
        "find_cycles": lambda: tools.find_cycles(3),
        "flow_bound": tools.flow_bound,
        "explain_unmatched": lambda: tools.explain_unmatched(pool.ids[0]),
        "explain_unmatched_unknown": lambda: tools.explain_unmatched(10**9),
        "best_set": lambda: tools.best_set(3),
        "true_cycle_optimum": tools.true_cycle_optimum,
    }
    valid = {outcome.value for outcome in Outcome}
    for name, call in calls.items():
        env = call()
        assert set(env) == {"outcome", "result", "path", "trace"}, name
        assert env["outcome"] in valid, name
        assert set(env["trace"]) == {"tool", "args", "ms", "rows"}, name
        assert isinstance(env["trace"]["ms"], (int, float)) and env["trace"]["ms"] >= 0, name
        assert isinstance(env["trace"]["rows"], int) and env["trace"]["rows"] >= 0, name
        assert isinstance(env["trace"]["args"], dict), name
        assert isinstance(env["path"], list), name
        for step in env["path"]:
            assert isinstance(step, list) and all(isinstance(v, int) for v in step), name


def test_unknown_id_is_unknown_entity(db: FalkorDB) -> None:
    pool, tools = _load(db, 30, 5, "swap_tools_unknown")
    env = tools.explain_unmatched(10**9)
    assert env["outcome"] == Outcome.UNKNOWN_ENTITY.value
    assert env["path"] == []
    with pytest.raises(UnknownEntity):
        resolve_pair(tools.graph, 10**9)
    assert resolve_pair(tools.graph, pool.ids[0]).id == pool.ids[0]


def test_zero_edge_pool_says_not_in_graph(db: FalkorDB) -> None:
    load_pool(_zero_edge_pool(30), "swap_tools_noedges", db=db)
    tools = SwapTools("swap_tools_noedges", db=db)
    for env in (
        tools.find_cycles(3),
        tools.flow_bound(),
        tools.best_set(3),
        tools.true_cycle_optimum(),
    ):
        assert env["outcome"] == Outcome.NOT_IN_GRAPH.value
        assert env["path"] == []
    assert tools.find_cycles(3)["result"]["total"] == 0
    assert tools.best_set(3)["result"]["matched"] == 0


def test_pair_with_no_incoming_candidate_says_not_in_graph(db: FalkorDB) -> None:
    pool, tools = _load(db, 60, 4242, "swap_tools_nocandidate")
    unreached = [pid for pid in pool.ids if not any(target == pid for _, target in pool.edges)]
    assert unreached, "expected a pair no donor can give to in this pool"
    env = tools.explain_unmatched(unreached[0])
    assert env["outcome"] == Outcome.NOT_IN_GRAPH.value
    assert "does not know" in env["result"]["note"]


@pytest.mark.parametrize("n", [2, 3])
def test_tiny_pools_do_not_crash(db: FalkorDB, n: int) -> None:
    pool, tools = _load(db, n, n + 1, f"swap_tools_tiny{n}")
    assert tools.find_cycles(2)["outcome"] in {o.value for o in Outcome}
    assert tools.find_cycles(3)["outcome"] in {o.value for o in Outcome}
    assert tools.flow_bound()["outcome"] in {o.value for o in Outcome}
    assert tools.best_set(3)["outcome"] in {o.value for o in Outcome}
    assert tools.true_cycle_optimum()["outcome"] in {o.value for o in Outcome}
    assert tools.explain_unmatched(pool.ids[0])["outcome"] in {o.value for o in Outcome}


@pytest.mark.parametrize("n,seed", CASES)
def test_cycles_and_matching_match_oracle(db: FalkorDB, n: int, seed: int) -> None:
    pool, tools = _load(db, n, seed, f"swap_tools_eq_{n}_{seed}")
    edges = oracle.adjacency(pool)

    found2 = tools.find_cycles(2)["result"]
    found3 = tools.find_cycles(3)["result"]
    assert found2["counts"][2] == len(oracle.cycles_of_length(edges, 2))
    assert found3["counts"][3] == len(oracle.cycles_of_length(edges, 3))
    assert found3["counts"][2] == found2["counts"][2]
    assert {tuple(c) for c in found3["cycles"]} == set(oracle.cycles_up_to(edges, 3))

    assert tools.flow_bound()["result"]["flow_bound"] == oracle.bipartite_matching(edges)


def test_four_cycles_match_oracle(db: FalkorDB) -> None:
    pool, tools = _load(db, 60, 4, "swap_tools_four")
    edges = oracle.adjacency(pool)
    found = tools.find_cycles(4)["result"]
    for length in (2, 3, 4):
        assert found["counts"][length] == len(oracle.cycles_of_length(edges, length))
    assert {tuple(c) for c in found["cycles"]} == set(oracle.cycles_up_to(edges, 4))


def test_explain_reasoning_matches_pool(db: FalkorDB) -> None:
    pool, tools = _load(db, 80, 9, "swap_tools_explain")
    edges = oracle.adjacency(pool)
    pid = pool.ids[0]
    env = tools.explain_unmatched(pid)
    result = env["result"]

    incoming = sorted(source for source, target in pool.edges if target == pid)
    outgoing = sorted(target for source, target in pool.edges if source == pid)
    assert result["incoming_candidates"] == incoming
    assert result["outgoing_targets"] == outgoing

    abo_incompatible = sum(
        1
        for other in pool.ids
        if other != pid and not can_give_abo(pool.donor_abo[other], pool.patient_abo[pid])
    )
    abo_compatible = (pool.n - 1) - abo_incompatible
    assert result["reasoning"]["abo_incompatible_pairs"] == abo_incompatible
    assert result["reasoning"]["abo_compatible_but_crossmatch_failed"] == (
        abo_compatible - len(incoming)
    )

    through = {_rotate_to_min(tuple(step)) for step in env["path"]}
    assert through == {_rotate_to_min(c) for c in oracle.cycles_up_to(edges, 3) if pid in c}


@pytest.mark.parametrize("n,seed", CASES)
def test_best_set_chain_and_oracle_packing(db: FalkorDB, n: int, seed: int) -> None:
    pool, tools = _load(db, n, seed, f"swap_tools_pack_{n}_{seed}")
    edges = oracle.adjacency(pool)

    bound = tools.flow_bound()["result"]["flow_bound"]
    two = tools.best_set(2)["result"]["matched"]
    three = tools.best_set(3)["result"]["matched"]
    cover = tools.true_cycle_optimum()["result"]["unbounded_cycle_cover"]

    assert two <= three <= cover <= bound
    assert two == oracle.pack(n, oracle.cycles_of_length(edges, 2))
    assert three == oracle.pack(n, oracle.cycles_up_to(edges, 3))
    assert cover == oracle.unbounded_cycle_cover(edges)


def test_priority_weighting_prefers_hard_patients(db: FalkorDB) -> None:
    pool, tools = _load(db, 120, 7, "swap_tools_priority")
    hard = set(pool.hard_ids)
    plain = tools.best_set(3)["result"]["chosen"]
    weighted = tools.best_set(3, priority=3.0)["result"]["chosen"]

    def hard_share(cycles: list[list[int]]) -> float:
        covered = [patient for cycle in cycles for patient in cycle]
        return sum(1 for patient in covered if patient in hard) / len(covered) if covered else 0.0

    assert hard_share(weighted) >= hard_share(plain)
    # weighting may trade total coverage for hard-patient coverage; both stay feasible packings.
    assert tools.best_set(3, priority=3.0)["result"]["priority_weight"] == 3.0


def test_n300_performance_within_budget(db: FalkorDB) -> None:
    _, tools = _load(db, *BENCH, "swap_tools_perf300")
    started = time.perf_counter()
    cycles = tools.find_cycles(3)
    find_seconds = time.perf_counter() - started
    started = time.perf_counter()
    flow = tools.flow_bound()
    flow_seconds = time.perf_counter() - started
    print(
        f"\nN=300 seed {BENCH[1]}: find_cycles(3) {find_seconds:.3f}s "
        f"({cycles['result']['total']} cycles), flow_bound {flow_seconds * 1000:.0f}ms "
        f"(bound {flow['result']['flow_bound']})"
    )
    # Budgets as in test_perf.py: the stored swap objects make the graph's matrices larger.
    assert find_seconds < 2.0, f"find_cycles(3) took {find_seconds:.3f}s (budget 2s)"
    assert flow_seconds < 0.3, f"flow_bound took {flow_seconds * 1000:.0f}ms (budget 300ms)"


def test_reference_means_are_reproduced(db: FalkorDB) -> None:
    rows = []
    started = time.perf_counter()
    for seed in SANITY_SEEDS:
        _, tools = _load(db, 100, seed, "swap_tools_sanity")
        rows.append(
            (
                tools.best_set(2)["result"]["matched"],
                tools.best_set(3)["result"]["matched"],
                tools.true_cycle_optimum()["result"]["unbounded_cycle_cover"],
                tools.flow_bound()["result"]["flow_bound"],
            )
        )
    means = np.array(rows, dtype=float).mean(axis=0)
    print(
        f"\nn=100 seeds {SANITY_SEEDS.start}..{SANITY_SEEDS.stop - 1} ({time.perf_counter() - started:.1f}s): "
        f"2-way {means[0]:.1f} | up-to-3 {means[1]:.1f} | unbounded {means[2]:.1f} | "
        f"matching {means[3]:.1f}  (reference {REFERENCE_MEANS})"
    )
    for observed, reference in zip(means, REFERENCE_MEANS, strict=True):
        assert observed == pytest.approx(reference, abs=4.0)
