# Swap

**An agent that runs a kidney-swap exchange inside a graph: FalkorDB holds every possible swap, guards every plan, reacts when a lab test fails, and explains every patient who is left out.**

Graph Hacks 2026 (WeMakeDevs × FalkorDB) · Track 1: Agents That Act on Connected Data · **Live demo:** https://swap.dhanalakshmi.tech/ · **Demo video:** _TODO — the human adds the link before submission._

> **What this is not:** not medical advice, not a clinical tool, not trained on or connected to any real patient data. Every patient in this repository is synthetic. It illustrates decision support for a transplant coordinator, nothing more.

Patients with a willing but incompatible donor can swap donors in rings (`A→B→C→A`). Most planned exchange transplants never happen, and a failed crossmatch, the lab test run just before surgery, is a common reason (Dickerson, Procaccia and Sandholm, *Failure-Aware Kidney Exchange*, Management Science 2019). Swap is built around that moment.

## The loop, on one pool (`make failure-story`, N = 200, seed 1000)
1. **The agent drafts a plan.** FalkorDB checks it before it is written: 41 swaps, 106 patients, every one of 106 donor→patient arcs present, no pair twice, nothing dead or taken.
2. **A person approves it.** The graph commits 106 pairs and marks the 24,303 stored swaps that touch them as taken.
3. **The lab reports a positive crossmatch** from the donor of pair 1 to the patient of pair 70. One Cypher statement kills the 27 stored swaps that relied on that arc and links the test to each of them. The approved 3-way swap `1>70>173` is broken; pairs 1, 70 and 173 are stranded and released. The graph finds the 2-way swap `1>173` still alive inside the broken ring (a back-arc), and counts the live options left: pair 1 has 9, pair 173 has 1, pair 70 has none.
4. **The agent drafts a repair** from the swaps still alive, re-matching the stranded patients first. It re-matches 1 and 173 through `1>173` and says plainly that pair 70 waits for the next run.
5. **A person approves the repair.** The graph records `(:Plan)-[:REPAIRS]->(:Plan)` and `(:Plan)-[:AFTER]->(:Crossmatch)`, so the history stays queryable.

Recording a lab result and approving a plan are human-only: neither exists in the agent's tool catalogue or on the MCP server, and tests fail if one appears there.

## What FalkorDB does here (`make graph-bench`)
The graph is the exchange's working state, not a store we read once. Measured through the product's own tools, N = 300 pairs, seed 1000, median of 5 fresh loads (count = swaps created, touched or read, or rows returned):

| step | what FalkorDB does | ms | count |
|---|---|---:|---:|
| `materialise_3` | store every 3-way swap as a `:Cycle`, counting its back-arcs | 593.3 | 64,976 |
| `materialise_2` | store every 2-way swap as a `:Cycle` | 13.9 | 1,888 |
| `candidates_all` | read the live swaps the integer program chooses from | 966.4 | 66,864 |
| `guard_pairs` | guard a draft: swaps distinct, no pair twice, none dead or taken | 0.8 | 64 |
| `guard_arcs` | guard a draft: every donor -> patient arc still exists | 2.7 | 156 |
| `add_selects` | link the plan to the swaps it chose | 7.2 | 64 |
| `commit_pairs` | approval: commit the plan's pairs | 1.6 | 156 |
| `mark_taken` | approval: mark every swap touching a committed pair as taken | 137.0 | 66,864 |
| `break_cycles` | positive crossmatch: kill every swap that used that arc, link the test to each | 6.3 | 4 |
| `release_stranded` | release the pairs of the approved swap it broke | 0.7 | 3 |
| `retake` | re-open swaps through the released pairs where nothing else holds them | 47.3 | 1,910 |
| `rescue_embedded_2way` | find the 2-way swap still alive inside the broken 3-way swap | 1.0 | 1 |
| `repair_candidates` | read the live swaps that can re-match the stranded patients | 5.3 | 55 |
| `explain_blocked_by` | why unmatched: the chosen swaps that block this patient (4 hops) | 3.3 | 5 |
| `explain_shortest_ring` | why unmatched: the shortest ring back to this patient | 0.8 | 4 |
| `whatif_copy` | what-if: `GRAPH.COPY` of the whole graph inside FalkorDB | 265.7 | 1 |
| `flow_bound` | `algo.maxFlow` over the donor -> patient network | 110.9 | 163 |

**Where the integer program fits.** Choosing the best set of disjoint swaps is an integer program (`scipy.optimize.milp`); no graph database solves that natively. It only chooses among the swaps FalkorDB stored, scored (`hard`, `back_arcs`) and filtered (`live`, not `taken`), and FalkorDB guards what it picks before anything is written. At N = 300 the solve itself takes several seconds in Python; repeat questions on an unchanged graph are answered from a memo keyed by a fingerprint FalkorDB computes over the stored swaps (27 ms), so a failed test, an approval or a reload always forces a fresh solve.

## The headline numbers
Same pool of synthetic pairs, two swap lengths:

| pool (N pairs) | 2-way only | up to 3-way | any cycle length | max-flow bound (also allows chains) |
|---:|---:|---:|---:|---:|
| 100 | 45.4 ± 7.5 | 56.1 ± 8.4 | 57.0 ± 8.0 | 61.6 ± 6.3 |
| 200 | 98.1 ± 7.2 | 115.7 ± 7.3 | 115.8 ± 7.4 | 121.8 ± 7.8 |
| 300 | 151.9 ± 12.7 | 170.2 ± 12.1 | 170.4 ± 12.1 | 178.6 ± 11.7 |

Every cell is `mean ± sd` of patients matched, over K = 20 seeds (1000–1019), read through the FalkorDB tools (`make verify SEEDS=20`). Go 2 → 3 and you match **+10.7 / +17.6 / +18.2** more patients (10.7% / 8.8% / 6.1% of the pool); 3-cycles already reach 92 / 99 / 98% of the best possible with cycles of **any** length. Re-run after the swap-object rewrite on 18 Oct 2026: identical, all cross-checks PASS.

### Priority weighting
`best_set(max_len=3, priority>1)` counts each highly sensitised patient (PRA 0.90) `priority` times, read from the `hard` count FalkorDB stores on every swap. N = 200 pairs over K = 20 seeds (1000–1019), weight 1 vs 3 (`make verify-fairness FAIRNESS_SEEDS=20`):

| priority weight | highly sensitised (PRA 0.90) matched | all patients matched |
|---:|---:|---:|
| 1 (off) | 76.5% ± 7.7 | 115.7 ± 7.3 |
| 3 | 96.8% ± 3.0 | 114.7 ± 7.1 |

Weighting lifted the highly sensitised share by **+20.3 ± 7.1** percentage points and improved it on **20/20 seeds**; mean total matched fell by **1.0** patient (lower on 9/20 seeds, higher on none).

## Why is this patient left out?
`explain_unmatched(pair_id)` answers with a verdict and the graph evidence behind it, against a plan (default: the latest) or the best set:
- **blocked**: the patient has live swaps, but each needs a pair a chosen swap already uses. A 4-hop walk (patient ← their swaps → the other pairs ← the chosen swap holding that pair) names the chosen swaps that block the most options, e.g. *"Pair 1 has 24 possible swaps, but each one needs a pair that plan 1 already uses. The chosen swap 147>251 blocks 9 of them."* (N = 300, seed 1000).
- **ring too long**: no 2- or 3-way swap exists; Cypher `shortestPath` finds the shortest ring back to the patient and `algo.SPpaths` lists equally short alternative routes.
- **no outgoing / no incoming**: e.g. at N = 300, seed 1000, pairs 118, 122, 144 and 235 have an AB donor with no outgoing link (an AB donor gives only to AB patients), so no swap of any length can include them; only an altruistic donor's chain could.
- **options dead**: every swap the patient had was killed by failed crossmatches.

## Graph data model
One graph per pool (default name `pool`). Synthetic pools are generated from a seed and loaded with `swap.graph.load_pool`.

Nodes
- `(:Pair {id, patient_abo, donor_abo, pra, hard, committed})`: one incompatible donor/patient pair. `pra` ∈ {0.05, 0.45, 0.90}, `hard` = `pra == 0.90`, `committed` = the approved plan holding it (absent otherwise).
- `(:Cycle {key, len, members, hard, back_arcs, live, taken})`: one 2- or 3-way swap. `members[i]`'s donor gives to `members[i+1]`'s patient (the last to the first), rotated so the smallest id is first; `key` is the same ids joined by `>`. `back_arcs` counts reverse arcs inside a 3-way swap (Manlove & O'Malley, *Paired and altruistic kidney donation in the UK*, J. Exp. Algorithmics 2014: a back-arc keeps a 2-way swap alive if the third pair drops out). `live` = no failed crossmatch on its arcs; `taken` = a member is committed.
- `(:Plan {id, kind, status, created_at, max_len, priority, matched, swaps, decided_by, decided_at})`: `kind` is `plan` or `repair`; `status` is `draft`, `approved` or `rejected`.
- `(:Crossmatch {id, donor_pair, patient_pair, result, recorded_by, recorded_at})`: one lab result.
- Flow network for `algo.maxFlow`: `(:S)`, `(:Donor {id})`, `(:Patient {id})`, `(:T)`. `(:Altruist {id, donor_abo})` exists only in a what-if copy.

Relationships
- `(:Pair)-[:CAN_GIVE]->(:Pair)`: the donor of the left pair can give to the patient of the right pair (ABO-compatible and the crossmatch passes at the patient's PRA). A positive crossmatch deletes it.
- `(:Cycle)-[:HAS {pos}]->(:Pair)`; `(:Plan)-[:SELECTS]->(:Cycle)`; `(:Plan)-[:PROPOSES {swap, pos}]->(:Pair)`.
- `(:Crossmatch)-[:DONOR]->(:Pair)`, `(:Crossmatch)-[:PATIENT]->(:Pair)`, `(:Crossmatch)-[:BROKE]->(:Cycle)`.
- `(:Plan)-[:REPAIRS]->(:Plan)`, `(:Plan)-[:AFTER]->(:Crossmatch)`.
- `(:S)-[:F {cap:1}]->(:Donor)-[:F {cap:1}]->(:Patient)-[:F {cap:1}]->(:T)`.

## The Cypher and algorithms the product depends on
All Cypher is human-written and parameterised, kept as module constants in `src/swap/graph.py`, `tools.py`, `failure.py`, `explain.py` and `whatif.py`, and listed by `GET /cypher`. **The model never writes Cypher**: it chooses a tool and fills typed arguments. Entity ids are resolved against the graph before any other query runs. `tests/test_readme.py` fails if any line below differs from the code.

### Every swap becomes a graph object (`graph.py`, at load)
```
MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(a) WHERE a.id < b.id CREATE (c:Cycle {key: toString(a.id) + '>' + toString(b.id), len: 2, members: [a.id, b.id], hard: (CASE WHEN a.hard THEN 1 ELSE 0 END) + (CASE WHEN b.hard THEN 1 ELSE 0 END), back_arcs: 0, live: true, taken: false}) CREATE (c)-[:HAS {pos: 0}]->(a), (c)-[:HAS {pos: 1}]->(b) RETURN count(c)
MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(a) WHERE a.id < b.id AND a.id < c.id OPTIONAL MATCH (b)-[r1:CAN_GIVE]->(a) OPTIONAL MATCH (c)-[r2:CAN_GIVE]->(b) OPTIONAL MATCH (a)-[r3:CAN_GIVE]->(c) WITH a, b, c, (CASE WHEN r1 IS NULL THEN 0 ELSE 1 END) + (CASE WHEN r2 IS NULL THEN 0 ELSE 1 END) + (CASE WHEN r3 IS NULL THEN 0 ELSE 1 END) AS back CREATE (cy:Cycle {key: toString(a.id) + '>' + toString(b.id) + '>' + toString(c.id), len: 3, members: [a.id, b.id, c.id], hard: (CASE WHEN a.hard THEN 1 ELSE 0 END) + (CASE WHEN b.hard THEN 1 ELSE 0 END) + (CASE WHEN c.hard THEN 1 ELSE 0 END), back_arcs: back, live: true, taken: false}) CREATE (cy)-[:HAS {pos: 0}]->(a), (cy)-[:HAS {pos: 1}]->(b), (cy)-[:HAS {pos: 2}]->(c) RETURN count(cy)
```

### The planner reads, FalkorDB guards, an approval takes (`tools.py`)
```
MATCH (c:Cycle) WHERE c.live AND NOT c.taken AND c.len <= $max_len RETURN c.key, c.members, c.hard, c.back_arcs ORDER BY c.len, c.members
UNWIND $keys AS k MATCH (c:Cycle {key: k}) UNWIND c.members AS m RETURN count(DISTINCT c), count(m), count(DISTINCT m), sum(CASE WHEN c.live THEN 0 ELSE 1 END), sum(CASE WHEN c.taken THEN 1 ELSE 0 END)
UNWIND $keys AS k MATCH (c:Cycle {key: k}) UNWIND range(0, c.len - 1) AS i WITH c.members[i] AS a, c.members[(i + 1) % c.len] AS b MATCH (x:Pair {id: a}), (y:Pair {id: b}) OPTIONAL MATCH (x)-[r:CAN_GIVE]->(y) RETURN count(*), sum(CASE WHEN r IS NULL THEN 1 ELSE 0 END)
MATCH (pl:Plan {id: $plan}) UNWIND $keys AS k MATCH (c:Cycle {key: k}) CREATE (pl)-[:SELECTS]->(c) RETURN count(*)
MATCH (:Plan {id: $id})-[:SELECTS]->(c:Cycle) RETURN count(c), sum(CASE WHEN c.live THEN 0 ELSE 1 END), sum(CASE WHEN c.taken THEN 1 ELSE 0 END)
MATCH (:Plan {id: $id})-[:SELECTS]->(c:Cycle) UNWIND c.members AS m MATCH (p:Pair {id: m}) SET p.committed = $id RETURN collect(p.id)
MATCH (c:Cycle) WHERE NOT c.taken AND any(m IN c.members WHERE m IN $ids) SET c.taken = true RETURN count(c)
```

### When a crossmatch fails (`failure.py`)
```
MATCH (a:Pair {id: $a})-[x:CAN_GIVE]->(b:Pair {id: $b}) CREATE (t:Crossmatch {id: $tid, donor_pair: $a, patient_pair: $b, result: $result, recorded_by: $by, recorded_at: $at}) CREATE (t)-[:DONOR]->(a), (t)-[:PATIENT]->(b) RETURN t.id
MATCH (t:Crossmatch {id: $tid}) MATCH (a:Pair {id: $a})<-[h1:HAS]-(c:Cycle)-[h2:HAS]->(b:Pair {id: $b}) WHERE c.live AND h2.pos = (h1.pos + 1) % c.len SET c.live = false CREATE (t)-[:BROKE]->(c) RETURN count(c)
MATCH (a:Pair {id: $a})-[x:CAN_GIVE]->(b:Pair {id: $b}) DELETE x RETURN count(*)
MATCH (d:Donor {id: $a})-[f:F]->(p:Patient {id: $b}) DELETE f RETURN count(*)
MATCH (t:Crossmatch {id: $tid})-[:BROKE]->(c:Cycle)<-[:SELECTS]-(pl:Plan {status: 'approved'}) MATCH (c)-[:HAS]->(p:Pair) WHERE p.committed = pl.id SET p.committed = null RETURN collect(p.id)
MATCH (c:Cycle) WHERE any(m IN c.members WHERE m IN $ids) UNWIND c.members AS m MATCH (p:Pair {id: m}) WITH c, sum(CASE WHEN p.committed IS NULL THEN 0 ELSE 1 END) AS held SET c.taken = held > 0 RETURN count(c), sum(CASE WHEN held = 0 THEN 1 ELSE 0 END)
MATCH (t:Crossmatch {id: $tid})-[:BROKE]->(c:Cycle)<-[:SELECTS]-(pl:Plan) RETURN pl.id, pl.status, pl.kind, c.key, c.members ORDER BY pl.id, c.key
MATCH (t:Crossmatch {id: $tid})-[:BROKE]->(c:Cycle {len: 3})<-[:SELECTS]-(:Plan {status: 'approved'}) MATCH (c)-[:HAS]->(x:Pair)<-[:HAS]-(r:Cycle {len: 2})-[:HAS]->(y:Pair)<-[:HAS]-(c) WHERE x.id < y.id AND r.live AND NOT r.taken RETURN c.key, r.key, r.members ORDER BY r.key
MATCH (c:Cycle) WHERE c.live AND NOT c.taken AND c.len <= $max_len AND any(m IN c.members WHERE m IN $ids) RETURN c.key, c.members, c.hard, c.back_arcs ORDER BY c.len, c.members
MATCH (r:Plan {id: $repair}), (t:Crossmatch {id: $tid}) CREATE (r)-[:AFTER]->(t) WITH r UNWIND $plans AS pid MATCH (old:Plan {id: pid}) CREATE (r)-[:REPAIRS]->(old) RETURN count(*)
```
`BREAK_CYCLES` reads "b sits right after a in the ring": `h2.pos = (h1.pos + 1) % c.len`.

### Why a patient is left out (`explain.py`)
```
MATCH (x:Pair {id: $id})<-[:HAS]-(opt:Cycle) WHERE opt.live UNWIND opt.members AS m WITH opt, m WHERE m <> $id MATCH (chosen:Cycle) WHERE chosen.key IN $keys AND m IN chosen.members RETURN chosen.key, chosen.members, count(DISTINCT opt) AS blocked ORDER BY blocked DESC, chosen.key LIMIT 5
MATCH (x:Pair {id: $id})-[:CAN_GIVE]->(y:Pair) WITH x, y, shortestPath((y)-[:CAN_GIVE*..6]->(x)) AS p WHERE p IS NOT NULL RETURN [x.id] + [n IN nodes(p) | n.id] AS ring ORDER BY size(ring), ring LIMIT 1
MATCH (x:Pair {id: $id}), (y:Pair {id: $next}) CALL algo.SPpaths({sourceNode: y, targetNode: x, relTypes: ['CAN_GIVE'], maxLen: $max_hops, pathCount: 3}) YIELD path RETURN [x.id] + [n IN nodes(path) | n.id] AS ring ORDER BY size(ring), ring
MATCH (p:Pair {id: $id}) OPTIONAL MATCH (p)-[o:CAN_GIVE]->(:Pair) WITH p, count(o) AS out OPTIONAL MATCH (:Pair)-[i:CAN_GIVE]->(p) RETURN out, count(i)
```

### What-ifs on a copy FalkorDB makes (`whatif.py`)
`what_if` runs `GRAPH.COPY` on the active graph, changes the copy with Cypher, re-runs the same tools on it, diffs and drops it; the pool never leaves the database (a test makes any Python read-back or reload raise).
```
MATCH (c:Cycle) WHERE $id IN c.members DETACH DELETE c RETURN count(c)
MATCH (p:Pair {id: $id}) DETACH DELETE p RETURN count(*)
MATCH (p:Pair {id: $id}) WHERE NOT p.hard SET p.hard = true RETURN count(p)
MATCH (c:Cycle) WHERE $id IN c.members SET c.hard = c.hard + 1 RETURN count(c)
```

### Cycles by pattern match, and the max-flow bound (`tools.py`)
```
MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(a) WHERE a.id < b.id RETURN a.id, b.id ORDER BY a.id, b.id
MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(a) WHERE a.id < b.id AND a.id < c.id RETURN a.id, b.id, c.id ORDER BY a.id, b.id, c.id
MATCH (a:Pair)-[:CAN_GIVE]->(b:Pair)-[:CAN_GIVE]->(c:Pair)-[:CAN_GIVE]->(d:Pair)-[:CAN_GIVE]->(a) WHERE a.id < b.id AND a.id < c.id AND a.id < d.id AND b.id <> d.id RETURN a.id, b.id, c.id, d.id ORDER BY a.id, b.id, c.id, d.id
MATCH (s:S),(t:T) CALL algo.maxFlow({sourceNodes:[s],targetNodes:[t],relationshipTypes:['F'],capacityProperty:'cap'}) YIELD maxFlow, nodes, edges, edgeFlows RETURN maxFlow, nodes, edges, edgeFlows
```
`a.id < b.id` (and `a.id < c.id`, …) canonicalises each directed cycle to its smallest id; in the 4-cycle `b.id <> d.id` rejects the walk `a→b→c→b→a`. 4-way swaps are counted but not stored as swap objects (and plans use 2- and 3-way swaps only).

### The solvers (`src/swap/solve.py`)
- **Best set** = vertex-disjoint packing of the graph's live swaps maximising covered patients, `scipy.optimize.milp`, with the per-swap weights FalkorDB stored.
- **True unbounded cycle optimum** = maximum cycle cover of any length, an **assignment problem with self-loops** (`scipy.optimize.linear_sum_assignment`).

## How `algo.maxFlow` is used, and why it is only an upper bound
`flow_bound` reads `algo.maxFlow` over the unit-capacity network `S → donors → patients → T`, which equals a **maximum bipartite matching** of donors to patients: a relaxation that also permits **chains** from a donor with no patient. So it is an **upper bound that also allows chains, NOT the cycle optimum**. The invariant the tests pin: `2-way ≤ up-to-3 ≤ true cycle cover ≤ max-flow bound`.

## The tool catalogue and the four outcomes
The agent's catalogue (`swap/agent.py`) has nine typed tools; the MCP server (`python -m swap.mcp_server`) adds `generate_pool`, `get_plan`, `list_plans` and `list_crossmatches`.

| tool | what it answers |
|---|---|
| `find_cycles(max_len)` | swaps of length 2..max_len, from the pattern match |
| `flow_bound()` | the `algo.maxFlow` bound |
| `best_set(max_len, priority?)` | the disjoint swaps maximising patients, from the stored swaps |
| `true_cycle_optimum()` | maximum cycle cover of any length |
| `explain_unmatched(pair_id, plan_id?)` | the verdict and graph evidence above |
| `what_if(kind, …)` | add an altruist, remove a pair, or set a priority, on a `GRAPH.COPY` |
| `draft_plan(max_len, priority?)` | a guarded **draft** plan in the graph; it cannot approve |
| `failure_impact(crossmatch_id?)` | what a recorded result broke, who is stranded, the 2-way rescue, the options left |
| `draft_repair(crossmatch_id?, max_len)` | a guarded **draft** repair over the swaps still alive |

Human-only, behind the web UI and HTTP only: `POST /plans/{id}/decision` (approve or reject, once, with a name) and `POST /crossmatch` (record a lab result).

Every tool returns the four-outcome **envelope** `{outcome, result, path, trace:{tool, args, ms, rows}}`, and graph-heavy tools add `result.graph_work`: each statement they ran, with its time and count.
- `answered_with_path`: the answer plus the swaps, rings or arcs that produced it.
- `from_memory`: reserved for dated recalls (not used by the current tools).
- `not_in_graph`: the traversal came back empty. This means **"the graph does not know"**, never "safe".
- `unknown_entity`: an id the caller named is not in the graph; returned before any other query runs.

## The agent
`run_agent(question)` returns `{answer, tool_calls, rejected_ids, mode}`.
- **LLM mode** (`mode: "llm"`): any OpenAI-compatible or Azure OpenAI endpoint from `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (and `LLM_PROVIDER`, `LLM_API_VERSION`). The model only chooses tools from the catalogue and fills typed arguments; tool results are the envelopes, fed back verbatim. Verified live on 17 Oct 2026 against an Azure OpenAI deployment: asked "Why is pair 5 not matched, and how many 3-way swaps exist?", the model called `explain_unmatched` and `find_cycles` on its own.
- **Tool mode** (`mode: "tool"`): with no LLM configured, a deterministic keyword router answers (cycles, matched, explain pair N, what if, bound, optimum, crossmatch, repair). It always works.
- **Sentence check**: pair ids named in the final answer that appear in **no tool result of the run** (and were not typed in the question) withhold the answer, listed in `rejected_ids`.

## Architecture
```
   synthetic generator ──► loader ──► ┌─────────────────────────────────────┐
                                      │ FalkorDB: pairs, CAN_GIVE, every    │
   question ─► agent loop ─► typed ──►│ 2-/3-way swap as :Cycle, plans,     │
               (LLM or router) tools  │ crossmatches, flow network          │
                     │                └───────────────┬─────────────────────┘
                     ▼                       human-written Cypher; GRAPH.COPY
   four-outcome envelope + graph_work         for what-ifs; algo.maxFlow,
                     │                        shortestPath, algo.SPpaths
                     ├─ MCP server   `python -m swap.mcp_server`
                     ├─ FastAPI      `make run`
                     └─ web UI       web/ (Cytoscape.js vendored)
   scipy milp picks among the graph's live swaps; humans approve plans and record lab results
```
Python/networkx oracles live only in tests (`tests/oracle.py`) and one labelled plain-Python check (`src/swap/crosscheck.py`) used by `/proof` and `make verify`. `src/` never imports networkx.

## Run it
Prerequisites: Python 3.12+ and Docker.

The whole stack in two containers (the app plus its own FalkorDB; only the app is published):
```bash
APP_PORT=8010 docker compose up -d --build  # app on http://127.0.0.1:8010; FalkorDB stays internal
curl -X POST localhost:8010/pool -H 'content-type: application/json' -d '{"n":120,"seed":7}'
curl localhost:8010/proof                    # the numbers above + the independent cross-check
docker compose down -v                        # stop the stack and drop its volume
```
At startup the app loads a default pool (N = 200, seed 1000) when the active graph is missing, and adds swap objects to an older graph that has none.

Local development against FalkorDB on the host (port 16400):
```bash
make up                      # start FalkorDB on 127.0.0.1:16400 (container swap-falkordb)
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
make test                    # pytest (graph tests skip cleanly if FalkorDB is down)
make failure-story           # the loop above, step by step
make graph-bench             # the "What FalkorDB does" table
make verify SEEDS=20         # the numbers table (seeds 1000-1019, ~5 min)
make verify-fairness FAIRNESS_SEEDS=20
make run                     # FastAPI + UI at http://127.0.0.1:8000
```
HTTP: `POST /pool {n ≤ 500, seed}`, `GET /tools/find_cycles`, `GET /tools/flow_bound`, `GET /tools/best_set`, `GET /tools/explain/{pair_id}?plan=`, `POST /tools/what_if`, `POST /tools/draft_plan`, `GET /plans`, `GET /plans/{id}`, `POST /plans/{id}/decision`, `GET /plans/{id}/packet.md`, `POST /crossmatch`, `GET /crossmatches`, `GET /crossmatches/{id}/impact`, `POST /crossmatches/{id}/repair`, `POST /ask`, `GET /proof`, `GET /graph`, `GET /trace`, `GET /cypher`.

Optional LLM endpoint (without it, `/ask` uses tool mode): `cp .env.example .env`, then fill `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`. Docker Compose passes the same `.env` to the app if it exists. A public demo caps model-backed answers at `LLM_DAILY_LIMIT` per day (default 200); after that `/ask` answers in tool mode and says so.

## How this compares with the literature
Ours is a synthetic re-implementation; the published figures below are **not ours**:
- Roth, Sönmez and Ünver, *Efficient Kidney Exchange*, AER 97(3), 2007, Table 2 (500 pools): at n = 100, 2-way matches 49.7, up to 3-way 59.7, up to 4-way 60.35, unconstrained 60.39. Our generator follows the same ABO/PRA mix and pool rule, omitting the wife-husband crossmatch adjustment, so our levels differ slightly while the shape matches.
- Saidman et al., *Transplantation* 81:773 (2006): on a real 45-patient set, 2-way allowed 8 pairs and adding 3-way allowed 11.
- Dickerson, Procaccia and Sandholm, *Failure-Aware Kidney Exchange*, Management Science 2019: matches failing after the algorithm commits to them; we react to such failures, we do not model failure probabilities.
- Manlove and O'Malley, J. Exp. Algorithmics 19 (2014): the UK scheme's optimality criteria, the first of which maximises "effective 2-cycles" (2-way swaps and 3-way swaps with a back-arc). We store and use back-arcs for the rescue; we do not implement the UK criteria hierarchy.

## Honest limits
- **Synthetic data only.** No real or personal data; decision support, not medical advice; no clinical validation.
- **Known result.** The 2-vs-3 swap effect is Roth, Sönmez and Ünver's (2007); we reproduce it.
- **The integer program decides the packing**, not the graph; the graph builds, scores, filters, guards and records what it chooses.
- **The max-flow figure is an upper bound that also allows chains.**
- **Storing every swap costs size**: 66,864 swap objects at N = 300 make FalkorDB's matrices larger, which roughly doubled `find_cycles(3)` (0.41 → 0.84 s) and `algo.maxFlow` (28 → about 110–130 ms). `POST /pool` is capped at 500 pairs for that reason.
- **Crossmatch results are entered by a person**; we do not model failure probabilities, and a repair only re-matches through stored 2- and 3-way swaps (no chains).
- **What-if chains are short**: the altruist what-if considers `altruist→p1` and `altruist→p1→p2` only.
- **`algo.SPpaths` alternatives are capped** at the shortest ring's own length to stay fast on a dense pool; a FalkorDB timeout lists no alternatives rather than failing.
- The UI loop (draft, approve, record a failed crossmatch, repair, explain a left-out patient) was driven in headless Chromium at desktop and phone width on 18 Oct 2026 with no console errors; there is no automated browser test in the repo.

## Tests
`make test` runs pytest over `tests/`, including `test_cycle_graph.py` (stored swaps equal the pattern match, same candidate order so the packing is unchanged, guard, approval, stale drafts), `test_failure.py` (a positive result kills exactly the swaps on that arc and the live swaps then equal the pattern match on the changed graph; release, rescue, repair, human-only recording), `test_explain.py` (every verdict on a hand-built pool; the memo is never stale), `test_whatif.py` (copy in the database, original untouched), plus the tool-vs-oracle, API, MCP, agent, edge-case, README-drift and performance tests.

## AI-use disclosure
See `AI_USE.md`. AI coding assistants were used throughout (research, implementation, tests, docs); the team is responsible for the graph model, the Cypher and the architecture, and every number here is reproduced by a command in this repository.
