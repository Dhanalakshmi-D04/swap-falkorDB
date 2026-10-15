"""Independent cycle-count / matching check, used only by `GET /proof`.

The product's answers always come from FalkorDB; this is a labelled, deliberately simple second
opinion computed from the same `Pool` object the graph was loaded from. `src/` never imports
networkx - the richer networkx oracle lives in `tests/oracle.py` - so this is plain Python.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from swap.generator import Pool


@dataclass(frozen=True, slots=True)
class Check:
    """One independent number, next to the FalkorDB one it should equal."""

    name: str
    crosscheck: int
    falkordb: int

    @property
    def agrees(self) -> bool:
        return self.crosscheck == self.falkordb


def adjacency(pool: Pool) -> dict[int, set[int]]:
    """Donor -> patient neighbours, from the pool's edge list alone."""
    neighbours: dict[int, set[int]] = {pid: set() for pid in pool.ids}
    for source, target in pool.edges:
        neighbours[source].add(target)
    return neighbours


def cycles_of_length(pool: Pool, length: int) -> int:
    """Count directed cycles of exactly `length` vertices, each once (smallest id first)."""
    neighbours = adjacency(pool)
    ids = sorted(pool.ids)
    total = 0
    if length == 2:
        for a in ids:
            total += sum(1 for b in neighbours[a] if b > a and a in neighbours[b])
        return total
    if length == 3:
        for a in ids:
            for b in neighbours[a]:
                if b <= a:
                    continue
                for c in neighbours[b]:
                    if c > a and c != b and a in neighbours[c]:
                        total += 1
        return total
    raise ValueError(f"length must be 2 or 3, got {length!r}")


def cycle_counts(pool: Pool, max_len: int = 3) -> dict[int, int]:
    return {length: cycles_of_length(pool, length) for length in range(2, max_len + 1)}


def matching_size(pool: Pool) -> int:
    """Maximum donor -> patient matching by Kuhn's augmenting paths (cf. `algo.maxFlow`)."""
    neighbours = adjacency(pool)
    patient_of: dict[int, int] = {}

    def augment(donor: int, seen: set[int]) -> bool:
        for patient in sorted(neighbours[donor]):
            if patient in seen:
                continue
            seen.add(patient)
            if patient not in patient_of or augment(patient_of[patient], seen):
                patient_of[patient] = donor
                return True
        return False

    return sum(1 for donor in sorted(pool.ids) if augment(donor, set()))


def checks(pool: Pool, *, cycles: dict[int, int], flow_bound: int) -> list[Check]:
    """Line up the independent numbers against the FalkorDB ones /proof reports."""
    local = cycle_counts(pool)
    return [
        Check("2-cycle count", local.get(2, 0), cycles.get(2, 0)),
        Check("3-cycle count", local.get(3, 0), cycles.get(3, 0)),
        Check("maximum matching (algo.maxFlow)", matching_size(pool), flow_bound),
    ]


def ordered(values: Iterable[int]) -> bool:
    """True when the sequence is non-decreasing (the 2 <= 3 <= cover <= bound invariant)."""
    items = list(values)
    return all(left <= right for left, right in pairwise(items))
