# Swap: instructions for coding agents

Project: a kidney paired exchange decision-support agent on SYNTHETIC patients, built for Graph Hacks (WeMakeDevs x FalkorDB, 15-18 Oct 2026, Track 1 "Agents That Act on Connected Data"). Submission deadline 18 Oct 2026 23:59 IST. Not medical advice; never use real or personal data.

Read first: docs/EVIDENCE.md (verified facts, expected numbers, FalkorDB gotchas), docs/OVERNIGHT.md (work order), docs/PLAN.md (original plan; EVIDENCE.md wins where they differ), docs/RULES.md (event rules), README.md (public skeleton).

## Hard rules
1. FalkorDB is the primary graph database and must be on the request path of every tool. Python/networkx/numpy oracles live only in tests and docs/reference.
2. The LLM never writes Cypher. All Cypher is human-written in the tool layer, parameterised. Entity ids are resolved against the graph first.
3. Every tool answer carries its path/evidence and a trace; an empty traversal means "the graph does not know", never "safe".
4. Do not claim anything the repo does not reproduce. Honest limits are a feature (synthetic data, known result, external integer-program solver, max-flow bound allows chains).
5. Disclose AI assistance: append to AI_USE.md at each milestone; use the commit trailer below.

## Approval boundaries (inherited from the user's global rules)
Run ordinary project work without asking: tests, builds, linting, formatting, code generation, edits inside this repo, running the local FalkorDB container.
Ask for explicit approval before: adding/upgrading/removing dependencies beyond the pre-approved list below; intentionally deleting source/config/data/user files; privileged commands; pushing to remotes; discarding work or rewriting Git history. Generated artifacts, caches and temp files may be removed freely.
Pre-approved Python dependencies (approved by the user for this overnight run): falkordb, numpy, scipy, fastapi, uvicorn, httpx, pydantic, mcp (official Python SDK), openai (OpenAI-compatible client), python-dotenv, pytest, networkx (tests/oracle only), ruff. Pre-approved frontend: vendor `cytoscape.min.js` (one file) into `web/vendor/`; no npm build step. Anything else: write it under "Needs approval" in docs/STATUS.md and work around it.

## Git
- Identity for this repo is LOCAL config: user.name dhanalakshmi-D04, user.email dhanalakshmidgowda04@gmail.com. Before every commit run `git config user.name && git config user.email` and `git log -1 --format='%an <%ae>'` after. Never change global git config. Never push; there is no remote.
- docs/ is gitignored (private notes). README.md, AI_USE.md, AGENTS.md, src/, tests/, web/, Makefile, pyproject.toml are the public repo.

## Conventions
Python 3.12+ with `.venv` at the repo root, `src/swap/` package, `tests/` with pytest, FalkorDB on host port 16400 (container `swap-falkordb`), config from env (see `.env.example`), no secrets in git. Keep modules small and typed. Use the user's global guidance: match surrounding code, avoid needless comments.
