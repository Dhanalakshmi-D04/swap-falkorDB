"""FalkorDB layer: one graph per pool name.

Nodes `(:Pair {id, patient_abo, donor_abo, pra, hard})`, edges `(:Pair)-[:CAN_GIVE]->(:Pair)`,
plus a donor -> patient flow network (`:S`, `:Donor`, `:Patient`, `:T` joined by `:F {cap: 1}`)
so `algo.maxFlow` can run. Every statement here is human-written and parameterised; each one
returns the number of rows it created, and `load_pool` refuses to report success on a partial load.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from falkordb import FalkorDB, Graph

from swap.config import DEFAULT_GRAPH, Settings, settings
from swap.generator import Pool

BATCH = 5000

# FalkorDB caps a result set at RESULTSET_SIZE rows (default 10000) and truncates silently, which
# would drop cycles and edges on a 300-pair pool. Every connection raises the cap once.
RESULT_SET_SIZE = 10_000_000

INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX FOR (p:Pair) ON (p.id)",
    "CREATE INDEX FOR (d:Donor) ON (d.id)",
    "CREATE INDEX FOR (p:Patient) ON (p.id)",
)

CREATE_PAIRS = (
    "UNWIND $rows AS r "
    "CREATE (:Pair {id: r[0], patient_abo: r[1], donor_abo: r[2], pra: r[3], hard: r[4]}) "
    "RETURN count(*)"
)
CREATE_PAIR_EDGES = (
    "UNWIND $rows AS x MATCH (a:Pair {id: x[0]}),(b:Pair {id: x[1]}) "
    "CREATE (a)-[:CAN_GIVE]->(b) RETURN count(*)"
)
CREATE_SOURCE_NODE = "CREATE (:S) RETURN count(*)"
CREATE_SINK_NODE = "CREATE (:T) RETURN count(*)"
CREATE_DONOR_NODES = "UNWIND $rows AS x CREATE (:Donor {id: x}) RETURN count(*)"
CREATE_PATIENT_NODES = "UNWIND $rows AS x CREATE (:Patient {id: x}) RETURN count(*)"
CREATE_SOURCE_EDGES = (
    "UNWIND $rows AS x MATCH (s:S),(d:Donor {id: x}) CREATE (s)-[:F {cap: 1}]->(d) RETURN count(*)"
)
CREATE_FLOW_EDGES = (
    "UNWIND $rows AS x MATCH (d:Donor {id: x[0]}),(p:Patient {id: x[1]}) "
    "CREATE (d)-[:F {cap: 1}]->(p) RETURN count(*)"
)
CREATE_SINK_EDGES = (
    "UNWIND $rows AS x MATCH (p:Patient {id: x}),(t:T) CREATE (p)-[:F {cap: 1}]->(t) RETURN count(*)"
)

COUNT_PAIRS = "MATCH (p:Pair) RETURN count(p)"
COUNT_PAIR_EDGES = "MATCH ()-[r:CAN_GIVE]->() RETURN count(r)"
COUNT_DONORS = "MATCH (d:Donor) RETURN count(d)"
COUNT_PATIENTS = "MATCH (p:Patient) RETURN count(p)"
COUNT_SOURCES = "MATCH (s:S) RETURN count(s)"
COUNT_SINKS = "MATCH (t:T) RETURN count(t)"
COUNT_FLOW_EDGES = "MATCH ()-[f:F]->() RETURN count(f)"
READ_PAIR_EDGES = "MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair) RETURN a.id, b.id ORDER BY a.id, b.id"
READ_PAIR = "MATCH (p:Pair {id: $id}) RETURN p.id, p.patient_abo, p.donor_abo, p.pra, p.hard"


@dataclass(frozen=True, slots=True)
class LoadStats:
    graph: str
    pairs: int
    edges: int
    flow_nodes: int
    flow_edges: int
    seconds: float

    def summary(self) -> dict[str, object]:
        return {
            "graph": self.graph,
            "pairs": self.pairs,
            "edges": self.edges,
            "flow_nodes": self.flow_nodes,
            "flow_edges": self.flow_edges,
            "seconds": round(self.seconds, 3),
        }


def connect(cfg: Settings | None = None) -> FalkorDB:
    cfg = cfg if cfg is not None else settings()
    db = FalkorDB(host=cfg.host, port=cfg.port)
    ensure_result_set_size(db)
    return db


def ensure_result_set_size(db: FalkorDB, size: int = RESULT_SET_SIZE) -> None:
    """Raise the server-side result-set cap; a silent truncation would corrupt every count."""
    try:
        if int(db.config_get("RESULTSET_SIZE")) < size:
            db.config_set("RESULTSET_SIZE", size)
    except Exception as exc:  # surfaced loudly; never a silent truncation
        raise RuntimeError(
            f"could not set RESULTSET_SIZE to {size} (got {exc!r}); refusing to run with a "
            "truncated result set"
        ) from exc


def drop_graph(db: FalkorDB, name: str) -> bool:
    """Delete a graph by name; False when it did not exist."""
    if name in db.list_graphs():
        db.select_graph(name).delete()
        return True
    return False


def load_pool(
    pool: Pool,
    name: str = DEFAULT_GRAPH,
    *,
    db: FalkorDB | None = None,
    cfg: Settings | None = None,
) -> LoadStats:
    """Replace graph `name` with the pool's pairs, edges and flow network."""
    db = db if db is not None else connect(cfg)
    ensure_result_set_size(db)
    started = perf_counter()
    drop_graph(db, name)
    graph = db.select_graph(name)
    for statement in INDEX_STATEMENTS:
        graph.query(statement)

    ids = [int(pair_id) for pair_id in pool.ids]
    edge_rows = pool.edge_rows()
    created = {
        "pairs": _run(graph, CREATE_PAIRS, pool.node_rows()),
        "edges": _run(graph, CREATE_PAIR_EDGES, edge_rows),
        "source": _count(graph, CREATE_SOURCE_NODE),
        "sink": _count(graph, CREATE_SINK_NODE),
        "donors": _run(graph, CREATE_DONOR_NODES, ids),
        "patients": _run(graph, CREATE_PATIENT_NODES, ids),
        "source_edges": _run(graph, CREATE_SOURCE_EDGES, ids),
        "flow_edges": _run(graph, CREATE_FLOW_EDGES, edge_rows),
        "sink_edges": _run(graph, CREATE_SINK_EDGES, ids),
    }
    expected = {
        "pairs": pool.n,
        "edges": len(pool.edges),
        "source": 1,
        "sink": 1,
        "donors": pool.n,
        "patients": pool.n,
        "source_edges": pool.n,
        "flow_edges": len(pool.edges),
        "sink_edges": pool.n,
    }
    if created != expected:
        raise RuntimeError(f"graph {name!r} loaded partially: created {created}, wanted {expected}")

    return LoadStats(
        graph=name,
        pairs=created["pairs"],
        edges=created["edges"],
        flow_nodes=2 + created["donors"] + created["patients"],
        flow_edges=(
            created["source_edges"] + created["flow_edges"] + created["sink_edges"]
        ),
        seconds=perf_counter() - started,
    )


def graph_counts(graph: Graph) -> dict[str, int]:
    return {
        "pairs": _count(graph, COUNT_PAIRS),
        "edges": _count(graph, COUNT_PAIR_EDGES),
        "donors": _count(graph, COUNT_DONORS),
        "patients": _count(graph, COUNT_PATIENTS),
        "sources": _count(graph, COUNT_SOURCES),
        "sinks": _count(graph, COUNT_SINKS),
        "flow_edges": _count(graph, COUNT_FLOW_EDGES),
    }


def can_give_edges(graph: Graph) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in graph.query(READ_PAIR_EDGES).result_set]


def read_pair(graph: Graph, pair_id: int) -> dict[str, Any] | None:
    rows = graph.query(READ_PAIR, {"id": int(pair_id)}).result_set
    if not rows:
        return None
    pid, patient_abo, donor_abo, pra, hard = rows[0]
    return {
        "id": int(pid),
        "patient_abo": patient_abo,
        "donor_abo": donor_abo,
        "pra": float(pra),
        "hard": bool(hard),
    }


def _run(graph: Graph, statement: str, rows: Sequence[Any]) -> int:
    """Run a batch statement over plain-Python rows, summing the rows it reports creating."""
    created = 0
    for chunk in _chunks(rows, BATCH):
        created += _count(graph, statement, {"rows": list(chunk)})
    return created


def _chunks(rows: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _count(graph: Graph, statement: str, params: dict[str, Any] | None = None) -> int:
    return int(graph.query(statement, params).result_set[0][0])
