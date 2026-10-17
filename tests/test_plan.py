"""The action: a drafted plan is written to the graph, only a person can decide it.

Needs FalkorDB (skipped cleanly without it). Pinned here: the plan read back out of the graph
equals the best set it was drafted from; a draft is not approved until a human decides; a decision
is final; approved plans commit their pairs and the next draft skips them; unknown plans answer
`unknown_entity`; and no agent tool or MCP tool can approve (the gate is structural).
"""

from __future__ import annotations

import pytest
from falkordb import FalkorDB
from redis.exceptions import RedisError

from swap.agent import TOOL_ORDER, ToolCatalogue, route
from swap.config import settings
from swap.envelope import Outcome
from swap.generator import generate_pool
from swap.graph import connect, load_pool
from swap.mcp_server import mcp
from swap.plan import render_packet_markdown
from swap.tools import PlanStateError, SwapTools

GRAPH = "swap_plan"


@pytest.fixture(scope="module")
def db() -> FalkorDB:
    cfg = settings()
    try:
        handle = connect(cfg)
        handle.list_graphs()
    except RedisError as exc:
        pytest.skip(f"FalkorDB unreachable at {cfg.where()} ({exc}); start it with `make up`")
    return handle


@pytest.fixture()
def tools(db: FalkorDB) -> SwapTools:
    """A fresh pool per test, so plans from one test never leak into another."""
    load_pool(generate_pool(120, 7), GRAPH, db=db)
    return SwapTools(GRAPH, db=db)


def test_draft_is_recorded_in_the_graph_and_equals_the_best_set(tools: SwapTools) -> None:
    best = tools.best_set(3)["result"]
    env = tools.draft_plan(3)

    assert env["outcome"] == Outcome.ANSWERED_WITH_PATH.value
    packet = env["result"]
    assert packet["plan"]["status"] == "draft"
    assert packet["requires_human_approval"] is True
    assert packet["plan"]["matched"] == best["matched"]
    assert [swap["pairs"] for swap in packet["swaps"]] == best["cycles"]
    assert env["path"] == best["cycles"]
    assert packet["summary"]["pairs_in_pool"] == 120
    assert env["trace"]["tool"] == "draft_plan"

    # what the API would show later comes from the graph, not from memory
    again = tools.get_plan(packet["plan"]["id"])["result"]
    assert again["swaps"] == packet["swaps"]
    assert again["plan"]["status"] == "draft"


def test_every_step_is_a_real_donor_to_patient_link(tools: SwapTools) -> None:
    packet = tools.draft_plan(3)["result"]
    links = {tuple(row) for row in tools.graph.query(
        "MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair) RETURN a.id, b.id").result_set}
    for swap in packet["swaps"]:
        assert len(set(swap["pairs"])) == swap["size"]
        for step in swap["steps"]:
            assert (step["donor_pair"], step["patient_pair"]) in links


def test_a_person_decides_once_and_it_is_recorded(tools: SwapTools) -> None:
    plan_id = tools.draft_plan(3)["result"]["plan"]["id"]
    env = tools.decide_plan(plan_id, "approve", "  Dr  Rao ")

    plan = env["result"]["plan"]
    assert plan["status"] == "approved"
    assert plan["decided_by"] == "Dr Rao"
    assert plan["decided_at"]
    assert env["result"]["requires_human_approval"] is False
    with pytest.raises(PlanStateError):
        tools.decide_plan(plan_id, "reject", "someone else")
    assert tools.get_plan(plan_id)["result"]["plan"]["status"] == "approved"


def test_bad_decisions_are_refused(tools: SwapTools) -> None:
    plan_id = tools.draft_plan(3)["result"]["plan"]["id"]
    with pytest.raises(ValueError, match="decision"):
        tools.decide_plan(plan_id, "maybe", "Dr Rao")
    with pytest.raises(ValueError, match="name"):
        tools.decide_plan(plan_id, "approve", "   ")
    assert tools.get_plan(plan_id)["result"]["plan"]["status"] == "draft"


def test_approved_plans_commit_pairs_and_the_next_draft_skips_them(tools: SwapTools) -> None:
    first = tools.draft_plan(2)["result"]
    assert tools.committed_pair_ids() == []
    tools.decide_plan(first["plan"]["id"], "approve", "Dr Rao")

    committed = set(tools.committed_pair_ids())
    assert committed == {pair for swap in first["swaps"] for pair in swap["pairs"]}

    # approve the safe 2-way plan, then plan rings of three among everyone not yet committed
    second = tools.draft_plan(3)
    assert second["outcome"] == Outcome.ANSWERED_WITH_PATH.value
    assert second["result"]["summary"]["excluded_committed"] == len(committed)
    planned = {pair for swap in second["result"]["swaps"] for pair in swap["pairs"]}
    assert planned and planned.isdisjoint(committed)
    assert second["result"]["plan"]["id"] == first["plan"]["id"] + 1


def test_once_the_best_set_is_approved_nothing_is_left_to_plan(tools: SwapTools) -> None:
    plan_id = tools.draft_plan(3)["result"]["plan"]["id"]
    tools.decide_plan(plan_id, "approve", "Dr Rao")
    env = tools.draft_plan(3)
    # the best set is maximal, so no swap fits among the remaining pairs: the graph says so
    assert env["outcome"] == Outcome.NOT_IN_GRAPH.value
    assert env["result"]["plan"] is None
    assert [p["id"] for p in tools.list_plans()["result"]["plans"]] == [plan_id]


def test_a_rejected_plan_commits_nothing(tools: SwapTools) -> None:
    plan_id = tools.draft_plan(3)["result"]["plan"]["id"]
    tools.decide_plan(plan_id, "reject", "Dr Rao")
    assert tools.committed_pair_ids() == []
    again = tools.draft_plan(3)["result"]
    assert again["summary"]["excluded_committed"] == 0


def test_unknown_plan_is_unknown_entity(tools: SwapTools) -> None:
    for call in (lambda: tools.get_plan(9999), lambda: tools.decide_plan(9999, "approve", "x")):
        env = call()
        assert env["outcome"] == Outcome.UNKNOWN_ENTITY.value
        assert env["path"] == []
    assert tools.get_plan("nope")["outcome"] == Outcome.UNKNOWN_ENTITY.value


def test_nothing_to_plan_is_not_in_graph_and_writes_no_plan(db: FalkorDB) -> None:
    load_pool(generate_pool(0, 1), GRAPH, db=db)
    tools = SwapTools(GRAPH, db=db)
    env = tools.draft_plan(3)
    assert env["outcome"] == Outcome.NOT_IN_GRAPH.value
    assert env["result"]["plan"] is None
    assert tools.list_plans()["outcome"] == Outcome.NOT_IN_GRAPH.value


def test_list_plans_reports_status_and_committed(tools: SwapTools) -> None:
    first = tools.draft_plan(2)["result"]["plan"]["id"]
    tools.decide_plan(first, "approve", "Dr Rao")
    tools.draft_plan(3)
    listed = tools.list_plans()["result"]
    assert [(plan["id"], plan["status"]) for plan in listed["plans"]] == [
        (first, "approved"),
        (first + 1, "draft"),
    ]
    assert listed["committed"] == tools.committed_pair_ids()


def test_the_markdown_packet_carries_the_notice_and_the_approval_state(tools: SwapTools) -> None:
    plan_id = tools.draft_plan(3)["result"]["plan"]["id"]
    draft = render_packet_markdown(tools.get_plan(plan_id)["result"])
    assert "DRAFT" in draft and "Not approved" in draft
    assert "Synthetic patients only" in draft and "not medical advice" in draft.lower()

    tools.decide_plan(plan_id, "approve", "Dr Rao")
    approved = render_packet_markdown(tools.get_plan(plan_id)["result"])
    assert "APPROVED" in approved and "**Approved** by Dr Rao" in approved


def test_the_agent_can_draft_but_has_no_way_to_decide() -> None:
    assert "draft_plan" in TOOL_ORDER
    assert not any("decide" in name or "approve" in name for name in TOOL_ORDER)
    assert not hasattr(ToolCatalogue, "decide_plan")
    assert route("draft a plan for sign-off") == [("draft_plan", {"max_len": 3})]


def test_the_mcp_server_can_draft_but_has_no_way_to_decide() -> None:
    import asyncio

    from mcp.client import Client

    async def names() -> set[str]:
        async with Client(mcp) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    exposed = asyncio.run(names())
    assert {"draft_plan", "get_plan", "list_plans"} <= exposed
    assert not any("decide" in name or "approve" in name for name in exposed)


def test_a_solver_that_gives_up_is_an_error_not_an_empty_answer(
    tools: SwapTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    import swap.tools as tools_module
    from swap.solve import PackResult
    from swap.tools import SolverError

    monkeypatch.setattr(
        tools_module, "pack_cycles", lambda *a, **k: PackResult([], 0, 0.0, "time limit", False)
    )
    with pytest.raises(SolverError, match="could not finish"):
        tools.best_set(3)
    with pytest.raises(SolverError):
        tools.draft_plan(3)
    assert tools.list_plans()["outcome"] == Outcome.NOT_IN_GRAPH.value  # nothing half-written


def test_a_set_found_before_the_time_limit_is_labelled_as_unproven(
    tools: SwapTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    import swap.tools as tools_module
    from swap.solve import PackResult

    real = tools_module.pack_cycles

    def unproven(*args, **kwargs):
        done = real(*args, **kwargs)
        return PackResult(done.chosen, done.covered, done.objective, "time limit", False, done.indices)

    monkeypatch.setattr(tools_module, "pack_cycles", unproven)
    result = tools.best_set(3)["result"]
    assert result["cycles"] and "not proven" in result["note"]
