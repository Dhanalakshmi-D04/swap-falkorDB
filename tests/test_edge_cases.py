"""Hardening: degenerate pools and bad input must never 500 or crash any endpoint or tool.

Every endpoint in the catalogue (`/pool`, `find_cycles`, `flow_bound`, `best_set`, `explain`,
`what_if`, `/proof`, `/graph`, `/ask`) is exercised against pools of n = 0, 1, 2, 3 and a hand-built
n = 5 pool with zero `CAN_GIVE` edges. Empty traversals answer `not_in_graph`; ids the graph does
not hold answer `unknown_entity`; oversized / invalid `POST /pool` input is a 4xx, not a hang.

This file pins three real bugs found while writing it (all fixed in `src/swap/tools.py`):
  * `flow_bound` raised `IndexError` on an unloaded graph (no `S`/`T` row) -> 500;
  * `flow_bound` raised `ResponseError` on a 0-pair pool (S and T exist but no `:F` arc);
  * the explain ABO split counted a phantom incompatible pair when the pool held no other pair.
"""

from __future__ import annotations

import pytest
from falkordb import FalkorDB
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from swap.api import ACTIVE_GRAPH, app
from swap.config import settings
from swap.envelope import Outcome
from swap.generator import Pool, generate_pool
from swap.graph import connect, drop_graph, load_pool
from swap.tools import SwapTools

VALID = {outcome.value for outcome in Outcome}
ENVELOPE_KEYS = {"outcome", "result", "path", "trace"}
EMPTY = Outcome.NOT_IN_GRAPH.value
UNKNOWN = Outcome.UNKNOWN_ENTITY.value
UNKNOWN_IDS = (99999, -1)


def _zero_edge_pool(n: int) -> Pool:
    """n pairs whose donors can never give to any patient here: no CAN_GIVE edge at all."""
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
def client(db: FalkorDB) -> TestClient:
    return TestClient(app)


def _assert_envelope(response, path: str) -> dict:
    assert response.status_code == 200, f"{path}: HTTP {response.status_code} {response.text}"
    env = response.json()
    assert set(env) == ENVELOPE_KEYS, path
    assert env["outcome"] in VALID, path
    assert set(env["trace"]) == {"tool", "args", "ms", "rows"}, path
    return env


def _hit_every_endpoint(client: TestClient, label: str) -> dict[str, dict]:
    """Call every endpoint once; fail on any 5xx. Returns the envelopes by endpoint."""
    get_paths = {
        "/tools/find_cycles": {"max_len": 3},
        "/tools/flow_bound": {},
        "/tools/best_set": {"max_len": 3},
        "/tools/explain/0": {},
    }
    envelopes = {
        path: _assert_envelope(client.get(path, params=params), f"{label} {path}")
        for path, params in get_paths.items()
    }
    for body in (
        {"kind": "add_altruistic_donor", "donor_abo": "O"},
        {"kind": "remove_pair", "pair_id": 0},
        {"kind": "set_priority", "pair_id": 0, "weight": 2.0},
    ):
        envelopes[f"/tools/what_if {body['kind']}"] = _assert_envelope(
            client.post("/tools/what_if", json=body), f"{label} what_if {body['kind']}"
        )

    proof = client.get("/proof")
    assert proof.status_code == 200, f"{label} /proof: HTTP {proof.status_code}"
    assert proof.json()["graph"] == ACTIVE_GRAPH

    graph = client.get("/graph", params={"max_len": 3})
    assert graph.status_code == 200, f"{label} /graph: HTTP {graph.status_code}"
    assert set(graph.json()) >= {"nodes", "edges", "chosen", "counts"}

    ask = client.post("/ask", json={"question": "how many swap cycles are there?"})
    assert ask.status_code == 200, f"{label} /ask: HTTP {ask.status_code}"
    assert set(ask.json()) == {"answer", "tool_calls", "rejected_ids", "mode"}
    return envelopes


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_api_generated_degenerate_pools_never_500(client: TestClient, n: int) -> None:
    response = client.post("/pool", json={"n": n, "seed": n + 1})
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["n"] == n
    _hit_every_endpoint(client, f"api-n{n}")


def test_api_zero_edge_pool_never_500(client: TestClient, db: FalkorDB) -> None:
    stats = load_pool(_zero_edge_pool(5), ACTIVE_GRAPH, db=db)
    assert stats.pairs == 5
    envelopes = _hit_every_endpoint(client, "api-zero-edge-5")
    # an empty traversal is the graph not knowing, never a claim of safety
    assert envelopes["/tools/find_cycles"]["outcome"] == EMPTY
    assert envelopes["/tools/best_set"]["outcome"] == EMPTY


def test_api_empty_traversals_are_not_in_graph(client: TestClient, db: FalkorDB) -> None:
    load_pool(_zero_edge_pool(5), ACTIVE_GRAPH, db=db)
    for path, params in (
        ("/tools/find_cycles", {"max_len": 3}),
        ("/tools/flow_bound", {}),
        ("/tools/best_set", {"max_len": 3}),
        ("/tools/explain/0", {}),
    ):
        env = _assert_envelope(client.get(path, params=params), path)
        assert env["outcome"] == EMPTY, path
        assert env["path"] == [], path
        assert "does not know" in env["result"]["note"], path


def test_api_zero_pair_pool_flow_bound_is_not_in_graph(client: TestClient) -> None:
    # regression: a 0-pair pool used to make algo.maxFlow raise (no `:F` relationship)
    assert client.post("/pool", json={"n": 0, "seed": 1}).status_code == 200
    env = _assert_envelope(client.get("/tools/flow_bound"), "flow_bound n=0")
    assert env["outcome"] == EMPTY
    assert env["result"]["flow_bound"] == 0
    proof = client.get("/proof")
    assert proof.status_code == 200
    assert proof.json()["loaded"] is False


def test_api_unknown_ids_are_unknown_entity(client: TestClient) -> None:
    assert client.post("/pool", json={"n": 20, "seed": 7}).status_code == 200
    for pair_id in UNKNOWN_IDS:
        explained = _assert_envelope(client.get(f"/tools/explain/{pair_id}"), "explain")
        assert explained["outcome"] == UNKNOWN, explained
        assert explained["path"] == []
        for kind in ("remove_pair", "set_priority"):
            body = {"kind": kind, "pair_id": pair_id}
            env = _assert_envelope(client.post("/tools/what_if", json=body), f"what_if {kind}")
            assert env["outcome"] == UNKNOWN, body
            assert env["path"] == []


def test_api_pool_size_is_bounded_and_never_hangs(client: TestClient) -> None:
    assert client.post("/pool", json={"n": -1, "seed": 1}).status_code == 422
    assert client.post("/pool", json={"n": "lots", "seed": 1}).status_code == 422
    absurd = client.post("/pool", json={"n": 100000, "seed": 1})
    assert 400 <= absurd.status_code < 500, absurd.text  # rejected by validation, not generated


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_tools_degenerate_pools_never_crash(db: FalkorDB, n: int) -> None:
    pool = generate_pool(n, n + 1)
    name = f"swap_edge_tools_{n}"
    load_pool(pool, name, db=db)
    try:
        tools = SwapTools(name, db=db)
        calls = {
            "find_cycles": tools.find_cycles(3),
            "find_cycles_2": tools.find_cycles(2),
            "flow_bound": tools.flow_bound(),
            "best_set": tools.best_set(3),
            "true_cycle_optimum": tools.true_cycle_optimum(),
        }
        for tool_name, env in calls.items():
            assert set(env) == ENVELOPE_KEYS, tool_name
            assert env["outcome"] in VALID, tool_name
    finally:
        drop_graph(db, name)


def test_tools_zero_edge_pool_is_not_in_graph(db: FalkorDB) -> None:
    name = "swap_edge_tools_zero"
    load_pool(_zero_edge_pool(5), name, db=db)
    try:
        tools = SwapTools(name, db=db)
        for tool_name, env in (
            ("find_cycles", tools.find_cycles(3)),
            ("flow_bound", tools.flow_bound()),
            ("best_set", tools.best_set(3)),
            ("true_cycle_optimum", tools.true_cycle_optimum()),
            ("explain", tools.explain_unmatched(0)),
        ):
            assert env["outcome"] == EMPTY, tool_name
            assert env["path"] == [], tool_name
    finally:
        drop_graph(db, name)


def test_tools_flow_bound_on_unloaded_graph_is_not_in_graph(db: FalkorDB) -> None:
    # regression: no S/T node used to make result_set[0] raise IndexError
    name = "swap_edge_tools_unloaded"
    drop_graph(db, name)
    tools = SwapTools(name, db=db)
    assert tools.n_flow_edges() == 0
    env = tools.flow_bound()
    assert env["outcome"] == EMPTY
    assert env["result"]["flow_bound"] == 0


def test_tools_unknown_ids_are_unknown_entity(db: FalkorDB) -> None:
    name = "swap_edge_tools_unknown"
    load_pool(_zero_edge_pool(5), name, db=db)
    try:
        tools = SwapTools(name, db=db)
        for pair_id in UNKNOWN_IDS:
            env = tools.explain_unmatched(pair_id)
            assert env["outcome"] == UNKNOWN, pair_id
            for kind in ("remove_pair", "set_priority"):
                assert tools.what_if(kind, {"pair_id": pair_id})["outcome"] == UNKNOWN, pair_id
    finally:
        drop_graph(db, name)


def test_explain_single_pair_counts_no_other_pairs(db: FalkorDB) -> None:
    # regression: OPTIONAL MATCH counted the null row as one incompatible pair at n = 1
    name = "swap_edge_tools_single"
    load_pool(generate_pool(1, 1), name, db=db)
    try:
        env = SwapTools(name, db=db).explain_unmatched(0)
        reasoning = env["result"]["reasoning"]
        assert reasoning["abo_incompatible_pairs"] == 0
        assert reasoning["abo_compatible_but_crossmatch_failed"] == 0
        assert env["outcome"] == EMPTY  # no other pair can give to this patient
    finally:
        drop_graph(db, name)
