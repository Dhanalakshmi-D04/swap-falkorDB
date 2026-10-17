"""Why a patient is not in a swap: verdicts and evidence walked in the graph, on hand-built pools.

Needs FalkorDB (skipped cleanly without it). Each verdict is pinned on a pool where the answer is
known by construction: a 4-pair ring (no 2- or 3-way swap, ring too long), an AB donor with no
outgoing link, a patient whose swaps a failed crossmatch killed, and a patient blocked by the
chosen swaps. Also pinned: the best-set memo never serves a stale answer.
"""

from __future__ import annotations

import pytest
from falkordb import FalkorDB
from redis.exceptions import RedisError

from swap.config import settings
from swap.generator import Pool, generate_pool
from swap.graph import connect, load_pool
from swap.tools import SwapTools

GRAPH = "swap_explain"


@pytest.fixture(scope="module")
def db() -> FalkorDB:
    cfg = settings()
    try:
        handle = connect(cfg)
        handle.list_graphs()
    except RedisError as exc:
        pytest.skip(f"FalkorDB unreachable at {cfg.where()} ({exc}); start it with `make up`")
    return handle


def _pool(edges: list[tuple[int, int]], n: int, donor_abo: list[str] | None = None) -> Pool:
    return Pool(
        n=n,
        patient_abo=["O"] * n,
        donor_abo=donor_abo or ["O"] * n,
        pra=[0.05] * n,
        ids=list(range(n)),
        hard=[False] * n,
        seed=0,
        edges=edges,
    )


def _tools(db: FalkorDB, pool: Pool) -> SwapTools:
    load_pool(pool, GRAPH, db=db)
    return SwapTools(GRAPH, db=db)


def test_a_four_pair_ring_is_too_long_and_its_routes_come_from_sppaths(db: FalkorDB) -> None:
    # 0 -> 1 -> 2 -> 3 -> 0 plus a second route 1 -> 4 -> 3: no 2- or 3-way swap exists
    tools = _tools(db, _pool([(0, 1), (1, 2), (2, 3), (3, 0), (1, 4), (4, 3)], 5))
    found = tools.explain_unmatched(0)["result"]
    assert found["verdict"] == "ring_too_long"
    assert found["shortest_ring"] == {"ring": [0, 1, 2, 3, 0], "pairs": 4, "max_hops": 6}
    assert sorted(found["alternative_routes"]) == [[0, 1, 2, 3, 0], [0, 1, 4, 3, 0]]
    assert "needs 4 pairs" in found["summary"]
    assert found["options"] == {"stored": 0, "live": 0, "available": 0}


def test_a_donor_who_can_give_to_nobody(db: FalkorDB) -> None:
    tools = _tools(db, _pool([(0, 1), (1, 0), (0, 2)], 3, donor_abo=["O", "O", "AB"]))
    found = tools.explain_unmatched(2)["result"]
    assert found["verdict"] == "no_outgoing"
    assert found["donor_reach"] == {"can_give_to": 0, "can_receive_from": 1}
    assert "altruistic donor" in found["summary"]


def test_a_pair_in_the_chosen_swap(db: FalkorDB) -> None:
    tools = _tools(db, _pool([(0, 1), (1, 0)], 2))
    found = tools.explain_unmatched(1)["result"]
    assert found["verdict"] == "in_swap" and found["in_swap"] == "0>1"
    assert found["against"]["kind"] == "best_set"


def test_blocked_names_the_chosen_swap_that_holds_the_needed_pair(db: FalkorDB) -> None:
    # 0<->1 and 1<->2: the best set takes one 2-way swap through 1; the other pair is blocked by it
    tools = _tools(db, _pool([(0, 1), (1, 0), (1, 2), (2, 1)], 3))
    chosen = tools.best_set(2)["result"]["cycle_keys"]
    left_out = 2 if chosen == ["0>1"] else 0
    found = tools.explain_unmatched(left_out)["result"]
    assert found["verdict"] == "blocked"
    assert found["blocked_by"] == [{"key": chosen[0], "members": [int(v) for v in chosen[0].split(">")], "blocks": 1}]
    assert found["options"]["live"] == 1


def test_against_a_plan_and_options_killed_by_a_failed_crossmatch(db: FalkorDB) -> None:
    tools = _tools(db, _pool([(0, 1), (1, 0), (1, 2), (2, 1)], 3))
    plan = tools.draft_plan(2)["result"]["plan"]["id"]
    found = tools.explain_unmatched(1)["result"]
    assert found["against"] == {"kind": "plan", "plan": plan, "swaps": 1}
    assert found["verdict"] == "in_swap"
    tools.record_crossmatch(0, 1, "positive", "Lab A")
    tools.record_crossmatch(2, 1, "positive", "Lab A")
    dead = tools.explain_unmatched(0)["result"]
    assert dead["verdict"] == "options_dead"  # its one swap, 0>1, was killed by the first result
    assert dead["options"] == {"stored": 1, "live": 0, "available": 0}
    assert "killed by failed crossmatches" in dead["summary"]


def test_the_best_set_memo_is_never_stale(db: FalkorDB) -> None:
    tools = _tools(db, generate_pool(120, 7))
    first = tools.best_set(3)["result"]
    again = tools.best_set(3)["result"]
    assert again["memo"] and again["chosen"] == first["chosen"]
    assert {w["name"] for w in again["graph_work"]} == {"swap_fingerprint", "pool_fingerprint"}
    swap = next(c for c in first["cycles"] if len(c) == 3)
    tools.record_crossmatch(swap[0], swap[1], "positive", "Lab A")
    after = tools.best_set(3)["result"]
    assert "memo" not in after
    assert swap not in after["cycles"]
    reloaded = _tools(db, generate_pool(120, 7))
    assert "memo" not in reloaded.best_set(3)["result"]  # a reload forgets the graph's entries


def test_explain_puts_its_evidence_on_the_path(db: FalkorDB) -> None:
    tools = _tools(db, _pool([(0, 1), (1, 2), (2, 3), (3, 0)], 4))
    env = tools.explain_unmatched(0)
    assert [0, 1, 2, 3] in env["path"]
