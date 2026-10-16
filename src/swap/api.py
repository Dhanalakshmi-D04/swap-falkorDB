"""M8: the FastAPI app - pool loading, every tool, the proof panel, a trace log and the graph view.

The app is a thin HTTP skin over the same FalkorDB tool layer the MCP server exposes: one active
graph (`pool`), loaded by `POST /pool`, and every tool answer carries the four-outcome envelope.
`GET /proof` shows the product's FalkorDB numbers next to `crosscheck.py`, a labelled independent
plain-Python count from the same pool. `make run` starts it (`uvicorn swap.api:app`).
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from swap import crosscheck
from swap.agent import run_agent
from swap.config import DEFAULT_ACTIVE_GRAPH
from swap.generator import generate_pool as draw_pool
from swap.graph import connect, graph_counts, load_pool, pool_from_graph
from swap.tools import SwapTools

ACTIVE_GRAPH = DEFAULT_ACTIVE_GRAPH
TRACE_LIMIT = 200
EDGE_CAP = 1500
#: Default pool loaded at startup when the active graph is missing, so a fresh visitor sees data.
DEFAULT_POOL_N = 200
DEFAULT_POOL_SEED = 1000
#: Overridable so a container can point at a web/ copied outside the installed package.
WEB_DIR = Path(os.environ.get("WEB_DIR") or (Path(__file__).resolve().parents[2] / "web"))

log = logging.getLogger("swap.api")


def ensure_default_pool(
    graph_name: str = ACTIVE_GRAPH,
    n: int = DEFAULT_POOL_N,
    seed: int = DEFAULT_POOL_SEED,
    *,
    db: Any | None = None,
) -> dict[str, Any]:
    """Load the default pool into `graph_name` unless that graph already exists in FalkorDB.

    Runs at startup so a fresh visitor (and `GET /proof` before any `POST /pool`) sees data; an
    explicit `POST /pool` still replaces it on demand through the UI Generate button.
    """
    db = db if db is not None else connect()
    if graph_name in db.list_graphs():
        return {"graph": graph_name, "loaded": False, "reason": "graph already exists"}
    pool = draw_pool(n, seed)
    stats = load_pool(pool, graph_name, db=db)
    return {"graph": graph_name, "loaded": True, "summary": pool.summary(), "load": stats.summary()}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Never let a graph hiccup take the HTTP server down: without data the endpoints simply report
    # the empty/graphless outcomes until a pool is loaded.
    try:
        log.info("startup default pool: %s", ensure_default_pool())
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - depends on a live FalkorDB
        log.warning("could not prepare the default pool at startup: %r", exc)
    yield


app = FastAPI(
    title="swap",
    version="0.1.0",
    description="Kidney paired-exchange decision support on FalkorDB (synthetic patients only)",
    lifespan=lifespan,
)

_traces: deque[dict[str, Any]] = deque(maxlen=TRACE_LIMIT)


def _tools() -> SwapTools:
    return SwapTools(ACTIVE_GRAPH)


def _record(env: dict[str, Any]) -> dict[str, Any]:
    """Answer with a tool envelope and keep its trace in the in-memory log (`GET /trace`)."""
    _traces.append({**env["trace"], "outcome": env["outcome"]})
    return env


class PoolRequest(BaseModel):
    n: int = Field(default=100, ge=0, le=2000, description="number of incompatible pairs")
    seed: int = Field(default=1, description="deterministic RNG seed")


class WhatIfRequest(BaseModel):
    kind: Literal["add_altruistic_donor", "remove_pair", "set_priority"]
    donor_abo: Literal["O", "A", "B", "AB"] | None = None
    pair_id: int | None = None
    weight: float | None = None
    max_len: int = Field(default=3, ge=2, le=4)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500, description="a question in plain English")


@app.post("/pool")
def create_pool(request: PoolRequest) -> dict[str, Any]:
    """Generate a synthetic pool from `{n, seed}` and load it as the active graph."""
    pool = draw_pool(request.n, request.seed)
    stats = load_pool(pool, ACTIVE_GRAPH)
    return {"graph": ACTIVE_GRAPH, "summary": pool.summary(), "load": stats.summary()}


@app.get("/tools/find_cycles")
def find_cycles(max_len: int = Query(3, ge=2, le=4)) -> dict[str, Any]:
    """Cycles of length 2..max_len, produced BY FalkorDB."""
    return _record(_tools().find_cycles(max_len))


@app.get("/tools/flow_bound")
def flow_bound() -> dict[str, Any]:
    """The `algo.maxFlow` bound (an upper bound that also allows chains)."""
    return _record(_tools().flow_bound())


@app.get("/tools/best_set")
def best_set(
    max_len: int = Query(3, ge=2, le=4),
    priority: float | None = Query(None, gt=0),
) -> dict[str, Any]:
    """Vertex-disjoint packing of the graph's cycles, optionally weighting hard patients."""
    return _record(_tools().best_set(max_len, priority))


@app.get("/tools/explain/{pair_id}")
def explain(pair_id: int) -> dict[str, Any]:
    """Who could give to this pair's patient and why others cannot; unknown id -> `unknown_entity`."""
    return _record(_tools().explain_unmatched(pair_id))


@app.post("/tools/what_if")
def what_if(request: WhatIfRequest) -> dict[str, Any]:
    """Re-run the tools on a modified copy of the pool and report what changed."""
    args = request.model_dump(exclude_none=True)
    kind = args.pop("kind")
    return _record(_tools().what_if(kind, args))


@app.post("/ask")
def ask(request: AskRequest) -> dict[str, Any]:
    """Answer a free-text question through the agent loop (tool mode when no LLM is configured).

    The model only chooses a tool from the fixed catalogue and fills typed arguments; the answer
    is rejected if it names a pair id that appears in no tool result of the run.
    """
    outcome = run_agent(request.question)
    for call in outcome["tool_calls"]:
        _traces.append(dict(call))
    return outcome


@app.get("/proof")
def proof() -> dict[str, Any]:
    """The reproducible numbers: 2-way / up-to-3 / unbounded optimum / flow bound, and the checks."""
    tools = _tools()
    if ACTIVE_GRAPH not in tools.db.list_graphs() or tools.n_pairs() == 0:
        return {
            "graph": ACTIVE_GRAPH,
            "loaded": False,
            "message": "no active pool; POST /pool first",
        }

    pool = pool_from_graph(tools.graph)
    two = tools.best_set(2)["result"]["matched"]
    three = tools.best_set(3)["result"]["matched"]
    cover = tools.true_cycle_optimum()["result"]["unbounded_cycle_cover"]
    bound = tools.flow_bound()["result"]["flow_bound"]
    cycles = tools.find_cycles(3)["result"]["counts"]
    checks = crosscheck.checks(pool, cycles=cycles, flow_bound=bound)
    ordering = [two, three, cover, bound]
    return {
        "graph": ACTIVE_GRAPH,
        "loaded": True,
        "pool": pool.summary(),
        "falkordb": {
            "cycles": cycles,
            "best_set_2": two,
            "best_set_3": three,
            "unbounded_cycle_cover": cover,
            "flow_bound": bound,
        },
        "crosscheck": {
            "label": "independent plain-Python check from the same pool (no networkx in src)",
            "cycles": crosscheck.cycle_counts(pool),
            "matching_bound": crosscheck.matching_size(pool),
        },
        "ordering": {
            "values": ordering,
            "invariant": "2-way <= up-to-3 <= unbounded cycle cover <= flow bound",
            "holds": crosscheck.ordered(ordering),
        },
        "checks": [
            {
                "name": check.name,
                "crosscheck": check.crosscheck,
                "falkordb": check.falkordb,
                "agrees": check.agrees,
            }
            for check in checks
        ],
        "all_checks_pass": all(check.agrees for check in checks) and crosscheck.ordered(ordering),
    }


@app.get("/trace")
def trace() -> dict[str, Any]:
    """The last (up to 200) tool traces: `{tool, args, ms, rows, outcome}`."""
    return {"limit": TRACE_LIMIT, "count": len(_traces), "traces": list(_traces)}


@app.get("/graph")
def graph_view(max_len: int = Query(3, ge=2, le=4)) -> dict[str, Any]:
    """Nodes, edges and the chosen cycles for the UI; edges are capped to the chosen pairs."""
    tools = _tools()
    pool = pool_from_graph(tools.graph)
    chosen = tools.best_set(max_len)["result"]["chosen"]
    covered = {patient for cycle in chosen for patient in cycle}
    edges = [[source, target] for source, target in pool.edges]
    truncated = False
    if len(edges) > EDGE_CAP:
        edges = [[source, target] for source, target in edges if source in covered and target in covered]
        truncated = True
    nodes = [
        {
            "id": pid,
            "patient_abo": pool.patient_abo[i],
            "donor_abo": pool.donor_abo[i],
            "pra": pool.pra[i],
            "hard": pool.hard[i],
        }
        for i, pid in enumerate(pool.ids)
    ]
    return {
        "graph": ACTIVE_GRAPH,
        "nodes": nodes,
        "edges": edges,
        "chosen": chosen,
        "counts": graph_counts(tools.graph),
        "edge_count": len(pool.edges),
        "truncated": truncated,
    }


if WEB_DIR.is_dir():
    # Registered last: the API routes above win, everything else is served from web/.
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
