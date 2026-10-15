# AI-use disclosure (Graph Hacks rules require this)

AI coding assistants (Anthropic Claude via Claude Code) are used throughout this project for research, planning, implementation, tests and documentation. The team members are responsible for the code, can explain the graph model, queries and architecture, and verify results with the reproducible checks in this repository (`make verify`, `make test`).

Log of assistance (append one entry per milestone; newest last):

- 2026-10-15: Research and fact-check of the event rules, the idea and the published literature; independent reference implementation used only as a cross-check oracle (not part of the product). Repo scaffold and documentation written with AI assistance. Human to verify: all claims in the README against `make verify` output.
- 2026-10-15 (night): M0-M2 (scaffold, synthetic generator, FalkorDB graph loader) written by an AI coding agent (DeepSeek V4.1 Flash via Command Code), supervised and reviewed by Claude Code. Human to verify: generator follows Roth/Sonmez/Unver rule, `make test` passes against a local FalkorDB.
- 2026-10-15 (night): M3/M4/M6 (four-outcome envelope, FalkorDB graph tools, MILP cycle packing, independent test oracle) written by DeepSeek V4.1 Flash via Command Code, supervised by Claude Code. Human to verify: Cypher in src/swap/tools.py, tests/oracle.py independence, numbers via `make test`.
- 2026-10-15 (night): M5 what-if, M7 MCP server, M8 FastAPI endpoints written by DeepSeek V4.1 Flash via Command Code, supervised by Claude Code. Human to verify: what_if semantics (altruist chains <= 2 hops), MCP catalogue, /proof numbers.
- 2026-10-15 (night): M9 single-page UI written by DeepSeek V4.1 Flash via Command Code, supervised by Claude Code. Human to verify: open the UI in a browser and click through each feature.
