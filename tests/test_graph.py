"""Graph-layer tests. Need FalkorDB on the configured host/port: skipped cleanly without it."""

from __future__ import annotations

from time import perf_counter

import pytest
from falkordb import FalkorDB
from redis.exceptions import RedisError

from swap import graph as graph_module
from swap.config import settings
from swap.generator import Pool, can_give_abo, generate_pool
from swap.graph import can_give_edges, connect, drop_graph, graph_counts, load_pool, read_pair

GRAPH_NAME = "swap_test"
ALT_GRAPH = "swap_test_alt"
BENCH_GRAPH = "swap_bench300"


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
def loaded(db: FalkorDB) -> tuple[Pool, object]:
    pool = generate_pool(60, 4242)
    return pool, load_pool(pool, GRAPH_NAME, db=db)


def test_loaded_graph_holds_exactly_the_pool(db: FalkorDB, loaded: tuple[Pool, object]) -> None:
    pool, stats = loaded
    counts = graph_counts(db.select_graph(GRAPH_NAME))
    assert counts["pairs"] == pool.n == stats.pairs
    assert counts["edges"] == len(pool.edges) == stats.edges
    assert counts["donors"] == pool.n
    assert counts["patients"] == pool.n
    assert counts["sources"] == 1
    assert counts["sinks"] == 1
    assert counts["flow_edges"] == 2 * pool.n + len(pool.edges) == stats.flow_edges
    assert stats.flow_nodes == 2 + 2 * pool.n


def test_graph_edges_are_exactly_the_pool_edges(db: FalkorDB, loaded: tuple[Pool, object]) -> None:
    pool, _ = loaded
    assert can_give_edges(db.select_graph(GRAPH_NAME)) == sorted(pool.edges)
    assert all(a != b for a, b in can_give_edges(db.select_graph(GRAPH_NAME)))


def test_spot_checks_on_nodes_and_edges(db: FalkorDB, loaded: tuple[Pool, object]) -> None:
    pool, _ = loaded
    graph = db.select_graph(GRAPH_NAME)
    stored = set(can_give_edges(graph))

    i, j = pool.edges[0]
    assert (i, j) in stored

    incompatible = [
        (i, j)
        for i in range(pool.n)
        for j in range(pool.n)
        if i != j and not can_give_abo(pool.donor_abo[i], pool.patient_abo[j])
    ]
    assert incompatible
    assert not any(pair in stored for pair in incompatible[:200])

    node = read_pair(graph, i)
    assert node is not None
    assert node["patient_abo"] == pool.patient_abo[i]
    assert node["donor_abo"] == pool.donor_abo[i]
    assert node["pra"] == pool.pra[i]
    assert node["hard"] is pool.hard[i]
    assert read_pair(graph, pool.n + 10_000) is None


def test_reloading_replaces_the_graph_instead_of_duplicating(db: FalkorDB) -> None:
    pool = generate_pool(40, 7)
    load_pool(pool, ALT_GRAPH, db=db)
    load_pool(pool, ALT_GRAPH, db=db)
    counts = graph_counts(db.select_graph(ALT_GRAPH))
    assert counts["pairs"] == 40
    assert counts["edges"] == len(pool.edges)
    drop_graph(db, ALT_GRAPH)


def test_each_graph_name_keeps_its_own_pool(db: FalkorDB, loaded: tuple[Pool, object]) -> None:
    small = generate_pool(25, 99)
    load_pool(small, ALT_GRAPH, db=db)
    assert graph_counts(db.select_graph(ALT_GRAPH))["pairs"] == 25
    assert graph_counts(db.select_graph(GRAPH_NAME))["pairs"] == loaded[0].n
    drop_graph(db, ALT_GRAPH)


def test_flow_network_is_wired_for_algo_max_flow(db: FalkorDB, loaded: tuple[Pool, object]) -> None:
    pool, _ = loaded
    graph = db.select_graph(GRAPH_NAME)
    bad_caps = graph.query("MATCH ()-[f:F]->() WHERE f.cap <> 1 RETURN count(f)").result_set[0][0]
    assert bad_caps == 0
    source_edges = graph.query("MATCH (:S)-[f:F]->(d:Donor) RETURN count(f)").result_set[0][0]
    sink_edges = graph.query("MATCH (p:Patient)-[f:F]->(:T) RETURN count(f)").result_set[0][0]
    assert source_edges == pool.n
    assert sink_edges == pool.n
    wired = graph.query(
        "MATCH (d:Donor)-[f:F]->(p:Patient) RETURN d.id, p.id ORDER BY d.id, p.id"
    ).result_set
    assert [(int(a), int(b)) for a, b in wired] == sorted(pool.edges)


def test_flow_network_ids_are_scalar_integers(db: FalkorDB, loaded: tuple[Pool, object]) -> None:
    # A one-element list id (UNWIND over [[0],[1],...] rather than [0,1,...]) would break every
    # donor/patient lookup, so check the stored ids are single integers.
    pool, _ = loaded
    graph = db.select_graph(GRAPH_NAME)
    ids = {int(value) for (value,) in graph.query("MATCH (d:Donor) RETURN d.id").result_set}
    patient_ids = {int(value) for (value,) in graph.query("MATCH (p:Patient) RETURN p.id").result_set}
    assert ids == set(pool.ids) == patient_ids


def test_a_partial_load_is_rejected(
    db: FalkorDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = generate_pool(20, 3)
    broken = monkeypatch.setattr(
        graph_module,
        "CREATE_FLOW_EDGES",
        "UNWIND $rows AS x MATCH (d:Donor {id: x[0] + 1000}),(p:Patient {id: x[1]}) "
        "CREATE (d)-[:F {cap: 1}]->(p) RETURN count(*)",
    )
    assert broken is None
    with pytest.raises(RuntimeError, match="loaded partially"):
        load_pool(pool, ALT_GRAPH, db=db)
    drop_graph(db, ALT_GRAPH)


def test_empty_pool_loads_cleanly(db: FalkorDB) -> None:
    stats = load_pool(generate_pool(0, 1), ALT_GRAPH, db=db)
    assert stats.pairs == 0
    assert graph_counts(db.select_graph(ALT_GRAPH))["sources"] == 1
    drop_graph(db, ALT_GRAPH)


def test_n300_generate_and_load_within_budget(db: FalkorDB) -> None:
    started = perf_counter()
    pool = generate_pool(300, 20261015)
    stats = load_pool(pool, BENCH_GRAPH, db=db)
    elapsed = perf_counter() - started
    print(
        f"\nN=300: {stats.pairs} pairs, {stats.edges} edges, "
        f"generate+load {elapsed:.2f}s (loader {stats.seconds:.2f}s, budget about 5s)"
    )
    counts = graph_counts(db.select_graph(BENCH_GRAPH))
    assert counts["pairs"] == 300
    assert counts["edges"] == len(pool.edges)
    assert elapsed < 10
    drop_graph(db, BENCH_GRAPH)
