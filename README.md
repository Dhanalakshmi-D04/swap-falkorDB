# Swap

**An agent that finds kidney-swap chains in a graph, shows the exact cycle behind every answer, and says "the graph does not know" when it does not.**

Graph Hacks 2026 (WeMakeDevs × FalkorDB) · Track 1: Agents That Act on Connected Data · [see the demo](#run-it)

> **What this is not:** not medical advice, not a clinical tool, not trained on or connected to any real patient data. Every patient in this repository is synthetic. It illustrates decision support for a transplant coordinator, nothing more.

Patients with a willing but incompatible donor can swap donors in cycles (`A→B→C→A`). Allowing 3-way swaps instead of only 2-way swaps matches more patients. Same pool of synthetic pairs, two swap lengths:

| pool (N pairs) | 2-way only | up to 3-way | any cycle length | max-flow bound (also allows chains) |
|---:|---:|---:|---:|---:|
| 100 | 45.4 ± 7.5 | 56.1 ± 8.4 | 57.0 ± 8.0 | 61.6 ± 6.3 |
| 200 | 98.1 ± 7.2 | 115.7 ± 7.3 | 115.8 ± 7.4 | 121.8 ± 7.8 |
| 300 | 151.9 ± 12.7 | 170.2 ± 12.1 | 170.4 ± 12.1 | 178.6 ± 11.7 |

Every cell is `mean ± sd` of patients matched, over K = 20 seeds (1000–1019), read through the FalkorDB tools. Go 2 → 3 and you match **+10.7 / +17.6 / +18.2** more patients (10.7% / 8.8% / 6.1% of the pool); 3-cycles already reach 92 / 99 / 98% of the best possible with cycles of **any** length. Reproduce with one command — see [Run it](#run-it).

## Priority weighting
`best_set(max_len=3, priority>1)` counts each patient with PRA 0.90 `priority` times, so the cycle packing prefers covering highly sensitised patients. Measured through the FalkorDB tools, N = 200 pairs over K = 20 seeds (1000–1019), priority weight 1 vs 3 (`make verify-fairness FAIRNESS_SEEDS=20`):

| priority weight | highly sensitised (PRA 0.90) matched | all patients matched |
|---:|---:|---:|
| 1 (off) | 76.5% ± 7.7 | 115.7 ± 7.3 |
| 3 | 96.8% ± 3.0 | 114.7 ± 7.1 |

Weighting lifted the highly sensitised share by **+20.3 ± 7.1** percentage points, and it improved that share on **20/20 seeds** (no ties, no seed worse). The cost is small but real: mean total matched fell by **1.0** patient (lower on 9/20 seeds, higher on none). The effect is **reproduced**, not assumed — the raw per-seed output is in the command above.

## Status
Working end-to-end build. The graph engine, the typed tool catalogue, the MCP server, the HTTP API, the single-page UI, the agent loop, `make verify` and `make verify-fairness` are in place; `make test` runs 109 tests green. Honest caveat: an LLM endpoint was **not** configured during the build, so the agent was exercised in its deterministic **tool mode** (see the agent section); the LLM path is covered by tests with a fake client, not by a live model. See `AI_USE.md`.

## Graph data model
One graph per pool (default name `pool`). Synthetic pools are generated from a seed and loaded with `swap.graph.load_pool`.

Nodes
- `(:Pair {id, patient_abo, donor_abo, pra, hard})` — one incompatible donor/patient pair. `id` is an integer, `patient_abo`/`donor_abo` ∈ {`O`,`A`,`B`,`AB`}, `pra` ∈ {0.05, 0.45, 0.90}, `hard` = `pra == 0.90`.
- Flow network so `algo.maxFlow` can run: `(:S)` source, `(:Donor {id})` per pair, `(:Patient {id})` per pair, `(:T)` sink.
- `(:Altruist {id, donor_abo})` — donor-only node, created only inside a what-if scratch graph, so it can start a chain but never appears in a cycle.
- `(:Plan {id, status, created_at, max_len, priority, matched, swaps, decided_by, decided_at})` — a drafted swap plan, written by `draft_plan` (status `draft`) and decided by a person (`approved` or `rejected`). It lives in the same graph as the pool, so a new pool replaces its plans.

Relationships
- `(:Pair)-[:CAN_GIVE]->(:Pair)` — "donor of the left pair can give to patient of the right pair": ABO-compatible **and** the crossmatch passes at the patient's PRA.
- `(:Plan)-[:PROPOSES {swap, pos}]->(:Pair)` — the pairs of each swap in a plan: `swap` is the swap's index and `pos` its position in the ring (each donor gives to the next position, the last to the first).
- `(:S)-[:F {cap:1}]->(:Donor)`, `(:Donor)-[:F {cap:1}]->(:Patient)`, `(:Patient)-[:F {cap:1}]->(:T)` — the unit-capacity flow network; `:Altruist` nodes join as a `:Donor`.

Indexes (created before any edges are loaded, `swap/graph.py`):

```
CREATE INDEX FOR (p:Pair) ON (p.id)
CREATE INDEX FOR (d:Donor) ON (d.id)
CREATE INDEX FOR (p:Patient) ON (p.id)
```

Node/edge loading, batched with `UNWIND` in chunks of 5000 (`swap/graph.py`):

```
UNWIND $rows AS r CREATE (:Pair {id: r[0], patient_abo: r[1], donor_abo: r[2], pra: r[3], hard: r[4]}) RETURN count(*)
UNWIND $rows AS x MATCH (a:Pair {id: x[0]}),(b:Pair {id: x[1]}) CREATE (a)-[:CAN_GIVE]->(b) RETURN count(*)
UNWIND $rows AS x CREATE (:Donor {id: x}) RETURN count(*)
UNWIND $rows AS x CREATE (:Patient {id: x}) RETURN count(*)
MATCH (s:S),(d:Donor {id: $id}) CREATE (s)-[:F {cap: 1}]->(d) RETURN count(*)
UNWIND $rows AS x MATCH (d:Donor {id: x[0]}),(p:Patient {id: x[1]}) CREATE (d)-[:F {cap: 1}]->(p) RETURN count(*)
UNWIND $rows AS x MATCH (p:Patient {id: x}),(t:T) CREATE (p)-[:F {cap: 1}]->(t) RETURN count(*)
```

## The Cypher and algorithms the product depends on
All Cypher is human-written, parameterised and lives in `src/swap/tools.py` and `src/swap/graph.py` (constants quoted verbatim below). **The model never writes Cypher** — it only chooses a tool and fills typed arguments. Entity ids are resolved against the graph before any other query runs:

```
MATCH (p:Pair {id: $id}) RETURN p.id, p.patient_abo, p.donor_abo, p.pra, p.hard
```

### Cycles — produced BY FalkorDB (`swap/tools.py`)
```
MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(a) WHERE a.id < b.id RETURN a.id, b.id ORDER BY a.id, b.id

MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(a) WHERE a.id < b.id AND a.id < c.id RETURN a.id, b.id, c.id ORDER BY a.id, b.id, c.id

MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(d:Pair)-[:CAN_GIVE]->(a) WHERE a.id < b.id AND a.id < c.id AND a.id < d.id AND b.id <> d.id RETURN a.id, b.id, c.id, d.id ORDER BY a.id, b.id, c.id, d.id
```
`a.id < b.id` (and `a.id < c.id`, …) canonicalises each directed cycle to its smallest id so it is counted once; in the 4-cycle, `b.id <> d.id` rejects the walk `a→b→c→b→a`.

### The max-flow bound (`swap/tools.py`)
```
MATCH (s:S),(t:T) CALL algo.maxFlow({sourceNodes:[s],targetNodes:[t],relationshipTypes:['F'],capacityProperty:'cap'}) YIELD maxFlow, nodes, edges, edgeFlows RETURN maxFlow, nodes, edges, edgeFlows
```

### Explaining a pair (`swap/tools.py`)
```
MATCH (p:Pair {id: $id})<-[:CAN_GIVE]-(d:Pair) RETURN d.id ORDER BY d.id
MATCH (p:Pair {id: $id})-[:CAN_GIVE]->(r:Pair) RETURN r.id ORDER BY r.id
MATCH (p:Pair {id: $id}) MATCH (q:Pair) WHERE q.id <> p.id WITH p, q, (q.donor_abo = 'O' OR p.patient_abo = 'AB' OR q.donor_abo = p.patient_abo) AS ok RETURN sum(CASE WHEN ok THEN 1 ELSE 0 END), sum(CASE WHEN ok THEN 0 ELSE 1 END)
MATCH (p:Pair {id: $id})-[:CAN_GIVE]->(q:Pair)-[:CAN_GIVE]->(p) RETURN p.id, q.id
MATCH (p:Pair {id: $id})-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(p) RETURN p.id, b.id, c.id
```
The last two statements return the cycles through one pair. The split query counts, from stored attributes alone, how many other pairs are ABO-compatible vs incompatible with this patient; the gap between ABO-compatible and actually-connected donors is a crossmatch failure at this patient's PRA, not a missing fact.

### What-if chains from a donor-only altruist (`swap/tools.py`, scratch graph only)
```
MATCH (a:Altruist) RETURN a.id ORDER BY a.id
MATCH (a:Altruist {id: $id})-[:CAN_GIVE]->(p:Pair) RETURN p.id ORDER BY p.id
MATCH (a:Altruist {id: $id})-[:CAN_GIVE]->(p1:Pair)-[:CAN_GIVE]->(p2:Pair) WHERE p1.id <> p2.id RETURN p1.id, p2.id ORDER BY p1.id, p2.id
```

### The action: draft, then a person decides (`swap/tools.py`)
`draft_plan` writes the best set back into the graph as a `:Plan` that `PROPOSES` each pair; `get_plan` rebuilds the coordinator packet from what is stored; `decide_plan` (human-only, see below) records the decision; pairs of an *approved* plan are committed and the next draft skips them.
```
MATCH (pl:Plan) RETURN coalesce(max(pl.id), 0) + 1
CREATE (pl:Plan {id: $id, status: 'draft', created_at: $created_at, max_len: $max_len, priority: $priority, matched: $matched, swaps: $swaps}) RETURN pl.id
UNWIND $rows AS r MATCH (pl:Plan {id: $plan}), (p:Pair {id: r[2]}) CREATE (pl)-[:PROPOSES {swap: r[0], pos: r[1]}]->(p) RETURN count(*)
MATCH (pl:Plan {id: $id}) RETURN pl.id, pl.status, pl.created_at, pl.max_len, pl.priority, pl.matched, pl.swaps, pl.decided_by, pl.decided_at
MATCH (pl:Plan {id: $id})-[r:PROPOSES]->(p:Pair) RETURN r.swap, r.pos, p.id, p.patient_abo, p.donor_abo, p.pra, p.hard ORDER BY r.swap, r.pos
MATCH (pl:Plan {id: $id}) SET pl.status = $status, pl.decided_by = $by, pl.decided_at = $at RETURN pl.id
MATCH (pl:Plan {status: 'approved'})-[:PROPOSES]->(p:Pair) RETURN DISTINCT p.id ORDER BY p.id
```
The approval gate is structural, not a prompt: `decide_plan` exists only behind `POST /plans/{id}/decision` (the web UI). It is not in the agent's tool catalogue and not on the MCP server, and `tests/test_plan.py` fails if it ever appears there. A decision needs a name, is recorded with a timestamp, and can be made once.

### The solvers (`src/swap/solve.py`)
FalkorDB supplies the **candidate** cycles and edges; the best disjoint set is chosen by an integer program the agent calls as a tool:
- **Best set** = vertex-disjoint cycle packing maximising covered patients, `scipy.optimize.milp` (0/1 per candidate cycle, ≤1 cycle per patient).
- **True unbounded cycle optimum** = maximum cycle cover of any length, which is an **assignment problem with self-loops** (`scipy.optimize.linear_sum_assignment`; the self-loop is "unmatched").

## The tool catalogue and the four outcomes
The catalogue is fixed and exposes seven tools to the agent (plus `generate_pool`, `get_plan` and `list_plans` on the MCP server). Each is typed; the OpenAI tool-calling schema is generated from the typed signatures (`swap/agent.py`).

| tool | arguments | what it answers |
|---|---|---|
| `find_cycles` | `max_len: 2\|3\|4` | cycles of length 2..max_len, as ordered pair-id lists + counts (from FalkorDB) |
| `flow_bound` | — | the `algo.maxFlow` bound over the donor/patient network |
| `best_set` | `max_len: 2\|3\|4`, `priority?: float` | vertex-disjoint packing of the graph's cycles, max patients; `priority>1` weights highly sensitised patients |
| `true_cycle_optimum` | — | maximum cycle cover of any length (MILP proof panel) |
| `explain_unmatched` | `pair_id: int` | who could give to this patient, why others cannot, and the cycles through the pair |
| `what_if` | `kind`, `donor_abo?`, `pair_id?`, `weight?`, `max_len` | re-run the tools on a modified copy and report the delta |
| `draft_plan` | `max_len: 2\|3\|4`, `priority?: float` | write the best set into the graph as a **draft** plan (skipping pairs already committed) and return the coordinator packet; it cannot approve |

Every tool returns a four-outcome **envelope** `{outcome, result, path, trace:{tool, args, ms, rows}}` (`swap/envelope.py`):

- `answered_with_path` — the answer plus the path (cycles/arcs) that produced it.
- `from_memory` — reserved for dated recalls (not used by the current tools).
- `not_in_graph` — the traversal came back empty. This means **"the graph does not know"**, never "safe".
- `unknown_entity` — an id the caller named is not in the graph; returned before any other query runs.

## How `algo.maxFlow` is used, and why it is only an upper bound
`flow_bound` builds a unit-capacity flow network `S → donors → patients → T` and reads `algo.maxFlow` with `capacityProperty:'cap'`. Because capacities are 1 and the middle arcs are donor→patient, this maximum flow equals a **maximum bipartite matching** of donors to patients — a **relaxation** of the cycle problem that also permits **chains**, i.e. paths starting at an altruistic donor with no patient to swap. So the figure is an **upper bound that also allows chains, NOT the cycle optimum**. The true unbounded **cycle** optimum is smaller (it forbids chains); the product computes it separately as an assignment problem with self-loops (`true_cycle_optimum`). The invariant the tests pin: `2-way ≤ up-to-3 ≤ true cycle cover ≤ max-flow bound`, with equality allowed.

## Architecture
```
            (M1) synthetic generator  ──►  (M2) loader  ──►  ┌──────────────┐
                                                             │   FalkorDB   │  one graph per pool
   question ─► (M10) agent loop ─► fixed tool catalogue ───► │  (:Pair,     │
                  │  LLM picks a tool        (M3/M4/M5)     │   CAN_GIVE,  │
                  │  or keyword router                        │   :F flow net)│
                  ▼                                           └──────┬───────┘
        four-outcome envelope {outcome, result, path, trace}         │
                  │                                          human-written Cypher
                  ├─ (M7) MCP server  `python -m swap.mcp_server`    │
                  ├─ (M8) FastAPI app  `make run`  (POST /ask, /tools/*, /proof, /graph, /trace)
                  └─ (M9) single-page UI (web/, Cytoscape.js vendored)
```
Every product answer walks the graph on the request path; the Python/networkx oracles live only in tests (`tests/oracle.py`) and one labelled plain-Python check (`src/swap/crosscheck.py`) used by `/proof` and `make verify`. `src/` never imports networkx.

### The agent loop (`swap/agent.py`)
`run_agent(question)` returns `{answer, tool_calls, rejected_ids, mode}`.
- **LLM mode** (`mode:"llm"`): configured by `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`. The model only chooses a tool from the catalogue and fills typed arguments; tool results are the envelopes, fed back verbatim.
- **Tool mode** (`mode:"tool"`): with no LLM configured, a deterministic keyword router answers ("cycle", "who can be matched", "explain pair N", "what if", "bound", "optimum"). This always works and is the demo's fallback.
- **Sentence check**: after the final answer is produced, pair ids named in it (`pair 17`, `#17`, ids in lists) are collected and any id that appears in **no tool result of the run** is rejected — the answer is withheld with the offending ids listed in `rejected_ids`.

## Run it
Prerequisites: Python 3.12+ and Docker.

**Live demo:** _TODO — the human adds the deployed URL here before submission._

The whole stack in two containers (the app plus its own FalkorDB; only the app is published):

```bash
APP_PORT=8010 docker compose up -d --build  # app on http://127.0.0.1:8010; FalkorDB stays internal
curl -X POST localhost:8010/pool -H 'content-type: application/json' -d '{"n":120,"seed":7}'
curl localhost:8010/proof                    # the numbers above + the independent cross-check
docker compose down -v                        # stop the stack and drop its volume
```
`make docker-up` / `make docker-down` wrap the first and last commands. `APP_PORT` sets the host port (default `80`; the container always listens on `8000`). Compose builds `python:3.12-slim`, installs the project from `pyproject.toml` and serves `web/`, so no local Python is needed. At startup the app loads a default pool (N=200, seed 1000) when the active graph is missing, so `/` and `/proof` show data immediately; the UI **Generate** button still loads any `{n, seed}` you pick.

Local development against FalkorDB on the host (port 16400):

```bash
make up                      # start FalkorDB on 127.0.0.1:16400 (container swap-falkordb)
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

make test                    # pytest (109 tests; graph tests skip cleanly if FalkorDB is down)
make verify                  # the numbers table above (default K=5 seeds)
make verify SEEDS=20         # the K=20 run quoted in this README (seeds 1000-1019, ~5 min)
make verify-fairness         # priority weighting (weight 1 vs 3) on highly sensitised patients

make run                     # FastAPI + UI at http://127.0.0.1:8000
```
HTTP endpoints: `POST /pool {n, seed}`, `GET /tools/find_cycles?max_len=`, `GET /tools/flow_bound`, `GET /tools/best_set?max_len=&priority=`, `GET /tools/explain/{pair_id}`, `POST /tools/what_if`, `POST /ask {question}`, `GET /proof`, `GET /graph`, `GET /trace`.

MCP server over stdio (the fixed catalogue, one graph per session):
```bash
PYTHONPATH=src .venv/bin/python -m swap.mcp_server
```

Optional LLM endpoint (any OpenAI-compatible API). Without it, `/ask` uses tool mode:
```bash
cp .env.example .env     # then fill LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
```

## How this compares with the literature
Ours is a synthetic re-implementation. The published figures below are **not ours** — they are labelled for context:
- Roth, Sönmez and Ünver, *Efficient Kidney Exchange*, AER 97(3), 2007, Table 2 (simulation means, 500 pools): at n=100 incompatible pairs, 2-way matches 49.7, up to 3-way 59.7, up to 4-way 60.35, unconstrained 60.39; the paper reports 3-cycles capture ~94% of remaining gains at n=100. Our generator follows the same ABO/PRA mix and pool rule (RSU 2007, Table 1 and §III.B), omitting their wife-husband crossmatch adjustment, so our levels differ slightly (different RNG stream and pool construction) while the shape — 2→3 is the big step, 3→4 adds under 1 patient per 100 pairs — matches.
- Saidman et al., *Transplantation* 81:773 (2006): on a real 45-patient set, 2-way allowed 8 pairs and adding 3-way allowed 11.

## Honest limits
- **Synthetic data only.** All patients are drawn from a seeded generator; no real or personal data is used, and this is decision support, not medical advice.
- **Known result.** The 2-vs-3 swap effect is the finding of Roth, Sönmez and Ünver (2007); we reproduce it, we do not discover it.
- **The max-flow figure is an upper bound that also allows chains**, not the cycle optimum — see above.
- **The generator omits the spouse-pair crossmatch adjustment** of the cited source.
- **What-if chains are deliberately short**: the altruist what-if only considers `altruist→p1` and `altruist→p1→p2` chains (one chain per donor). Longer chains are out of scope.
- **The agent was verified in tool mode**, without a live LLM (see Status).
- The UI was reworked on 2026-10-17 into a guided five-step flow and checked in headless Chromium (desktop, phone width, light and dark): every step, the map, the explanations, the what-if cards, the guided tour and the glossary were clicked through with no console errors. There is no automated browser test in the repo; `tests/test_web.py` only checks that the files are served and that ids and glossary terms line up.
- No clinical validation of any kind.

## Tests
`make test` runs pytest over `tests/`:
- `test_generator.py` — the pool rule, determinism, and ABO/PRA/edge-density against theory and an independent scalar re-implementation.
- `test_graph.py` — the loader holds exactly the pool (nodes, `CAN_GIVE` edges, flow network), and rejects a partial load.
- `test_tools.py` — every graph tool cross-checked against `tests/oracle.py` (networkx matching, `milp` packing, `linear_sum_assignment` cover): cycle counts equal, `maxFlow == bipartite matching`, the chain `2 ≤ 3 ≤ cover ≤ bound`, and the empty-traversal → `not_in_graph` semantics.
- `test_whatif.py` — `remove_pair` equals a from-scratch recomputation; adding an altruist never lowers the count; the scratch graph never outlives the call.
- `test_mcp.py` — the catalogue lists over the MCP SDK client and every tool answers in-process.
- `test_api.py` — every HTTP endpoint, unknown ids as `unknown_entity` (HTTP 200), bad input as 422, the proof panel's checks agree, and `/ask` answers in tool mode.
- `test_agent.py` — the tool-calling loop with a scripted fake OpenAI client (no network): routing, schema generation, the hallucinated-id rejection, and tool mode without env.
- `test_verify.py` — the `make verify` aggregation helpers (mean/sd, table, checks, ties) that decide PASS/FAIL and what this README quotes.
- `test_web.py` — the single page and its one vendored asset are served.
- `test_edge_cases.py` — degenerate pools (n = 0..3, and a hand-built zero-edge pool) through every endpoint and tool never 500; empty traversals are `not_in_graph`, unknown ids `unknown_entity`, and oversized `/pool` input is a 4xx, not a hang.
- `test_readme.py` — every Cypher line this README quotes is a constant in `swap/tools.py` / `swap/graph.py` verbatim, so the README cannot drift from the code, and the empty-traversal wording is pinned.
- `test_perf.py` — the N=300 bench budgets (generate+load < 5 s, `find_cycles(3)` < 1 s, `flow_bound` < 100 ms), printing their timings (marker `perf`, still run by `make test`).

## AI-use disclosure
See `AI_USE.md`. AI coding assistants were used throughout (research, implementation, tests, docs); the team is responsible for the graph model, the Cypher and the architecture, and every number here is reproduced by a command in this repository.
