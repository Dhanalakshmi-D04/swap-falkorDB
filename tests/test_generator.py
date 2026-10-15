"""Generator tests: the pool rule, determinism, and the distribution of retained pairs."""

from __future__ import annotations

import random
from typing import Any

import numpy as np
import pytest

from swap.generator import (
    ABO_FREQ,
    ABO_TYPES,
    HARD_PRA,
    PRA_FREQ,
    PRA_LEVELS,
    can_give_abo,
    crossmatch_positive_probability,
    draw_pairs,
    generate_pool,
    pair_is_incompatible,
)

# The cited source's numbers, restated independently of the module
# (Roth, Sonmez, Unver, AER 97(3) 2007, Table 1).
ABO_RAW = {"O": 0.4814, "A": 0.3373, "B": 0.1428, "AB": 0.0385}
PRA_RAW = {0.05: 0.7019, 0.45: 0.2000, 0.90: 0.0981}
DONOR_ACCEPTS = {"O": {"O", "A", "B", "AB"}, "A": {"A", "AB"}, "B": {"B", "AB"}, "AB": {"AB"}}

SAMPLE = 20000


def compatible_probability(patient_abo: str) -> float:
    """P(a freshly drawn donor's ABO type is compatible with this patient)."""
    return sum(q for donor, q in ABO_RAW.items() if patient_abo in DONOR_ACCEPTS[donor])


def keep_probability(patient_abo: str, pra: float) -> float:
    """P(a drawn pair survives the pool rule), for one patient ABO and PRA level."""
    return 1 - compatible_probability(patient_abo) * (1 - pra)


def retained_distribution() -> dict[tuple[str, float], float]:
    """Joint distribution of (patient ABO, PRA) over retained pairs, from the raw draws."""
    joint = {
        (patient, pra): abo_q * pra_q * keep_probability(patient, pra)
        for patient, abo_q in ABO_RAW.items()
        for pra, pra_q in PRA_RAW.items()
    }
    total = sum(joint.values())
    return {key: value / total for key, value in joint.items()}


def retained_donor_distribution() -> dict[str, float]:
    """Donor ABO mix over retained pairs: a donor is dropped when it can serve the patient."""
    crossmatch_pass_rate = sum(q * (1 - pra) for pra, q in PRA_RAW.items())
    weights = {
        donor: q * (1 - crossmatch_pass_rate * sum(ABO_RAW[t] for t in DONOR_ACCEPTS[donor]))
        for donor, q in ABO_RAW.items()
    }
    total = sum(weights.values())
    return {donor: weight / total for donor, weight in weights.items()}


def expected_edge_density() -> float:
    """Share of ordered pair slots i != j that carry an edge, from theory alone."""
    joint = retained_distribution()
    donors = retained_donor_distribution()
    return sum(
        p_donor * q_pair * (1 - pra)
        for donor, p_donor in donors.items()
        for (patient, pra), q_pair in joint.items()
        if patient in DONOR_ACCEPTS[donor]
    )


def test_constants_match_the_cited_source() -> None:
    assert dict(zip(ABO_TYPES, ABO_FREQ, strict=True)) == ABO_RAW
    assert dict(zip(PRA_LEVELS, PRA_FREQ, strict=True)) == PRA_RAW
    assert crossmatch_positive_probability(HARD_PRA) == pytest.approx(0.90)
    assert can_give_abo("O", "AB")
    assert can_give_abo("A", "A")
    assert not can_give_abo("A", "O")
    assert not can_give_abo("AB", "A")


def test_pool_rule_keeps_only_incompatible_pairs() -> None:
    for patient in ABO_TYPES:
        for donor in ABO_TYPES:
            abo_ok = can_give_abo(donor, patient)
            assert pair_is_incompatible(patient, donor, crossmatch_positive=False) == (not abo_ok)
            assert pair_is_incompatible(patient, donor, crossmatch_positive=True)


def test_generation_is_deterministic_in_the_seed() -> None:
    assert generate_pool(120, 11) == generate_pool(120, 11)
    first, other = generate_pool(120, 11), generate_pool(120, 12)
    assert first.patient_abo != other.patient_abo
    assert first.edges != other.edges


def test_ids_are_consecutive_and_hard_flags_track_pra() -> None:
    pool = generate_pool(300, 3)
    assert pool.ids == list(range(300))
    assert pool.hard == [level == HARD_PRA for level in pool.pra]
    assert pool.hard_ids == [pid for pid, is_hard in zip(pool.ids, pool.hard, strict=True) if is_hard]
    assert pool.n == len(pool.patient_abo) == len(pool.donor_abo) == len(pool.pra)


def test_there_are_no_self_edges() -> None:
    pool = generate_pool(400, 4)
    assert all(i != j for i, j in pool.edges)


def test_every_stored_edge_is_abo_compatible() -> None:
    pool = generate_pool(400, 5)
    for i, j in pool.edges:
        assert can_give_abo(pool.donor_abo[i], pool.patient_abo[j])

    stored = set(pool.edges)
    for i in range(pool.n):
        for j in range(pool.n):
            if i != j and not can_give_abo(pool.donor_abo[i], pool.patient_abo[j]):
                assert (i, j) not in stored


def test_retained_abo_and_pra_frequencies_match_the_conditioned_distribution() -> None:
    pairs = draw_pairs(SAMPLE, np.random.default_rng(101))
    expected = retained_distribution()

    observed_abo = {t: pairs.patient_abo.count(t) / SAMPLE for t in ABO_TYPES}
    expected_abo = {
        t: sum(q for (patient, _), q in expected.items() if patient == t) for t in ABO_TYPES
    }
    for abo in ABO_TYPES:
        assert observed_abo[abo] == pytest.approx(expected_abo[abo], abs=0.015)

    observed_pra = {level: pairs.pra.count(level) / SAMPLE for level in PRA_LEVELS}
    expected_pra = {
        level: sum(q for (_, pra), q in expected.items() if pra == level) for level in PRA_LEVELS
    }
    for level in PRA_LEVELS:
        assert observed_pra[level] == pytest.approx(expected_pra[level], abs=0.015)

    observed_joint = {
        (patient, pra): sum(
            1
            for p, v in zip(pairs.patient_abo, pairs.pra, strict=True)
            if (p, v) == (patient, pra)
        )
        / SAMPLE
        for patient in ABO_TYPES
        for pra in PRA_LEVELS
    }
    for cell, q in expected.items():
        if q >= 0.02:
            assert observed_joint[cell] == pytest.approx(q, abs=0.01), cell


def test_retained_pairs_are_never_compatible_and_crossmatch_clean_ones() -> None:
    # The rule drops compatible pairs whose crossmatch passes, so a retained pair either has an
    # incompatible own donor or a positive crossmatch: the share with a compatible own donor is
    # P(compatible and crossmatch positive) / P(retained), about 27 percent, not about 74 percent.
    kept = sum(
        abo_q * pra_q * keep_probability(patient, pra)
        for patient, abo_q in ABO_RAW.items()
        for pra, pra_q in PRA_RAW.items()
    )
    own_donor_compatible = sum(
        abo_q * pra_q * compatible_probability(patient) * pra
        for patient, abo_q in ABO_RAW.items()
        for pra, pra_q in PRA_RAW.items()
    )
    expected_share = own_donor_compatible / kept
    assert expected_share == pytest.approx(0.271, abs=0.005)

    pairs = draw_pairs(SAMPLE, np.random.default_rng(202))
    observed_share = (
        sum(
            1
            for patient, donor in zip(pairs.patient_abo, pairs.donor_abo, strict=True)
            if can_give_abo(donor, patient)
        )
        / SAMPLE
    )
    assert observed_share == pytest.approx(expected_share, abs=0.01)


def _weighted_pick(rnd: random.Random, weights: dict[Any, float]) -> Any:
    threshold, running = rnd.random(), 0.0
    for key, weight in weights.items():
        running += weight
        if threshold <= running:
            return key
    raise AssertionError("weights must sum to at least 1")


def _independent_pool(n: int, seed: int) -> tuple[int, int]:
    """Independent scalar re-implementation of the same rules, for a density cross-check."""
    rnd = random.Random(seed)
    patients: list[tuple[str, float]] = []
    donors: list[str] = []
    while len(patients) < n:
        patient = _weighted_pick(rnd, ABO_RAW)
        donor = _weighted_pick(rnd, ABO_RAW)
        pra = _weighted_pick(rnd, PRA_RAW)
        if patient in DONOR_ACCEPTS[donor] and rnd.random() >= pra:
            continue
        patients.append((patient, pra))
        donors.append(donor)

    edges = 0
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            patient, pra = patients[j]
            if patient not in DONOR_ACCEPTS[donors[i]]:
                continue
            if rnd.random() >= pra:
                edges += 1
    return n, edges


def test_edge_density_agrees_with_theory_and_with_an_independent_implementation() -> None:
    n = 800
    pool = generate_pool(n, seed=77)
    density = len(pool.edges) / (pool.n * (pool.n - 1))
    assert density == pytest.approx(expected_edge_density(), abs=0.02)

    ref_n, ref_edges = _independent_pool(n, seed=78)
    reference = ref_edges / (ref_n * (ref_n - 1))
    assert density == pytest.approx(reference, abs=0.02)


def test_retained_donor_abo_mix_follows_the_rule() -> None:
    pairs = draw_pairs(SAMPLE, np.random.default_rng(303))
    expected = retained_donor_distribution()
    observed = {t: pairs.donor_abo.count(t) / SAMPLE for t in ABO_TYPES}
    for abo in ABO_TYPES:
        assert observed[abo] == pytest.approx(expected[abo], abs=0.015)
