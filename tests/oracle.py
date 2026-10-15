"""Independent oracle for the tool layer, over a `Pool` object (tests only; AGENTS.md rule 1).

Nothing here touches FalkorDB. From the pool's edge list alone this re-derives: exact cycle counts
for lengths 2 and 3, the bipartite matching (networkx, a different algorithm from `algo.maxFlow`),
vertex-disjoint packing (scipy `milp`) and the unbounded cycle cover (`linear_sum_assignment`).
If the graph tools and this code disagree, one of them is wrong.
"""

from __future__ import annotations

import networkx as nx
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, linear_sum_assignment, milp
from scipy.sparse import lil_matrix

from swap.generator import Pool

MILP_TIME_LIMIT = 120


def adjacency(pool: Pool) -> np.ndarray:
    """Boolean donor -> patient edge matrix built from the pool's edge list."""
    matrix = np.zeros((pool.n, pool.n), dtype=bool)
    for source, target in pool.edges:
        matrix[source, target] = True
    return matrix


def cycles_of_length(edges: np.ndarray, length: int) -> list[tuple[int, ...]]:
    """All directed cycles of exactly `length` vertices, each once, canonical (minimum id first)."""
    n = len(edges)
    found: list[tuple[int, ...]] = []
    if length == 2:
        for a in range(n):
            for b in range(a + 1, n):
                if edges[a, b] and edges[b, a]:
                    found.append((a, b))
        return found
    if length == 3:
        out = [np.flatnonzero(edges[i]).tolist() for i in range(n)]
        for a in range(n):
            for b in out[a]:
                if b <= a:
                    continue
                for c in out[b]:
                    if c > a and c != b and edges[c, a]:
                        found.append((a, b, c))
        return found
    if length == 4:
        out = [np.flatnonzero(edges[i]).tolist() for i in range(n)]
        for a in range(n):
            for b in out[a]:
                if b <= a:
                    continue
                for c in out[b]:
                    if c <= a or c == b:
                        continue
                    for d in out[c]:
                        if d <= a or d == b or d == c:
                            continue
                        if edges[d, a]:
                            found.append((a, b, c, d))
        return found
    raise ValueError(f"length must be 2, 3 or 4, got {length!r}")


def cycles_up_to(edges: np.ndarray, max_len: int) -> list[tuple[int, ...]]:
    """Cycles of every length from 2 to `max_len` (the candidate set a packing runs over)."""
    found: list[tuple[int, ...]] = []
    for length in range(2, max_len + 1):
        found.extend(cycles_of_length(edges, length))
    return found


def cycle_counts(pool: Pool, max_len: int = 3) -> dict[int, int]:
    edges = adjacency(pool)
    return {length: len(cycles_of_length(edges, length)) for length in range(2, max_len + 1)}


def bipartite_matching(edges: np.ndarray) -> int:
    """Maximum donor -> patient matching via networkx (compare with the graph's algo.maxFlow)."""
    n = len(edges)
    graph = nx.Graph()
    graph.add_nodes_from((("d", i) for i in range(n)), bipartite=0)
    graph.add_nodes_from((("p", j) for j in range(n)), bipartite=1)
    graph.add_edges_from((("d", i), ("p", j)) for i, j in zip(*np.nonzero(edges), strict=True))
    matching = nx.bipartite.maximum_matching(graph, top_nodes=[("d", i) for i in range(n)])
    return sum(1 for node in matching if node[0] == "d")


def pack(n_pairs: int, cycles: list[tuple[int, ...]]) -> int:
    """Maximum vertex-disjoint packing of the given cycles, via scipy `milp`."""
    if not cycles:
        return 0
    coverage = lil_matrix((n_pairs, len(cycles)), dtype=float)
    for column, cycle in enumerate(cycles):
        for patient in cycle:
            coverage[patient, column] = 1.0
    solution = milp(
        c=-np.array([len(cycle) for cycle in cycles], dtype=float),
        constraints=LinearConstraint(coverage.tocsr(), -np.inf, 1.0),
        integrality=np.ones(len(cycles)),
        bounds=Bounds(0, 1),
        options={"time_limit": MILP_TIME_LIMIT, "presolve": True},
    )
    return round(-solution.fun)


def unbounded_cycle_cover(edges: np.ndarray) -> int:
    """Maximum cycle cover with cycles of any length, via an assignment with self-loops."""
    n = len(edges)
    if n == 0:
        return 0
    weights = np.where(edges, 1.0, -float(n + 1))
    np.fill_diagonal(weights, 0.0)
    rows, columns = linear_sum_assignment(-weights)
    return sum(1 for i, j in zip(rows, columns, strict=True) if i != j)
