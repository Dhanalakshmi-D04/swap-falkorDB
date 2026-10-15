"""Pure solvers over candidate cycles / edges: cycle packing, and the unbounded cycle cover.

FalkorDB supplies the candidates (cycles) and the edges; nothing here talks to the graph, and
nothing here writes Cypher. Vertex-disjoint cycle packing is an integer program
(`scipy.optimize.milp`); the unbounded cycle cover is an assignment problem with self-loops
(`scipy.optimize.linear_sum_assignment`, self-loop = unmatched).

Patient ids are taken from the candidates themselves, so the solvers work on pools whose ids are
not contiguous (a what-if copy with one pair removed) and on graphs that also hold donor-only
altruist nodes. A candidate is any set of patients that can be covered together - a cycle, or, for
the altruist what-if, a chain. `at_most_one_groups` marks column groups of which at most one may
be chosen (one chain per altruist donor).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, linear_sum_assignment, milp
from scipy.sparse import lil_matrix

MILP_TIME_LIMIT = 120

Cycle = tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PackResult:
    """A vertex-disjoint selection of candidates (cycles, and chains for an altruist)."""

    chosen: list[list[int]]
    covered: int
    objective: float
    status: str
    success: bool
    indices: list[int] = field(default_factory=list)


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
    candidates: Iterable[Sequence[int]],
    *,
    hard_ids: Iterable[int] = (),
    priority_weight: float = 1.0,
    at_most_one_groups: Iterable[Iterable[int]] = (),
) -> PackResult:
    """Choose vertex-disjoint candidates maximising covered patients (weighted coverage).

    `candidates` are patient-id lists (cycles, or altruist chains). `at_most_one_groups` holds
    groups of candidate indices of which at most one may be chosen.
    """
    cycles: list[list[int]] = [[int(p) for p in c] for c in candidates]
    if not cycles:
        return PackResult([], 0, 0.0, "no candidate cycles", False)

    columns = len(cycles)
    weights = np.array(
        [cycle_weight(c, hard_ids, priority_weight) for c in cycles], dtype=float
    )
    patients = sorted({patient for cycle in cycles for patient in cycle})
    row_of = {patient: row for row, patient in enumerate(patients)}
    groups = [list(group) for group in at_most_one_groups]
    coverage = lil_matrix((len(patients) + len(groups), columns), dtype=float)
    for column, cycle in enumerate(cycles):
        for patient in cycle:
            coverage[row_of[patient], column] = 1.0
    for offset, group in enumerate(groups):
        for column in group:
            coverage[len(patients) + offset, column] = 1.0

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

    indices = [int(j) for j in np.flatnonzero(solution.x > 0.5)]
    chosen = [cycles[j] for j in indices]
    covered = sum(len(cycle) for cycle in chosen)
    objective = float(-solution.fun) if solution.fun is not None else 0.0
    return PackResult(chosen, covered, objective, str(solution.message), bool(solution.success), indices)


def unbounded_cycle_cover(edges: Iterable[Sequence[int]]) -> CoverResult:
    """Maximum cycle cover with cycles of any length, via an assignment with self-loops."""
    arcs_in = [(int(source), int(target)) for source, target in edges]
    ids = sorted({source for source, _ in arcs_in} | {target for _, target in arcs_in})
    if not ids:
        return CoverResult([], 0)

    row_of = {patient: row for row, patient in enumerate(ids)}
    size = len(ids)
    # Self-loop = unmatched (weight 0); an absent edge is worse than being unmatched.
    floor = -float(size + 1)
    weights = np.full((size, size), floor, dtype=float)
    for source, target in arcs_in:
        weights[row_of[source], row_of[target]] = 1.0
    np.fill_diagonal(weights, 0.0)

    rows, columns = linear_sum_assignment(-weights)
    arcs = [(ids[i], ids[j]) for i, j in zip(rows, columns, strict=True) if i != j]
    return CoverResult(arcs, len(arcs))
