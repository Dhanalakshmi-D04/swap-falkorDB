"""Synthetic incompatible pairs and the donor -> patient compatibility graph.

Frequencies and the pool rule follow Roth, Sonmez and Unver, "Efficient Kidney Exchange",
AER 97(3) 2007, Table 1 and section III.B (OPTN/SRTR data 1993-2002). We omit their
wife-husband crossmatch adjustment. Synthetic patients only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

ABO_TYPES: tuple[str, ...] = ("O", "A", "B", "AB")
ABO_FREQ: tuple[float, ...] = (0.4814, 0.3373, 0.1428, 0.0385)

# donor ABO (row) -> patient ABO (column): O is the universal donor, AB the universal recipient
ABO_COMPATIBLE = np.array(
    [
        [True, True, True, True],
        [False, True, False, True],
        [False, False, True, True],
        [False, False, False, True],
    ],
    dtype=bool,
)

PRA_LEVELS: tuple[float, ...] = (0.05, 0.45, 0.90)
PRA_FREQ: tuple[float, ...] = (0.7019, 0.2000, 0.0981)
HARD_PRA = 0.90

# In the cited source the tabulated PRA level is also the probability that a crossmatch
# against a randomly drawn donor comes back positive.
CROSSMATCH_POSITIVE_PROB: tuple[float, ...] = PRA_LEVELS

EDGE_ROW_BLOCK = 1024


@dataclass(frozen=True, slots=True)
class DrawnPairs:
    """Incompatible pairs only: attributes of the patients and their willing donors."""

    n: int
    patient_abo: list[str]
    donor_abo: list[str]
    pra: list[float]
    ids: list[int]
    hard: list[bool]

    def node_rows(self) -> list[list[object]]:
        """Plain Python rows (id, patient_abo, donor_abo, pra, hard) for the graph loader."""
        return [
            [pid, patient, donor, float(pra), bool(is_hard)]
            for pid, patient, donor, pra, is_hard in zip(
                self.ids, self.patient_abo, self.donor_abo, self.pra, self.hard, strict=True
            )
        ]

    @property
    def hard_ids(self) -> list[int]:
        return [pid for pid, is_hard in zip(self.ids, self.hard, strict=True) if is_hard]

    def summary(self) -> dict[str, object]:
        return {
            "n": self.n,
            "hard": len(self.hard_ids),
            "patient_abo": {t: self.patient_abo.count(t) for t in ABO_TYPES},
            "pra": {level: self.pra.count(level) for level in PRA_LEVELS},
        }


@dataclass(frozen=True, slots=True)
class Pool(DrawnPairs):
    """A draw of pairs plus the donor -> patient edge list over those pairs."""

    seed: int
    edges: list[tuple[int, int]]

    def edge_rows(self) -> list[list[int]]:
        """Plain Python [i, j] rows for the graph loader."""
        return [[i, j] for i, j in self.edges]

    def summary(self) -> dict[str, object]:
        # `super()` is unusable here: `@dataclass(slots=True)` rebuilds the class object, so the
        # zero-arg `super()` cell points at the pre-slots class. Call the base explicitly.
        return {**DrawnPairs.summary(self), "seed": self.seed, "edges": len(self.edges)}


def can_give_abo(donor_abo: str, patient_abo: str) -> bool:
    """True when the donor's ABO type is compatible with the patient's."""
    return bool(ABO_COMPATIBLE[ABO_TYPES.index(donor_abo), ABO_TYPES.index(patient_abo)])


def crossmatch_positive_probability(pra: float) -> float:
    """Probability that the crossmatch against a randomly drawn donor is positive."""
    return float(pra)


def pair_is_incompatible(patient_abo: str, donor_abo: str, crossmatch_positive: bool) -> bool:
    """Pool rule: a drawn pair is kept only when its donor cannot give to its own patient."""
    return not (can_give_abo(donor_abo, patient_abo) and not crossmatch_positive)


def draw_pairs(n: int, rng: np.random.Generator) -> DrawnPairs:
    """Draw `n` incompatible pairs: redraw until a pair is incompatible with its own donor."""
    if n < 0:
        raise ValueError("n must be non-negative")

    patient: list[str] = []
    donor: list[str] = []
    pra: list[float] = []
    crossmatch_prob = np.asarray(CROSSMATCH_POSITIVE_PROB)

    while len(patient) < n:
        want = n - len(patient)
        block = max(2 * want, 64)
        donor_ix = rng.choice(len(ABO_TYPES), size=block, p=ABO_FREQ)
        patient_ix = rng.choice(len(ABO_TYPES), size=block, p=ABO_FREQ)
        pra_ix = rng.choice(len(PRA_LEVELS), size=block, p=PRA_FREQ)
        passes = rng.random(block) >= crossmatch_prob[pra_ix]
        keep = np.flatnonzero(~(ABO_COMPATIBLE[donor_ix, patient_ix] & passes))[:want]
        patient.extend(ABO_TYPES[i] for i in patient_ix[keep])
        donor.extend(ABO_TYPES[i] for i in donor_ix[keep])
        pra.extend(float(PRA_LEVELS[i]) for i in pra_ix[keep])

    return DrawnPairs(
        n=len(patient),
        patient_abo=patient,
        donor_abo=donor,
        pra=pra,
        ids=list(range(len(patient))),
        hard=[level == HARD_PRA for level in pra],
    )


def build_edges(pairs: DrawnPairs, rng: np.random.Generator) -> list[tuple[int, int]]:
    """Edge i->j when donor_i is ABO-compatible with patient_j and that crossmatch passes."""
    donor_ix = np.array([ABO_TYPES.index(t) for t in pairs.donor_abo])
    patient_ix = np.array([ABO_TYPES.index(t) for t in pairs.patient_abo])
    pra = np.asarray(pairs.pra)
    edges: list[tuple[int, int]] = []

    for start in range(0, pairs.n, EDGE_ROW_BLOCK):
        stop = min(start + EDGE_ROW_BLOCK, pairs.n)
        abo_ok = ABO_COMPATIBLE[donor_ix[start:stop, None], patient_ix[None, :]]
        passes = rng.random((stop - start, pairs.n)) >= pra[None, :]
        mask = abo_ok & passes
        mask[np.arange(stop - start), np.arange(start, stop)] = False
        edges.extend(
            (int(start + row), int(col))
            for row, col in zip(*np.nonzero(mask), strict=True)
        )
    return edges


def generate_pool(n: int, seed: int) -> Pool:
    """Draw `n` incompatible pairs and their edges, deterministically from `seed`."""
    rng = np.random.default_rng(seed)
    pairs = draw_pairs(n, rng)
    return Pool(
        n=pairs.n,
        patient_abo=pairs.patient_abo,
        donor_abo=pairs.donor_abo,
        pra=pairs.pra,
        ids=pairs.ids,
        hard=pairs.hard,
        seed=seed,
        edges=build_edges(pairs, rng),
    )
