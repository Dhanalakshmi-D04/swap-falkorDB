"""M11 verify tests: the aggregation and reporting helpers, without touching FalkorDB.

`verify.run` needs the graph, but its mean/sd, table, check-table and ties/negatives helpers are
pure functions over `Row` objects, and those are what decide PASS/FAIL and what the README quotes.
"""

from __future__ import annotations

import pytest

from swap import verify
from swap.verify import Row


def _row(n: int, seed: int, two: int, three: int, cover: int, bound: int) -> Row:
    return Row(
        n=n,
        seed=seed,
        two=two,
        three=three,
        cover=cover,
        bound=bound,
        cycles_agree={2: True, 3: True},
        bound_agrees=True,
        ordered=two <= three <= cover <= bound,
    )


def test_defaults_documented_in_the_readme() -> None:
    assert verify.SIZES == (100, 200, 300)
    assert verify.DEFAULT_SEEDS == 5
    assert verify.SEED_START == 1000


def test_mean_sd_matches_numpy_and_handles_one_seed() -> None:
    mean, sd = verify._mean_sd([1, 2, 3, 4])
    assert mean == pytest.approx(2.5)
    assert sd == pytest.approx(1.2909944, rel=1e-6)
    assert verify._mean_sd([7]) == (7.0, 0.0)


def test_table_rows_carry_n_k_and_the_gain_columns() -> None:
    rows = {100: [_row(100, 1000, 45, 56, 57, 62), _row(100, 1001, 46, 57, 58, 63)]}
    table = "\n".join(verify._table(rows))
    assert "| 100 | 2 |" in table  # N and K
    assert "45.5 ± 0.7" in table  # mean ± sd of 2-way over the two seeds
    assert "+11.0 pts" in table  # 3-way gain
    assert "11.0%" in table  # as a percent of N
    assert "| 92% |" in table  # ≤3 share of the unbounded gain (11 of 12, rounded)


def test_check_table_passes_when_every_flag_holds() -> None:
    rows = {100: [_row(100, 1000, 45, 56, 57, 62)]}
    lines, ok = verify._check_table(rows)
    assert ok is True
    assert all("PASS" in line for line in lines[2:])
    assert verify._check_table({"100": []})[0]  # no crash on an empty group


def test_check_table_fails_when_a_flag_breaks() -> None:
    broken = Row(
        n=100,
        seed=1000,
        two=45,
        three=56,
        cover=55,  # cover < three violates the ordering invariant
        bound=62,
        cycles_agree={2: True, 3: False},
        bound_agrees=False,
        ordered=False,
    )
    lines, ok = verify._check_table({100: [broken]})
    assert ok is False
    assert any("FAIL" in line for line in lines[2:])


def test_negatives_report_ties_and_no_ties() -> None:
    tied = {100: [_row(100, 1000, 45, 57, 57, 62), _row(100, 1001, 45, 45, 60, 62)]}
    text = "\n".join(verify._negatives(tied))
    assert "adds no patient over 2-way" in text
    assert "1 of 2" in text  # the second seed: 3-way adds nothing
    assert "1 of 2" in text  # (first line) 3-way reaches the cover on the first seed too

    no_ties = {200: [_row(200, 1000, 90, 110, 112, 120)]}
    clean = "\n".join(verify._negatives(no_ties))
    assert "0 of 1" in clean


def test_negatives_lists_at_most_a_handful_of_seeds() -> None:
    rows = {100: [_row(100, 1000 + i, 45, 45, 60, 62) for i in range(40)]}
    text = "\n".join(verify._negatives(rows))
    assert "40 of 40" in text
    assert "(+32 more)" in text  # the seed list is capped, not printed 40 times
