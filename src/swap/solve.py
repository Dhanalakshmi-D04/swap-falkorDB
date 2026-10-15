"""Pure solvers over candidate cycles / edges: cycle packing, and the unbounded cycle cover.

FalkorDB supplies the candidates (cycles) and the edges; nothing here talks to the graph, and
nothing here writes Cypher. Vertex-disjoint cycle packing is an integer program
(`scipy.optimize.milp`); the unbounded cycle cover is an assignment problem with self-loops
(`scipy.optimize.linear_sum_assignment`, self-loop = unmatched).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, linear_sum_assignment, milp
from scipy.sparse import lil_matrix

MILP_TIME_LIMIT = 120

Cycle = tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PackResult:
    """A vertex-disjoint selection of cycles and what it covers."""

    chosen: list[list[int]]
    covered: int
    objective: float
    status: str
    success: bool


@dataclass(frozen=True, slots=True)
class CoverResult:
    """The unbounded cycle cover: one arc per matched patient, self-loops dropped."""

    arcs: list[tuple[int, int]]
    covered: int


def cycle_weight(cycle: Sequence[int], hard_ids: Iterable[int], priority_weight: float) -> float:
    """Patients a cycle covers, each hard (PRA 0.90) patient counting `priority_weight` times."""
    if priority_weight == 1.0:
        return float(len(cycle))
    hard = set(hard_ids)
    return float(sum(priority_weight if patient in hard else 1.0 for patient in cycle))


def pack_cycles(
    n_pairs: int,
    cycles: Iterable[Sequence[int]],
    *,
    hard_ids: Iterable[int] = (),
    priority_weight: float = 1.0,
) -> PackResult:
    """Choose vertex-disjoint cycles maximising covered patients (weighted coverage)."""
    candidates: list[list[int]] = [[int(p) for p in c] for c in cycles]
    if not candidates:
        return PackResult([], 0, 0.0, "no candidate cycles", False)

    columns = len(candidates)
    weights = np.array(
        [cycle_weight(c, hard_ids, priority_weight) for c in candidates], dtype=float
    )
    coverage = lil_matrix((n_pairs, columns), dtype=float)
    for column, cycle in enumerate(candidates):
        for patient in cycle:
            coverage[patient, column] = 1.0

    constraints = LinearConstraint(coverage.tocsr(), -np.inf, 1.0)
    solution = milp(
        c=-weights,
        constraints=constraints,
        integrality=np.ones(columns),
        bounds=Bounds(0, 1),
        options={"time_limit": MILP_TIME_LIMIT, "presolve": True},
    )
    if solution.x is None:
        return PackResult([], 0, 0.0, str(solution.message), bool(solution.success))

    chosen = [candidates[j] for j in np.flatnonzero(solution.x > 0.5)]
    covered = sum(len(cycle) for cycle in chosen)
    objective = float(-solution.fun) if solution.fun is not None else 0.0
    return PackResult(chosen, covered, objective, str(solution.message), bool(solution.success))


def unbounded_cycle_cover(n_pairs: int, edges: Iterable[Sequence[int]]) -> CoverResult:
    """Maximum cycle cover with cycles of any length, via an assignment with self-loops."""
    if n_pairs <= 0:
        return CoverResult([], 0)

    # Self-loop = unmatched (weight 0); an absent edge is worse than being unmatched.
    floor = -float(n_pairs + 1)
    weights = np.full((n_pairs, n_pairs), floor, dtype=float)
    for source, target in edges:
        weights[int(source), int(target)] = 1.0
    np.fill_diagonal(weights, 0.0)

    rows, columns = linear_sum_assignment(-weights)
    arcs = [(int(i), int(j)) for i, j in zip(rows, columns, strict=True) if i != j]
    return CoverResult(arcs, len(arcs))
