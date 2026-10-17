"""Swap objects: every 2- and 3-way swap lives in the graph, and plans are guarded and taken there.

Needs FalkorDB (skipped cleanly without it). Pinned here: the stored swaps are exactly the rings the
pattern match finds, with the right arcs, back-arcs and hard counts; the planner reads them in the
pattern match's order, so the packing (and every published number) is unchanged; a draft passes
FalkorDB's guard and links its swaps; approval commits pairs and takes every swap touching them;
and a draft that overlaps an approved plan can no longer be approved.
"""

from __future__ import annotations

import pytest
from falkordb import FalkorDB
from redis.exceptions import RedisError

from swap.config import settings
from swap.generator import Pool, generate_pool
from swap.graph import connect, ensure_swap_objects, load_pool
from swap.solve import pack_cycles
from swap.tools import PlanStateError, SwapTools

GRAPH = "swap_cycle_graph"


@pytest.fixture(scope="module")
def db() -> FalkorDB:
    cfg = settings()
    try:
        handle = connect(cfg)
        handle.list_graphs()
    except RedisError as exc:
        pytest.skip(f"FalkorDB unreachable at {cfg.where()} ({exc}); start it with `make up`")
    return handle


def _load(db: FalkorDB, n: int, seed: int) -> tuple[Pool, SwapTools]:
    pool = generate_pool(n, seed)
    load_pool(pool, GRAPH, db=db)
    return pool, SwapTools(GRAPH, db=db)


def _stored(tools: SwapTools) -> list[list]:
    return tools.graph.query(
        "MATCH (c:Cycle) RETURN c.key, c.len, c.members, c.hard, c.back_arcs, c.live, c.taken"
    ).result_set


@pytest.mark.parametrize(("n", "seed"), [(100, 1), (60, 3)])
def test_stored_swaps_are_exactly_the_rings_the_graph_finds(db: FalkorDB, n: int, seed: int) -> None:
    pool, tools = _load(db, n, seed)
    stored = _stored(tools)
    found = tools.find_cycles(3)["result"]
    assert sum(1 for row in stored if row[1] == 2) == found["counts"][2]
    assert sum(1 for row in stored if row[1] == 3) == found["counts"][3]
    assert sorted(row[2] for row in stored) == sorted(found["cycles"])

    arcs = set(pool.edges)
    hard = {pid for pid, is_hard in zip(pool.ids, pool.hard, strict=True) if is_hard}
    assert len({row[0] for row in stored}) == len(stored)
    for key, length, members, n_hard, back, live, taken in stored:
        assert key == ">".join(str(m) for m in members)
        assert members[0] == min(members) and len(members) == length
        ring = list(zip(members, members[1:] + members[:1], strict=True))
        assert all(arc in arcs for arc in ring)
        expected_back = 0 if length == 2 else sum((b, a) in arcs for a, b in ring)
        assert back == expected_back
        assert n_hard == sum(m in hard for m in members)
        assert live and not taken


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("priority", [None, 3.0])
def test_the_planner_reads_swaps_in_the_pattern_match_order(
    db: FalkorDB, seed: int, priority: float | None
) -> None:
    """Same candidates, same order, same weights: the packing is the one published before."""
    _, tools = _load(db, 100, seed)
    for max_len in (2, 3):
        found = tools.find_cycles(max_len)["result"]["cycles"]
        rows = tools.graph.query(
            "MATCH (c:Cycle) WHERE c.live AND c.len <= $max_len "
            "RETURN c.members ORDER BY c.len, c.members",
            {"max_len": max_len},
        ).result_set
        assert [row[0] for row in rows] == found
        reference = pack_cycles(found, hard_ids=tools.hard_pair_ids(), priority_weight=priority or 1.0)
        best = tools.best_set(max_len, priority)["result"]
        assert best["chosen"] == reference.chosen
        assert best["matched"] == reference.covered
        assert best["cycle_keys"] == [">".join(map(str, c)) for c in best["cycles"]]
        assert best["candidates_from"] == "swap objects"


def test_a_draft_is_guarded_by_the_graph_and_linked_to_its_swaps(db: FalkorDB) -> None:
    _, tools = _load(db, 120, 7)
    packet = tools.draft_plan(3)["result"]
    guard = packet["guard"]
    assert guard["ok"] and guard["missing_arcs"] == guard["shared_pairs"] == 0
    assert guard["cycles"] == len(packet["swaps"])
    assert guard["arcs_checked"] == sum(swap["size"] for swap in packet["swaps"])
    linked = tools.graph.query(
        "MATCH (:Plan {id: $id})-[:SELECTS]->(c:Cycle) RETURN c.key", {"id": packet["plan"]["id"]}
    ).result_set
    assert sorted(row[0] for row in linked) == sorted(swap["key"] for swap in packet["swaps"])
    assert all(swap["live"] and swap["broken_by"] == [] for swap in packet["swaps"])
    assert packet["plan"]["kind"] == "plan"
    assert {step["name"] for step in packet["graph_work"]} >= {"guard_pairs", "guard_arcs", "add_selects"}


def test_approval_commits_pairs_and_takes_every_swap_touching_them(db: FalkorDB) -> None:
    _, tools = _load(db, 120, 7)
    plan_id = tools.draft_plan(2)["result"]["plan"]["id"]
    decided = tools.decide_plan(plan_id, "approve", "Dr Test")["result"]
    committed = set(tools.committed_pair_ids())
    assert committed == {p for swap in decided["swaps"] for p in swap["pairs"]}
    assert decided["effects"]["committed_pairs"] == len(committed)

    stored = _stored(tools)
    should_be_taken = {row[0] for row in stored if committed.intersection(row[2])}
    assert {row[0] for row in stored if row[6]} == should_be_taken
    assert decided["effects"]["swaps_now_unavailable"] == len(should_be_taken)
    rows = tools.graph.query(
        "MATCH (p:Pair) WHERE p.committed IS NOT NULL RETURN DISTINCT p.committed"
    ).result_set
    assert rows == [[plan_id]]

    second = tools.draft_plan(3)["result"]
    planned = {p for swap in second["swaps"] for p in swap["pairs"]}
    assert planned and planned.isdisjoint(committed)


def test_a_draft_overlapping_an_approved_plan_is_stale(db: FalkorDB) -> None:
    _, tools = _load(db, 120, 7)
    first = tools.draft_plan(3)["result"]["plan"]["id"]
    second = tools.draft_plan(3)["result"]["plan"]["id"]  # same pool state: the same swaps again
    tools.decide_plan(first, "approve", "Dr Test")
    with pytest.raises(PlanStateError, match="stale"):
        tools.decide_plan(second, "approve", "Dr Test")
    assert tools.get_plan(second)["result"]["plan"]["status"] == "draft"
    tools.decide_plan(second, "reject", "Dr Test")  # rejecting a stale draft is still allowed


def test_plans_need_two_or_three_way_swaps(db: FalkorDB) -> None:
    _, tools = _load(db, 30, 1)
    with pytest.raises(ValueError, match="2 or 3"):
        tools.draft_plan(4)


def test_an_old_graph_without_swap_objects_gets_them(db: FalkorDB) -> None:
    _, tools = _load(db, 60, 3)
    expected = len(_stored(tools))
    tools.graph.query("MATCH (c:Cycle) DETACH DELETE c")
    fresh = SwapTools(GRAPH, db=db)
    assert fresh.best_set(3)["result"]["candidate_cycles"] == expected
    assert ensure_swap_objects(fresh.graph) == {}  # already there: left alone


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_tiny_pools_store_no_swaps_and_plan_nothing(db: FalkorDB, n: int) -> None:
    _, tools = _load(db, n, 5)
    assert tools.best_set(3)["outcome"] in ("answered_with_path", "not_in_graph")
    assert tools.draft_plan(3)["outcome"] in ("answered_with_path", "not_in_graph")
