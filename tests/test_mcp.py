"""M7 MCP smoke test: list the fixed catalogue through the SDK client and call it in-process.

Uses the SDK's in-memory `Client(server)`, so no stdio subprocess is spawned. Needs FalkorDB:
skipped cleanly without it.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.client import Client
from redis.exceptions import RedisError

from swap.config import settings
from swap.envelope import Outcome
from swap.graph import connect
from swap.mcp_server import ACTIVE_GRAPH, mcp

CATALOGUE = {
    "generate_pool",
    "find_cycles",
    "flow_bound",
    "best_set",
    "true_cycle_optimum",
    "explain_unmatched",
    "what_if",
    "draft_plan",
    "get_plan",
    "list_plans",
    "failure_impact",
    "draft_repair",
    "list_crossmatches",
}
VALID = {outcome.value for outcome in Outcome}


@pytest.fixture(scope="module", autouse=True)
def _falkordb() -> None:
    cfg = settings()
    try:
        connect(cfg).list_graphs()
    except RedisError as exc:
        pytest.skip(f"FalkorDB unreachable at {cfg.where()} ({exc}); start it with `make up`")


async def _exercise() -> None:
    async with Client(mcp) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == CATALOGUE
        for tool in listed.tools:
            assert tool.input_schema["type"] == "object", tool.name
            assert tool.description, tool.name

        generated = (await client.call_tool("generate_pool", {"n": 40, "seed": 7})).structured_content
        assert generated is not None
        assert generated["outcome"] == Outcome.ANSWERED_WITH_PATH.value
        assert generated["result"]["graph"] == ACTIVE_GRAPH
        assert generated["result"]["summary"]["n"] == 40

        found = (await client.call_tool("find_cycles", {"max_len": 3})).structured_content
        assert found is not None
        assert set(found) == {"outcome", "result", "path", "trace"}
        assert set(found["trace"]) == {"tool", "args", "ms", "rows"}
        assert found["trace"]["tool"] == "find_cycles"
        assert found["outcome"] in VALID
        assert found["result"]["counts"]["2"] >= 0

        for name, arguments in (
            ("flow_bound", {}),
            ("best_set", {"max_len": 3}),
            ("true_cycle_optimum", {}),
            ("explain_unmatched", {"pair_id": 0}),
            ("what_if", {"kind": "remove_pair", "pair_id": 0}),
            ("list_crossmatches", {}),
            ("failure_impact", {"crossmatch_id": 1}),
        ):
            env = (await client.call_tool(name, arguments)).structured_content
            assert env is not None, name
            assert env["outcome"] in VALID, name
            assert set(env["trace"]) == {"tool", "args", "ms", "rows"}, name

        unknown = (await client.call_tool("explain_unmatched", {"pair_id": 10**9})).structured_content
        assert unknown is not None
        assert unknown["outcome"] == Outcome.UNKNOWN_ENTITY.value
        assert unknown["path"] == []


def test_mcp_catalogue_lists_and_answers() -> None:
    asyncio.run(_exercise())
