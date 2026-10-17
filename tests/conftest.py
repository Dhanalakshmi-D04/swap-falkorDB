"""Run the suite against its own FalkorDB graph so `make test` never replaces the live demo pool."""

from __future__ import annotations

import contextlib
import os

import pytest

TEST_GRAPH = "pool_test"
os.environ["SWAP_ACTIVE_GRAPH"] = TEST_GRAPH  # before any `swap` module reads the config


@pytest.fixture(scope="session", autouse=True)
def _drop_test_graph_at_end():
    yield
    with contextlib.suppress(Exception):  # FalkorDB may be down; those tests are skipped anyway
        from swap.graph import connect, drop_graph

        drop_graph(connect(), TEST_GRAPH)
