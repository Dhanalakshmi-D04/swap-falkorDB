"""FalkorDB layer: one graph per pool name.

Nodes `(:Pair {id, patient_abo, donor_abo, pra, hard})`, edges `(:Pair)-[:CAN_GIVE]->(:Pair)`,
plus a donor -> patient flow network (`:S`, `:Donor`, `:Patient`, `:T` joined by `:F {cap: 1}`)
so `algo.maxFlow` can run. Every statement here is human-written and parameterised; each one
returns the number of rows it created, and `load_pool` refuses to report success on a partial load.

Once the pairs are in, FalkorDB turns every 2- and 3-way swap it can find into a swap object,
`(:Cycle {key, len, members, hard, back_arcs, live, taken})-[:HAS {pos}]->(:Pair)`: the rings the
planner chooses from, the ones a failed crossmatch kills, and the ones an approval takes, all live in
the graph. `members[i]`'s donor gives to `members[i + 1]`'s patient (the last to the first), rotated
so the smallest id comes first; `key` is the same ids joined by `>`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from falkordb import FalkorDB, Graph

from swap import cache
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
    "CREATE INDEX FOR (c:Cycle) ON (c.key)",
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

# Swap objects. The pattern match canonicalises each directed cycle to its smallest id (as in
# tools.FIND_CYCLES_2/3); `back_arcs` counts the reverse arcs inside a 3-way swap, each of which keeps
# a 2-way swap alive if the third pair drops out (Manlove & O'Malley 2014, the UK scheme's criterion).
MATERIALISE_2 = (
    "MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(a) WHERE a.id < b.id "
    "CREATE (c:Cycle {key: toString(a.id) + '>' + toString(b.id), len: 2, members: [a.id, b.id], "
    "hard: (CASE WHEN a.hard THEN 1 ELSE 0 END) + (CASE WHEN b.hard THEN 1 ELSE 0 END), "
    "back_arcs: 0, live: true, taken: false}) "
    "CREATE (c)-[:HAS {pos: 0}]->(a), (c)-[:HAS {pos: 1}]->(b) RETURN count(c)"
)
MATERIALISE_3 = (
    "MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(a) "
    "WHERE a.id < b.id AND a.id < c.id "
    "OPTIONAL MATCH (b)-[r1:CAN_GIVE]->(a) OPTIONAL MATCH (c)-[r2:CAN_GIVE]->(b) "
    "OPTIONAL MATCH (a)-[r3:CAN_GIVE]->(c) "
    "WITH a, b, c, (CASE WHEN r1 IS NULL THEN 0 ELSE 1 END) + (CASE WHEN r2 IS NULL THEN 0 ELSE 1 END)"
    " + (CASE WHEN r3 IS NULL THEN 0 ELSE 1 END) AS back "
    "CREATE (cy:Cycle {key: toString(a.id) + '>' + toString(b.id) + '>' + toString(c.id), len: 3, "
    "members: [a.id, b.id, c.id], hard: (CASE WHEN a.hard THEN 1 ELSE 0 END) "
    "+ (CASE WHEN b.hard THEN 1 ELSE 0 END) + (CASE WHEN c.hard THEN 1 ELSE 0 END), "
    "back_arcs: back, live: true, taken: false}) "
    "CREATE (cy)-[:HAS {pos: 0}]->(a), (cy)-[:HAS {pos: 1}]->(b), (cy)-[:HAS {pos: 2}]->(c) "
    "RETURN count(cy)"
)

COUNT_PAIRS = "MATCH (p:Pair) RETURN count(p)"
COUNT_CYCLES = "MATCH (c:Cycle) RETURN count(c)"
COUNT_PAIR_EDGES = "MATCH ()-[r:CAN_GIVE]->() RETURN count(r)"
COUNT_DONORS = "MATCH (d:Donor) RETURN count(d)"
COUNT_PATIENTS = "MATCH (p:Patient) RETURN count(p)"
COUNT_SOURCES = "MATCH (s:S) RETURN count(s)"
COUNT_SINKS = "MATCH (t:T) RETURN count(t)"
COUNT_FLOW_EDGES = "MATCH ()-[f:F]->() RETURN count(f)"
READ_PAIR_EDGES = "MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair) RETURN a.id, b.id ORDER BY a.id, b.id"
READ_PAIR = "MATCH (p:Pair {id: $id}) RETURN p.id, p.patient_abo, p.donor_abo, p.pra, p.hard"
READ_PAIRS = (
    "MATCH (p:Pair) RETURN p.id, p.patient_abo, p.donor_abo, p.pra, p.hard ORDER BY p.id"
)

# Donor-only altruist nodes (the add_altruistic_donor what-if). They carry a donor ABO type and
# CAN_GIVE edges to every ABO-compatible patient, and join the flow network as a Donor with no
# Patient side. They are labelled `:Altruist` (never `:Pair`), so they can start a chain but never
# appear in a cycle, in `MATCH (a:Pair)`, or in the unbounded cycle cover.
CREATE_ALTRUIST = "CREATE (:Altruist {id: $id, donor_abo: $abo}) RETURN count(*)"
CREATE_ALTRUIST_EDGES = (
    "MATCH (a:Altruist {id: $id}) MATCH (p:Pair) "
    "WHERE a.donor_abo = 'O' OR p.patient_abo = 'AB' OR a.donor_abo = p.patient_abo "
    "CREATE (a)-[:CAN_GIVE]->(p) RETURN count(*)"
)
CREATE_ALTRUIST_DONOR = "CREATE (:Donor {id: $id}) RETURN count(*)"
CREATE_ALTRUIST_SOURCE_EDGE = (
    "MATCH (s:S),(d:Donor {id: $id}) CREATE (s)-[:F {cap: 1}]->(d) RETURN count(*)"
)
CREATE_ALTRUIST_FLOW_EDGES = (
    "MATCH (a:Altruist {id: $id})-[:CAN_GIVE]->(p:Pair) "
    "MATCH (d:Donor {id: $id}),(pt:Patient) WHERE pt.id = p.id "
    "CREATE (d)-[:F {cap: 1}]->(pt) RETURN count(*)"
)

#: The loader statements, named, so `GET /cypher` can show them next to the tool queries.
CYPHER: dict[str, str] = {
    "loader_index_pair": INDEX_STATEMENTS[0],
    "loader_index_donor": INDEX_STATEMENTS[1],
    "loader_index_patient": INDEX_STATEMENTS[2],
    "loader_index_cycle": INDEX_STATEMENTS[3],
    "loader_pairs": CREATE_PAIRS,
    "loader_pair_edges": CREATE_PAIR_EDGES,
    "loader_source": CREATE_SOURCE_NODE,
    "loader_sink": CREATE_SINK_NODE,
    "loader_donor_nodes": CREATE_DONOR_NODES,
    "loader_patient_nodes": CREATE_PATIENT_NODES,
    "loader_source_edges": CREATE_SOURCE_EDGES,
    "loader_flow_edges": CREATE_FLOW_EDGES,
    "loader_sink_edges": CREATE_SINK_EDGES,
    "materialise_2": MATERIALISE_2,
    "materialise_3": MATERIALISE_3,
    "loader_altruist": CREATE_ALTRUIST,
    "loader_altruist_edges": CREATE_ALTRUIST_EDGES,
    "loader_altruist_donor": CREATE_ALTRUIST_DONOR,
    "loader_altruist_source_edge": CREATE_ALTRUIST_SOURCE_EDGE,
    "loader_altruist_flow_edges": CREATE_ALTRUIST_FLOW_EDGES,
    "loader_read_pairs": READ_PAIRS,
    "loader_read_pair_edges": READ_PAIR_EDGES,
    "loader_read_pair": READ_PAIR,
    "loader_count_pairs": COUNT_PAIRS,
    "loader_count_pair_edges": COUNT_PAIR_EDGES,
    "loader_count_flow_edges": COUNT_FLOW_EDGES,
}


@dataclass(frozen=True, slots=True)
class LoadStats:
    graph: str
    pairs: int
    edges: int
    flow_nodes: int
    flow_edges: int
    seconds: float
    cycles_by_len: dict[int, int]
    cycle_ms: dict[int, float]

    @property
    def cycles(self) -> int:
        return sum(self.cycles_by_len.values())

    def summary(self) -> dict[str, object]:
        return {
            "graph": self.graph,
            "pairs": self.pairs,
            "edges": self.edges,
            "flow_nodes": self.flow_nodes,
            "flow_edges": self.flow_edges,
            "cycles": self.cycles,
            "cycles_by_len": self.cycles_by_len,
            "seconds": round(self.seconds, 3),
            "cycle_ms": self.cycle_ms,
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
    cache.forget(name)
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
    cache.forget(name)
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

    cycles_by_len: dict[int, int] = {}
    cycle_ms: dict[int, float] = {}
    for length, statement in ((2, MATERIALISE_2), (3, MATERIALISE_3)):
        cycles_started = perf_counter()
        cycles_by_len[length] = _count(graph, statement)
        cycle_ms[length] = round((perf_counter() - cycles_started) * 1000, 1)

    return LoadStats(
        graph=name,
        pairs=created["pairs"],
        edges=created["edges"],
        flow_nodes=2 + created["donors"] + created["patients"],
        flow_edges=(
            created["source_edges"] + created["flow_edges"] + created["sink_edges"]
        ),
        seconds=perf_counter() - started,
        cycles_by_len=cycles_by_len,
        cycle_ms=cycle_ms,
    )


def ensure_swap_objects(graph: Graph) -> dict[int, int]:
    """Store the swap objects of a graph loaded before they existed (an old volume, say).

    A graph that already holds any `:Cycle` is left alone. Returns the swaps created per length.
    """
    if _count(graph, COUNT_CYCLES):
        return {}
    with suppress(Exception):  # the index may already exist
        graph.query(INDEX_STATEMENTS[3])
    return {2: _count(graph, MATERIALISE_2), 3: _count(graph, MATERIALISE_3)}


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


def pool_from_graph(graph: Graph, seed: int = 0) -> Pool:
    """Rebuild a `Pool` from the `:Pair` nodes and `CAN_GIVE` edges already in the graph.

    Used by the what-if tools to make a modified copy without keeping the original `Pool` around,
    so the copy is built from the same graph the product answers from.
    """
    rows = graph.query(READ_PAIRS).result_set
    return Pool(
        n=len(rows),
        patient_abo=[str(r[1]) for r in rows],
        donor_abo=[str(r[2]) for r in rows],
        pra=[float(r[3]) for r in rows],
        ids=[int(r[0]) for r in rows],
        hard=[bool(r[4]) for r in rows],
        seed=seed,
        edges=can_give_edges(graph),
    )


def attach_altruist(graph: Graph, altruist_id: int, donor_abo: str) -> int:
    """Add one donor-only `:Altruist` to an already-loaded graph and wire it into the flow network.

    Returns the number of `CAN_GIVE` edges the altruist got. The node is created after `load_pool`,
    so a scratch what-if copy can carry it without a `Pool` that models donor-only nodes.
    """
    args = {"id": int(altruist_id), "abo": donor_abo}
    if _count(graph, CREATE_ALTRUIST, args) != 1:
        raise RuntimeError(f"could not create altruist {altruist_id}")
    created = _count(graph, CREATE_ALTRUIST_EDGES, args)
    if _count(graph, CREATE_ALTRUIST_DONOR, args) != 1:
        raise RuntimeError(f"could not create altruist donor node {altruist_id}")
    _count(graph, CREATE_ALTRUIST_SOURCE_EDGE, args)
    if _count(graph, CREATE_ALTRUIST_FLOW_EDGES, args) != created:
        raise RuntimeError(f"altruist {altruist_id}: CAN_GIVE edges and flow edges disagree")
    return created


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
