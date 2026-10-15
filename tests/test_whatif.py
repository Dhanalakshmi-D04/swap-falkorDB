"""M5 what-if tests: a modified copy in a scratch graph, diffed against the active pool.

Needs FalkorDB on the configured host/port: skipped cleanly without it. The invariants pinned here:
removing a pair never raises the matched count (and equals an independent recomputation from a
freshly loaded copy), adding an altruist never lowers it (and adds at most two patients), and the
scratch graph never outlives the call.
"""

from __future__ import annotations

import pytest
from falkordb import FalkorDB
from redis.exceptions import RedisError

from swap.config import settings
from swap.envelope import Outcome
from swap.generator import Pool, generate_pool
from swap.graph import connect, drop_graph, load_pool
from swap.tools import SwapTools
from swap.whatif import KINDS, scratch_name

GRAPH = "swap_whatif"
RECHECK = "swap_whatif_recheck"
VALID = {outcome.value for outcome in Outcome}


@pytest.fixture(scope="module")
def db() -> FalkorDB:
    cfg = settings()
    try:
        handle = connect(cfg)
        handle.list_graphs()
    except RedisError as exc:
        pytest.skip(f"FalkorDB unreachable at {cfg.where()} ({exc}); start it with `make up`")
    return handle


@pytest.fixture(scope="module")
def active(db: FalkorDB) -> tuple[Pool, SwapTools]:
    pool = generate_pool(80, 4242)
    load_pool(pool, GRAPH, db=db)
    return pool, SwapTools(GRAPH, db=db)


def _assert_envelope(env: dict) -> None:
    assert set(env) == {"outcome", "result", "path", "trace"}
    assert env["outcome"] in VALID
    assert set(env["trace"]) == {"tool", "args", "ms", "rows"}
    assert env["trace"]["tool"] == "what_if"
    for step in env["path"]:
        assert isinstance(step, list) and all(isinstance(v, int) for v in step)


def _without(pool: Pool, pair_id: int) -> Pool:
    """Independent copy of the pool without one pair, for a from-scratch recomputation."""
    keep = [i for i, pid in enumerate(pool.ids) if pid != pair_id]
    return Pool(
        n=len(keep),
        patient_abo=[pool.patient_abo[i] for i in keep],
        donor_abo=[pool.donor_abo[i] for i in keep],
        pra=[pool.pra[i] for i in keep],
        ids=[pool.ids[i] for i in keep],
        hard=[pool.hard[i] for i in keep],
        seed=pool.seed,
        edges=[(a, b) for a, b in pool.edges if a != pair_id and b != pair_id],
    )


def test_unknown_pair_id_is_unknown_entity(active: tuple[Pool, SwapTools]) -> None:
    _, tools = active
    for kind in ("remove_pair", "set_priority"):
        env = tools.what_if(kind, {"pair_id": 10**9})
        _assert_envelope(env)
        assert env["outcome"] == Outcome.UNKNOWN_ENTITY.value
        assert env["path"] == []


def test_bad_kind_and_bad_donor_are_rejected(active: tuple[Pool, SwapTools]) -> None:
    _, tools = active
    with pytest.raises(ValueError, match="unknown what-if kind"):
        tools.what_if("not_a_kind", {})
    with pytest.raises(ValueError, match="donor_abo"):
        tools.what_if("add_altruistic_donor", {"donor_abo": "Z"})


def test_add_altruist_never_decreases_and_bounds_the_gain(db: FalkorDB) -> None:
    pool = generate_pool(120, 7)
    load_pool(pool, GRAPH, db=db)
    tools = SwapTools(GRAPH, db=db)
    scratch = scratch_name(GRAPH)

    for donor_abo in ("O", "A", "B", "AB"):
        env = tools.what_if("add_altruistic_donor", {"donor_abo": donor_abo})
        _assert_envelope(env)
        assert env["outcome"] in {Outcome.ANSWERED_WITH_PATH.value, Outcome.NOT_IN_GRAPH.value}
        result = env["result"]
        assert result["after"]["matched"] >= result["before"]["matched"]
        assert 0 <= result["gain"] <= 2
        assert result["flow_bound"]["after"] >= result["flow_bound"]["before"]
        assert scratch not in db.list_graphs()

    load_pool(pool, RECHECK, db=db)
    recheck = SwapTools(RECHECK, db=db).best_set(3)["result"]["matched"]
    assert tools.best_set(3)["result"]["matched"] == recheck
    drop_graph(db, RECHECK)


def test_remove_pair_matches_a_from_scratch_recomputation(db: FalkorDB) -> None:
    pool = generate_pool(80, 11)
    load_pool(pool, GRAPH, db=db)
    tools = SwapTools(GRAPH, db=db)

    for pair_id in (0, 5, 40, pool.n - 1):
        env = tools.what_if("remove_pair", {"pair_id": pair_id})
        _assert_envelope(env)
        result = env["result"]
        assert result["after"]["matched"] <= result["before"]["matched"]
        assert result["before"]["pairs"] == pool.n
        assert result["after"]["pairs"] == pool.n - 1

        modified = _without(pool, pair_id)
        load_pool(modified, RECHECK, db=db)
        recheck = SwapTools(RECHECK, db=db).best_set(3)["result"]["matched"]
        assert recheck == result["after"]["matched"]
        drop_graph(db, RECHECK)

    assert scratch_name(GRAPH) not in db.list_graphs()


def test_set_priority_reports_the_change(active: tuple[Pool, SwapTools], db: FalkorDB) -> None:
    pool, tools = active
    pair_id = pool.hard_ids[0] if pool.hard_ids else pool.ids[0]
    env = tools.what_if("set_priority", {"pair_id": pair_id, "weight": 3.0})
    _assert_envelope(env)
    result = env["result"]
    assert result["weight"] == 3.0
    assert result["pair"]["id"] == pair_id
    assert isinstance(result["prioritised_pair_matched"], bool)
    assert 0 <= result["after"]["matched"] <= pool.n
    assert scratch_name(GRAPH) not in db.list_graphs()


def test_every_kind_returns_an_envelope(active: tuple[Pool, SwapTools]) -> None:
    pool, tools = active
    calls = {
        "add_altruistic_donor": {"donor_abo": "O"},
        "remove_pair": {"pair_id": pool.ids[1]},
        "set_priority": {"pair_id": pool.ids[2], "weight": 2.0},
    }
    assert set(calls) == set(KINDS)
    for kind, args in calls.items():
        env = tools.what_if(kind, args)
        _assert_envelope(env)
        assert env["result"]["kind"] == kind
        assert env["trace"]["rows"] >= 0
