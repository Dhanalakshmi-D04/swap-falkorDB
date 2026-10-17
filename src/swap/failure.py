"""When a crossmatch fails: the graph finds what broke, who is stranded and what can still be saved.

A crossmatch is the lab test run before a transplant; most planned exchange transplants never happen,
and a positive crossmatch is a common reason (Dickerson, Procaccia and Sandholm, "Failure-Aware
Kidney Exchange", Management Science 2019). Here a person records the result (`record_crossmatch`,
HUMAN-ONLY: not in the agent catalogue or the MCP server). On a positive result FalkorDB, in Cypher:

  * kills every stored swap that relied on that donor -> patient arc and links the test to each
    (`(:Crossmatch)-[:BROKE]->(:Cycle)`), then removes the arc and its flow arc;
  * releases the pairs of any approved swap it broke (they are stranded) and makes the swaps
    through them available again where nothing else holds them;
  * finds the 2-way swap still alive inside a broken 3-way swap (a back-arc).

The agent can then read the impact (`failure_impact`) and draft a repair (`draft_repair`) over the
swaps still alive, weighted to re-match the stranded patients first; a person approves it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from swap.envelope import Outcome, Timer, UnknownEntity, build_trace, envelope, resolve_pair
from swap.solve import pack_cycles

if TYPE_CHECKING:
    from swap.tools import SwapTools

RESULTS = ("positive", "negative")
STRANDED_WEIGHT = 10.0  # a stranded patient counts ten times: re-match them first, then the rest

ARC_EXISTS = (
    "MATCH (a:Pair {id: $a})-[x:CAN_GIVE]->(b:Pair {id: $b}) RETURN count(x)"
)
NEXT_CROSSMATCH_ID = "MATCH (t:Crossmatch) RETURN coalesce(max(t.id), 0) + 1"
RECORD_CROSSMATCH = (
    "MATCH (a:Pair {id: $a})-[x:CAN_GIVE]->(b:Pair {id: $b}) "
    "CREATE (t:Crossmatch {id: $tid, donor_pair: $a, patient_pair: $b, result: $result, "
    "recorded_by: $by, recorded_at: $at}) "
    "CREATE (t)-[:DONOR]->(a), (t)-[:PATIENT]->(b) RETURN t.id"
)
MARK_ARC_NEGATIVE = (
    "MATCH (a:Pair {id: $a})-[x:CAN_GIVE]->(b:Pair {id: $b}) SET x.crossmatch = 'negative', "
    "x.tested = $tid RETURN count(x)"
)
# Every swap in which pair a's donor gives to pair b's patient: b sits right after a in the ring.
BREAK_CYCLES = (
    "MATCH (t:Crossmatch {id: $tid}) "
    "MATCH (a:Pair {id: $a})<-[h1:HAS]-(c:Cycle)-[h2:HAS]->(b:Pair {id: $b}) "
    "WHERE c.live AND h2.pos = (h1.pos + 1) % c.len "
    "SET c.live = false CREATE (t)-[:BROKE]->(c) RETURN count(c)"
)
DROP_ARC = "MATCH (a:Pair {id: $a})-[x:CAN_GIVE]->(b:Pair {id: $b}) DELETE x RETURN count(*)"
DROP_FLOW_ARC = "MATCH (d:Donor {id: $a})-[f:F]->(p:Patient {id: $b}) DELETE f RETURN count(*)"
RELEASE_STRANDED = (
    "MATCH (t:Crossmatch {id: $tid})-[:BROKE]->(c:Cycle)<-[:SELECTS]-(pl:Plan {status: 'approved'}) "
    "MATCH (c)-[:HAS]->(p:Pair) WHERE p.committed = pl.id SET p.committed = null RETURN collect(p.id)"
)
RETAKE = (
    "MATCH (c:Cycle) WHERE any(m IN c.members WHERE m IN $ids) UNWIND c.members AS m "
    "MATCH (p:Pair {id: m}) WITH c, sum(CASE WHEN p.committed IS NULL THEN 0 ELSE 1 END) AS held "
    "SET c.taken = held > 0 RETURN count(c), sum(CASE WHEN held = 0 THEN 1 ELSE 0 END)"
)

READ_CROSSMATCH = (
    "MATCH (t:Crossmatch {id: $tid}) "
    "RETURN t.id, t.donor_pair, t.patient_pair, t.result, t.recorded_by, t.recorded_at"
)
LIST_CROSSMATCHES = (
    "MATCH (t:Crossmatch) OPTIONAL MATCH (t)-[:BROKE]->(c:Cycle) "
    "RETURN t.id, t.donor_pair, t.patient_pair, t.result, t.recorded_by, t.recorded_at, count(c) "
    "ORDER BY t.id"
)
SWAPS_KILLED = (
    "MATCH (t:Crossmatch {id: $tid})-[:BROKE]->(c:Cycle) "
    "RETURN count(c), sum(CASE WHEN c.len = 2 THEN 1 ELSE 0 END)"
)
BROKEN_PLANNED = (
    "MATCH (t:Crossmatch {id: $tid})-[:BROKE]->(c:Cycle)<-[:SELECTS]-(pl:Plan) "
    "RETURN pl.id, pl.status, pl.kind, c.key, c.members ORDER BY pl.id, c.key"
)
STRANDED = (
    "MATCH (t:Crossmatch {id: $tid})-[:BROKE]->(c:Cycle)<-[:SELECTS]-(:Plan {status: 'approved'}) "
    "UNWIND c.members AS m MATCH (p:Pair {id: m}) "
    "RETURN DISTINCT p.id, p.patient_abo, p.donor_abo, p.pra, p.hard, p.committed ORDER BY p.id"
)
# The 2-way swap still alive inside a broken 3-way swap: a reverse arc between two of its pairs.
RESCUE_EMBEDDED_2WAY = (
    "MATCH (t:Crossmatch {id: $tid})-[:BROKE]->(c:Cycle {len: 3})<-[:SELECTS]-(:Plan {status: 'approved'}) "
    "MATCH (c)-[:HAS]->(x:Pair)<-[:HAS]-(r:Cycle {len: 2})-[:HAS]->(y:Pair)<-[:HAS]-(c) "
    "WHERE x.id < y.id AND r.live AND NOT r.taken RETURN c.key, r.key, r.members ORDER BY r.key"
)
OPTIONS_OF_STRANDED = (
    "UNWIND $ids AS s MATCH (:Pair {id: s})<-[:HAS]-(c:Cycle) WHERE c.live AND NOT c.taken "
    "RETURN s, count(c) ORDER BY s"
)
REPAIRS_AFTER = (
    "MATCH (r:Plan)-[:AFTER]->(:Crossmatch {id: $tid}) RETURN r.id, r.status ORDER BY r.id"
)
REPAIR_CANDIDATES = (
    "MATCH (c:Cycle) WHERE c.live AND NOT c.taken AND c.len <= $max_len "
    "AND any(m IN c.members WHERE m IN $ids) "
    "RETURN c.key, c.members, c.hard, c.back_arcs ORDER BY c.len, c.members"
)
LINK_REPAIR = (
    "MATCH (r:Plan {id: $repair}), (t:Crossmatch {id: $tid}) CREATE (r)-[:AFTER]->(t) "
    "WITH r UNWIND $plans AS pid MATCH (old:Plan {id: pid}) CREATE (r)-[:REPAIRS]->(old) "
    "RETURN count(*)"
)

FAILURE_CYPHER: dict[str, str] = {
    "crossmatch_arc_exists": ARC_EXISTS,
    "next_crossmatch_id": NEXT_CROSSMATCH_ID,
    "record_crossmatch": RECORD_CROSSMATCH,
    "mark_arc_negative": MARK_ARC_NEGATIVE,
    "break_cycles": BREAK_CYCLES,
    "drop_arc": DROP_ARC,
    "drop_flow_arc": DROP_FLOW_ARC,
    "release_stranded": RELEASE_STRANDED,
    "retake": RETAKE,
    "read_crossmatch": READ_CROSSMATCH,
    "list_crossmatches": LIST_CROSSMATCHES,
    "swaps_killed": SWAPS_KILLED,
    "broken_planned": BROKEN_PLANNED,
    "stranded": STRANDED,
    "rescue_embedded_2way": RESCUE_EMBEDDED_2WAY,
    "options_of_stranded": OPTIONS_OF_STRANDED,
    "repairs_after": REPAIRS_AFTER,
    "repair_candidates": REPAIR_CANDIDATES,
    "link_repair": LINK_REPAIR,
}


def _pair_dict(pair: Any) -> dict[str, Any]:
    return {
        "id": pair.id,
        "patient_abo": pair.patient_abo,
        "donor_abo": pair.donor_abo,
        "pra": pair.pra,
        "hard": pair.hard,
    }


def _unknown(tool: str, args: dict[str, Any], timer: Timer, exc: UnknownEntity) -> dict[str, Any]:
    timer.stop(0)
    result = {"message": str(exc), "entity": {"kind": exc.kind, "id": exc.entity_id}}
    return envelope(Outcome.UNKNOWN_ENTITY, result, (), trace=build_trace(tool, args, timer))


def _test_id(tools: SwapTools, crossmatch_id: Any) -> int:
    try:
        tid = int(crossmatch_id)
    except (TypeError, ValueError):
        raise UnknownEntity("Crossmatch", crossmatch_id) from None
    if not tools.graph.query(READ_CROSSMATCH, {"tid": tid}).result_set:
        raise UnknownEntity("Crossmatch", tid)
    return tid


def record_crossmatch(
    tools: SwapTools, donor_pair: Any, patient_pair: Any, result: str, recorded_by: str
) -> dict[str, Any]:
    """HUMAN-ONLY: record a lab crossmatch on one donor -> patient arc, and let the graph react."""
    timer = Timer()
    args = {
        "donor_pair": donor_pair,
        "patient_pair": patient_pair,
        "result": result,
        "recorded_by": recorded_by,
    }
    if result not in RESULTS:
        raise ValueError(f"result must be one of {list(RESULTS)}, got {result!r}")
    who = " ".join(str(recorded_by).split())
    if not who or len(who) > 80:
        raise ValueError("recorded_by must be a name of 1 to 80 characters")
    try:
        donor = resolve_pair(tools.graph, donor_pair)
        patient = resolve_pair(tools.graph, patient_pair)
    except UnknownEntity as exc:
        return _unknown("record_crossmatch", args, timer, exc)

    tools.work = []
    ids = {"a": donor.id, "b": patient.id}
    if not int(tools._run("crossmatch_arc_exists", ids)[0][0]):
        timer.stop(0)
        found = {
            "donor": _pair_dict(donor),
            "patient": _pair_dict(patient),
            "note": (
                f"the graph does not know of a link from the donor of pair {donor.id} to the patient "
                f"of pair {patient.id} (incompatible, or already failed a crossmatch); nothing was "
                "recorded"
            ),
            "graph_work": list(tools.work),
        }
        return envelope(
            Outcome.NOT_IN_GRAPH, found, (), trace=build_trace("record_crossmatch", args, timer)
        )

    tid = int(tools._run("next_crossmatch_id")[0][0])
    at = datetime.now(UTC).isoformat(timespec="seconds")
    tools._run("record_crossmatch", {**ids, "tid": tid, "result": result, "by": who, "at": at})
    effects: dict[str, Any] = {}
    if result == "negative":
        tools._run("mark_arc_negative", {**ids, "tid": tid})
    else:
        killed = int(tools._run("break_cycles", {**ids, "tid": tid})[0][0])
        tools._run("drop_arc", ids)
        tools._run("drop_flow_arc", ids)
        released = [int(v) for v in tools._run("release_stranded", {"tid": tid})[0][0]]
        reopened = 0
        if released:
            reopened = int(tools._run("retake", {"ids": released})[0][1] or 0)
        effects = {"swaps_killed": killed, "released": released, "swaps_reopened": reopened}
    work = list(tools.work)
    timer.stop(len(work))

    impact = failure_impact(tools, tid)["result"] if result == "positive" else None
    found = {
        "crossmatch": {
            "id": tid,
            "donor_pair": donor.id,
            "patient_pair": patient.id,
            "result": result,
            "recorded_by": who,
            "recorded_at": at,
        },
        "effects": effects,
        "impact": impact,
        "graph_work": work,
        "notice": "Synthetic patients only. Decision support, not medical advice.",
    }
    path = (impact or {}).get("path") or [[donor.id, patient.id]]
    return envelope(
        Outcome.ANSWERED_WITH_PATH, found, path, trace=build_trace("record_crossmatch", args, timer)
    )


def failure_impact(tools: SwapTools, crossmatch_id: Any) -> dict[str, Any]:
    """What one crossmatch result broke, who it stranded, and what is still alive to save them."""
    timer = Timer()
    args = {"crossmatch_id": crossmatch_id}
    try:
        tid = _test_id(tools, crossmatch_id)
    except UnknownEntity as exc:
        return _unknown("failure_impact", args, timer, exc)

    tools.work = []
    _, donor, patient, outcome, by, at = tools._run("read_crossmatch", {"tid": tid})[0]
    killed, killed_2way = tools._run("swaps_killed", {"tid": tid})[0]
    broken = [
        {"plan": int(r[0]), "status": str(r[1]), "kind": r[2] or "plan", "key": str(r[3]),
         "members": [int(m) for m in r[4]]}
        for r in tools._run("broken_planned", {"tid": tid})
    ]
    stranded = [
        {"id": int(r[0]), "patient_abo": r[1], "donor_abo": r[2], "pra": r[3], "hard": bool(r[4]),
         "now_committed_to": None if r[5] is None else int(r[5])}
        for r in tools._run("stranded", {"tid": tid})
    ]
    rescue = [
        {"broken": str(r[0]), "rescue": str(r[1]), "members": [int(m) for m in r[2]]}
        for r in tools._run("rescue_embedded_2way", {"tid": tid})
    ]
    waiting = [p["id"] for p in stranded if p["now_committed_to"] is None]
    options = dict.fromkeys(waiting, 0)
    if waiting:
        options.update(
            {int(s): int(n) for s, n in tools._run("options_of_stranded", {"ids": waiting})}
        )
    repairs = [
        {"plan": int(r[0]), "status": str(r[1])}
        for r in tools._run("repairs_after", {"tid": tid})
    ]
    timer.stop(len(tools.work))

    path = [b["members"] for b in broken] + [r["members"] for r in rescue]
    result = {
        "crossmatch": {
            "id": tid,
            "donor_pair": int(donor),
            "patient_pair": int(patient),
            "result": outcome,
            "recorded_by": by,
            "recorded_at": at,
        },
        "swaps_killed": int(killed or 0),
        "swaps_killed_2way": int(killed_2way or 0),
        "broken_planned_swaps": broken,
        "stranded": stranded,
        "still_waiting": waiting,
        "options_left": {str(k): v for k, v in options.items()},
        "rescue": rescue,
        "repairs": repairs,
        "path": path,
        "graph_work": list(tools.work),
    }
    trace = build_trace("failure_impact", args, timer)
    if not result["swaps_killed"]:
        result["note"] = "the graph knows of no swap this result broke"
        return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=trace)
    return envelope(Outcome.ANSWERED_WITH_PATH, result, path, trace=trace)


def draft_repair(tools: SwapTools, crossmatch_id: Any, max_len: int = 3) -> dict[str, Any]:
    """Draft a repair plan over the swaps still alive, re-matching the stranded patients first."""
    timer = Timer()
    args = {"crossmatch_id": crossmatch_id, "max_len": max_len}
    if max_len not in (2, 3):
        raise ValueError(f"a repair uses 2- or 3-way swaps; max_len must be 2 or 3, got {max_len!r}")
    impact = failure_impact(tools, crossmatch_id)
    if impact["outcome"] == Outcome.UNKNOWN_ENTITY.value:
        return {**impact, "trace": {**impact["trace"], "tool": "draft_repair"}}
    found = impact["result"]
    tid = found["crossmatch"]["id"]
    waiting = found["still_waiting"]
    work = list(found["graph_work"])
    if not waiting:
        timer.stop(0)
        result = {
            "plan": None,
            "crossmatch": found["crossmatch"],
            "note": "nobody is waiting because of this result: there is nothing to repair",
            "graph_work": work,
        }
        return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=build_trace("draft_repair", args, timer))

    tools.work = []
    rows = tools._run("repair_candidates", {"max_len": max_len, "ids": waiting})
    work += tools.work
    stranded = set(waiting)
    cycles = [[int(m) for m in row[1]] for row in rows]
    weights = [sum(STRANDED_WEIGHT if m in stranded else 1.0 for m in cycle) for cycle in cycles]
    pack = pack_cycles(cycles, weights=weights)
    if not pack.chosen:
        timer.stop(len(rows))
        result = {
            "plan": None,
            "crossmatch": found["crossmatch"],
            "still_waiting": waiting,
            "note": (
                "the graph has no live swap that can re-match these patients now; they wait for "
                "the next match run"
            ),
            "graph_work": work,
        }
        return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=build_trace("draft_repair", args, timer))

    keys = [str(rows[i][0]) for i in pack.indices]
    matched = sum(len(c) for c in pack.chosen)
    plan_id, guard = tools._write_plan("repair", pack.chosen, keys, matched, max_len, None)
    original = sorted({b["plan"] for b in found["broken_planned_swaps"] if b["status"] == "approved"})
    tools._run("link_repair", {"repair": plan_id, "tid": tid, "plans": original})
    work += tools.work
    timer.stop(len(rows) + len(keys))

    rematched = sorted(stranded.intersection(m for c in pack.chosen for m in c))
    rescue_keys = {r["rescue"] for r in found["rescue"]}
    packet = tools._packet(plan_id)
    packet["guard"] = guard
    packet["repair"] = {
        "crossmatch_id": tid,
        "repairs_plans": original,
        "stranded": waiting,
        "rematched": rematched,
        "still_waiting": sorted(stranded.difference(rematched)),
        "used_embedded_2way": sorted(rescue_keys.intersection(keys)),
        "candidates": len(rows),
    }
    packet["graph_work"] = work
    packet["note"] = "the repair is a draft in the graph; a person has to approve it"
    return envelope(
        Outcome.ANSWERED_WITH_PATH, packet, pack.chosen, trace=build_trace("draft_repair", args, timer)
    )


def list_crossmatches(tools: SwapTools) -> dict[str, Any]:
    """Every crossmatch result recorded in the graph, with how many swaps each one killed."""
    timer = Timer()
    rows = tools.graph.query(LIST_CROSSMATCHES).result_set
    tests = [
        {"id": int(r[0]), "donor_pair": int(r[1]), "patient_pair": int(r[2]), "result": r[3],
         "recorded_by": r[4], "recorded_at": r[5], "swaps_killed": int(r[6])}
        for r in rows
    ]
    timer.stop(len(tests))
    result: dict[str, Any] = {"crossmatches": tests}
    trace = build_trace("list_crossmatches", {}, timer)
    if not tests:
        result["note"] = "the graph does not know of any crossmatch result yet"
        return envelope(Outcome.NOT_IN_GRAPH, result, (), trace=trace)
    return envelope(Outcome.ANSWERED_WITH_PATH, result, (), trace=trace)
