"""Run the suite against its own FalkorDB graph so `make test` never replaces the live demo pool."""

from __future__ import annotations

import contextlib
import os

import pytest

TEST_GRAPH = "pool_test"
os.environ["SWAP_ACTIVE_GRAPH"] = TEST_GRAPH  # before any `swap` module reads the config

# Never let a developer's `.env` switch the suite to a real model: an empty value is "set", so the
# dotenv loader (override=False) leaves it alone and the agent stays in deterministic tool mode.
for _name in (
    "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_PROVIDER", "LLM_API_VERSION",
    "VISION_ENDPOINT", "VISION_API_KEY", "VISION_MODEL", "VISION_PROVIDER", "VISION_API_VERSION",
):
    os.environ[_name] = ""


@pytest.fixture(scope="session", autouse=True)
def _drop_test_graph_at_end():
    yield
    with contextlib.suppress(Exception):  # FalkorDB may be down; those tests are skipped anyway
        from swap.graph import connect, drop_graph

        drop_graph(connect(), TEST_GRAPH)
