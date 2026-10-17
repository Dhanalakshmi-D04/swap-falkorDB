"""When a crossmatch fails: the graph reacts, the agent drafts a repair, a person approves it.

Needs FalkorDB (skipped cleanly without it). Pinned here: a positive result kills exactly the stored
swaps that used that arc (the live swaps then equal what the pattern match finds on the changed
graph), removes the arc, breaks the approved swap and releases its pairs, finds the 2-way swap still
alive inside a broken 3-way swap, and the repair re-matches stranded patients from live swaps only;
recording a result is human-only (no agent or MCP tool can do it); unknown ids and missing links
answer `unknown_entity` / `not_in_graph`.
"""

from __future__ import annotations

import pytest
from falkordb import FalkorDB
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from swap.agent import TOOL_ORDER, ToolCatalogue, route, run_tool_mode
from swap.config import settings
from swap.generator import generate_pool
from swap.graph import connect, load_pool
from swap.mcp_server import mcp
from swap.tools import PlanStateError, SwapTools

GRAPH = "swap_failure"


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
    load_pool(generate_pool(200, 1000), GRAPH, db=db)
    return SwapTools(GRAPH, db=db)


def _approved_three_way_with_back_arc(tools: SwapTools) -> tuple[int, dict, dict]:
    """Approve a plan and return it, one of its 3-way swaps with a back-arc, and an arc to fail.

    The failed arc is chosen so that the back-arc's 2-way swap survives: fail an arc that is not
    part of that 2-way swap.
    """
    plan = tools.draft_plan(3)["result"]
    plan_id = plan["plan"]["id"]
    tools.decide_plan(plan_id, "approve", "Dr Test")
    keys = [swap["key"] for swap in plan["swaps"] if swap["size"] == 3]
    rows = tools.graph.query(
        "UNWIND $keys AS k MATCH (c:Cycle {key: k}) WHERE c.back_arcs > 0 RETURN c.key, c.members",
        {"keys": keys},
    ).result_set
    assert rows, "the bench pool should approve at least one 3-way swap with a back-arc"
    key, members = rows[0]
    arcs = list(zip(members, members[1:] + members[:1], strict=True))
    edges = set(map(tuple, tools.graph.query(
        "MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair) RETURN a.id, b.id").result_set))
    for a, b in arcs:
        third = next(m for m in members if m not in (a, b))
        survivors = [(x, y) for x, y in ((a, third), (b, third)) if (y, x) in edges and (x, y) in edges]
        if survivors:
            return plan_id, {"key": key, "members": members}, {"a": a, "b": b, "survivor": survivors[0]}
    pytest.skip("no arc in this swap leaves an embedded 2-way swap alive")


def _live_keys(tools: SwapTools) -> set[str]:
    return {r[0] for r in tools.graph.query("MATCH (c:Cycle) WHERE c.live RETURN c.key").result_set}


def test_a_positive_crossmatch_kills_exactly_the_swaps_that_used_the_arc(tools: SwapTools) -> None:
    before = _live_keys(tools)
    a, b = tools.graph.query(
        "MATCH (c:Cycle {len: 3}) RETURN c.members[0], c.members[1] ORDER BY c.key LIMIT 1"
    ).result_set[0]
    env = tools.record_crossmatch(a, b, "positive", "Lab A")
    assert env["outcome"] == "answered_with_path"
    after = _live_keys(tools)

    def uses_arc(key: str) -> bool:
        members = [int(m) for m in key.split(">")]
        return (a, b) in zip(members, members[1:] + members[:1], strict=True)

    killed = before - after
    assert killed == {key for key in before if uses_arc(key)}
    assert len(killed) == env["result"]["effects"]["swaps_killed"]
    # the stored live swaps equal what the pattern match now finds in the changed graph
    found = tools.find_cycles(3)["result"]["cycles"]
    assert sorted(after) == sorted(">".join(map(str, c)) for c in found)
    assert not tools.graph.query(
        "MATCH (:Pair {id: $a})-[x:CAN_GIVE]->(:Pair {id: $b}) RETURN x", {"a": a, "b": b}
    ).result_set
    linked = tools.graph.query(
        "MATCH (:Crossmatch {id: $t})-[:BROKE]->(c:Cycle) RETURN count(c)",
        {"t": env["result"]["crossmatch"]["id"]},
    ).result_set[0][0]
    assert linked == len(killed)


def test_the_loop_breaks_releases_rescues_and_repairs(tools: SwapTools) -> None:
    plan_id, swap, fail = _approved_three_way_with_back_arc(tools)
    env = tools.record_crossmatch(fail["a"], fail["b"], "positive", "Lab A")
    impact = env["result"]["impact"]
    assert [b["key"] for b in impact["broken_planned_swaps"]] == [swap["key"]]
    assert sorted(p["id"] for p in impact["stranded"]) == sorted(swap["members"])
    assert sorted(impact["still_waiting"]) == sorted(swap["members"])
    assert set(tools.committed_pair_ids()).isdisjoint(swap["members"])
    survivor = ">".join(map(str, sorted(fail["survivor"])))
    assert survivor in [r["rescue"] for r in impact["rescue"]]

    packet = tools.get_plan(plan_id)["result"]
    broken = [s for s in packet["swaps"] if not s["live"]]
    assert [s["key"] for s in broken] == [swap["key"]]
    assert broken[0]["broken_by"] == [env["result"]["crossmatch"]["id"]]

    repair = tools.draft_repair(env["result"]["crossmatch"]["id"])
    assert repair["outcome"] == "answered_with_path"
    found = repair["result"]
    assert found["plan"]["kind"] == "repair" and found["guard"]["ok"]
    assert set(found["repair"]["rematched"]) >= set(fail["survivor"])
    links = tools.graph.query(
        "MATCH (r:Plan {id: $r})-[:REPAIRS]->(old:Plan), (r)-[:AFTER]->(t:Crossmatch) RETURN old.id, t.id",
        {"r": found["plan"]["id"]},
    ).result_set
    assert links == [[plan_id, env["result"]["crossmatch"]["id"]]]

    tools.decide_plan(found["plan"]["id"], "approve", "Dr Test")
    after = tools.failure_impact(env["result"]["crossmatch"]["id"])["result"]
    assert {p["id"] for p in after["stranded"] if p["now_committed_to"] == found["plan"]["id"]} == set(
        found["repair"]["rematched"]
    )


def test_a_failure_makes_an_overlapping_draft_stale(tools: SwapTools) -> None:
    draft = tools.draft_plan(3)["result"]
    swap = next(s for s in draft["swaps"] if s["size"] == 3)
    step = swap["steps"][0]
    tools.record_crossmatch(step["donor_pair"], step["patient_pair"], "positive", "Lab A")
    with pytest.raises(PlanStateError, match="stale"):
        tools.decide_plan(draft["plan"]["id"], "approve", "Dr Test")


def test_a_negative_result_is_recorded_and_changes_nothing(tools: SwapTools) -> None:
    before = _live_keys(tools)
    a, b = tools.graph.query("MATCH (c:Cycle {len: 2}) RETURN c.members[0], c.members[1] LIMIT 1").result_set[0]
    env = tools.record_crossmatch(a, b, "negative", "Lab A")
    assert env["result"]["effects"] == {} and env["result"]["impact"] is None
    assert _live_keys(tools) == before
    assert tools.list_crossmatches()["result"]["crossmatches"][0]["result"] == "negative"


def test_unknown_ids_and_missing_links(tools: SwapTools) -> None:
    assert tools.record_crossmatch(99999, 1, "positive", "Lab A")["outcome"] == "unknown_entity"
    assert tools.failure_impact(424242)["outcome"] == "unknown_entity"
    assert tools.draft_repair(424242)["outcome"] == "unknown_entity"
    a, b = tools.graph.query(
        "MATCH (a:Pair), (b:Pair) WHERE a.id <> b.id AND NOT (a)-[:CAN_GIVE]->(b) "
        "RETURN a.id, b.id LIMIT 1"
    ).result_set[0]
    env = tools.record_crossmatch(a, b, "positive", "Lab A")
    assert env["outcome"] == "not_in_graph"
    assert tools.list_crossmatches()["outcome"] == "not_in_graph"
    with pytest.raises(ValueError):
        tools.record_crossmatch(a, b, "maybe", "Lab A")
    with pytest.raises(ValueError):
        tools.record_crossmatch(a, b, "positive", "  ")


def test_a_failure_outside_any_plan_strands_nobody(tools: SwapTools) -> None:
    a, b = tools.graph.query("MATCH (c:Cycle {len: 2}) RETURN c.members[0], c.members[1] LIMIT 1").result_set[0]
    tid = tools.record_crossmatch(a, b, "positive", "Lab A")["result"]["crossmatch"]["id"]
    impact = tools.failure_impact(tid)["result"]
    assert impact["broken_planned_swaps"] == [] and impact["stranded"] == []
    assert tools.draft_repair(tid)["outcome"] == "not_in_graph"


def test_recording_a_result_is_human_only() -> None:
    import asyncio

    from mcp.client import Client

    async def names() -> set[str]:
        async with Client(mcp) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    exposed = asyncio.run(names())
    assert {"failure_impact", "draft_repair", "list_crossmatches"} <= exposed
    assert not any("record" in name for name in exposed)
    assert "record_crossmatch" not in TOOL_ORDER
    assert not hasattr(ToolCatalogue, "record_crossmatch")
    assert {"failure_impact", "draft_repair"} <= set(TOOL_ORDER)


def test_tool_mode_routes_failure_questions(tools: SwapTools) -> None:
    assert route("what did the failed crossmatch break?") == [("failure_impact", {})]
    assert route("draft a repair for the stranded patients") == [("draft_repair", {"max_len": 3})]
    _, _, fail = _approved_three_way_with_back_arc(tools)
    tools.record_crossmatch(fail["a"], fail["b"], "positive", "Lab A")
    answer = run_tool_mode("what did the failed crossmatch break?", ToolCatalogue(tools))
    assert answer["rejected_ids"] == [] and "killed" in answer["answer"]
    repaired = run_tool_mode("draft a repair", ToolCatalogue(tools))
    assert "drafted repair plan" in repaired["answer"]


def test_the_api_records_reacts_and_repairs(db: FalkorDB, monkeypatch: pytest.MonkeyPatch) -> None:
    from swap import api

    monkeypatch.setattr(api, "ACTIVE_GRAPH", GRAPH)
    load_pool(generate_pool(200, 1000), GRAPH, db=db)
    tools = SwapTools(GRAPH, db=db)
    _, _, fail = _approved_three_way_with_back_arc(tools)
    client = TestClient(api.app)
    body = {"donor_pair": fail["a"], "patient_pair": fail["b"], "result": "positive", "recorded_by": "Lab A"}
    recorded = client.post("/crossmatch", json=body)
    assert recorded.status_code == 200 and recorded.json()["outcome"] == "answered_with_path"
    tid = recorded.json()["result"]["crossmatch"]["id"]
    assert client.get("/crossmatches").json()["result"]["crossmatches"][0]["id"] == tid
    assert client.get(f"/crossmatches/{tid}/impact").json()["result"]["stranded"]
    repair = client.post(f"/crossmatches/{tid}/repair", json={"max_len": 3}).json()
    assert repair["result"]["plan"]["kind"] == "repair"
    assert client.post("/crossmatch", json={**body, "result": "maybe"}).status_code == 422
    assert client.get("/crossmatches/999/impact").json()["outcome"] == "unknown_entity"
