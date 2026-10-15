"""M8 API tests: every endpoint through FastAPI's TestClient. Needs FalkorDB: skipped without it.

Pinned here: unknown ids answer HTTP 200 with outcome `unknown_entity` (never a 500), and bad
input answers 422 (validated by the request models / query bounds before any graph work).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from swap.api import app
from swap.config import settings
from swap.envelope import Outcome
from swap.graph import connect

VALID = {outcome.value for outcome in Outcome}
ENVELOPE_KEYS = {"outcome", "result", "path", "trace"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    cfg = settings()
    try:
        connect(cfg).list_graphs()
    except RedisError as exc:
        pytest.skip(f"FalkorDB unreachable at {cfg.where()} ({exc}); start it with `make up`")
    return TestClient(app)


def _load_pool(client: TestClient, n: int = 60, seed: int = 3) -> None:
    response = client.post("/pool", json={"n": n, "seed": seed})
    assert response.status_code == 200
    assert response.json()["summary"]["n"] == n


def test_pool_loads_then_every_tool_answers(client: TestClient) -> None:
    _load_pool(client)

    queries = {
        "/tools/find_cycles": {"max_len": 3},
        "/tools/flow_bound": {},
        "/tools/best_set": {"max_len": 3, "priority": 2.0},
    }
    for path, params in queries.items():
        response = client.get(path, params=params)
        assert response.status_code == 200, path
        env = response.json()
        assert set(env) == ENVELOPE_KEYS, path
        assert env["outcome"] in VALID, path
        assert set(env["trace"]) == {"tool", "args", "ms", "rows"}, path

    assert client.get("/tools/explain/0").json()["outcome"] in VALID
    for body in (
        {"kind": "add_altruistic_donor", "donor_abo": "O"},
        {"kind": "remove_pair", "pair_id": 0},
        {"kind": "set_priority", "pair_id": 1, "weight": 3.0},
    ):
        response = client.post("/tools/what_if", json=body)
        assert response.status_code == 200, body
        env = response.json()
        assert set(env) == ENVELOPE_KEYS, body
        assert env["outcome"] in VALID, body


def test_unknown_id_is_200_with_unknown_entity(client: TestClient) -> None:
    _load_pool(client, n=30, seed=1)

    explained = client.get("/tools/explain/1000000000")
    assert explained.status_code == 200
    assert explained.json()["outcome"] == Outcome.UNKNOWN_ENTITY.value
    assert explained.json()["path"] == []

    removed = client.post("/tools/what_if", json={"kind": "remove_pair", "pair_id": 1000000000})
    assert removed.status_code == 200
    assert removed.json()["outcome"] == Outcome.UNKNOWN_ENTITY.value

    missing = client.post("/tools/what_if", json={"kind": "set_priority"})
    assert missing.status_code == 200
    assert missing.json()["outcome"] == Outcome.UNKNOWN_ENTITY.value


def test_bad_input_is_422(client: TestClient) -> None:
    assert client.post("/pool", json={"n": "lots", "seed": 1}).status_code == 422
    assert client.post("/pool", json={"n": -5}).status_code == 422
    assert client.get("/tools/find_cycles", params={"max_len": 99}).status_code == 422
    assert client.get("/tools/explain/not-an-int").status_code == 422
    assert client.get("/tools/best_set", params={"priority": -1}).status_code == 422
    assert client.post("/tools/what_if", json={"kind": "nope"}).status_code == 422
    bad_abo = {"kind": "add_altruistic_donor", "donor_abo": "Z"}
    assert client.post("/tools/what_if", json=bad_abo).status_code == 422


def test_proof_panel_checks_agree_and_ordering_holds(client: TestClient) -> None:
    _load_pool(client, n=60, seed=4)
    body = client.get("/proof").json()

    assert body["loaded"] is True
    values = body["ordering"]["values"]
    assert values[0] <= values[1] <= values[2] <= values[3]
    assert body["ordering"]["holds"] is True

    names = {check["name"] for check in body["checks"]}
    assert "2-cycle count" in names and "3-cycle count" in names
    assert "maximum matching (algo.maxFlow)" in names
    assert all(check["agrees"] for check in body["checks"])
    assert body["all_checks_pass"] is True

    falkordb = body["falkordb"]
    assert falkordb["cycles"]["2"] == body["crosscheck"]["cycles"]["2"]
    assert falkordb["cycles"]["3"] == body["crosscheck"]["cycles"]["3"]


def test_trace_log_records_tool_calls(client: TestClient) -> None:
    _load_pool(client, n=20, seed=2)
    before = client.get("/trace").json()["count"]

    client.get("/tools/find_cycles", params={"max_len": 2})
    body = client.get("/trace").json()
    assert body["count"] == before + 1
    assert body["limit"] == 200
    last = body["traces"][-1]
    assert set(last) == {"tool", "args", "ms", "rows", "outcome"}
    assert last["tool"] == "find_cycles"


def test_graph_view_returns_nodes_and_chosen_cycles(client: TestClient) -> None:
    _load_pool(client, n=50, seed=6)
    body = client.get("/graph").json()

    assert len(body["nodes"]) == 50
    assert body["counts"]["pairs"] == 50
    assert body["counts"]["edges"] == body["edge_count"]
    assert isinstance(body["edges"], list) and isinstance(body["chosen"], list)
    assert body["truncated"] is False
    for node in body["nodes"][:5]:
        assert {"id", "patient_abo", "donor_abo", "pra", "hard"} <= set(node)


def test_graph_view_caps_edges_for_large_pools(client: TestClient) -> None:
    _load_pool(client, n=200, seed=8)
    body = client.get("/graph", params={"max_len": 2}).json()

    assert body["truncated"] is True
    assert len(body["edges"]) < body["edge_count"]
    covered = {patient for cycle in body["chosen"] for patient in cycle}
    assert all(edge[0] in covered and edge[1] in covered for edge in body["edges"])
    assert len(body["nodes"]) == 200
