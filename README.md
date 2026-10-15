# Swap

**An agent that finds kidney-swap chains in a graph, shows the exact cycle behind every answer, and says "the graph does not know" when it does not.**

Graph Hacks 2026 (WeMakeDevs x FalkorDB) · Track 1: Agents That Act on Connected Data · work in progress

> **What this is not:** not medical advice, not a clinical tool, not trained on or connected to any real patient data. Every patient in this repository is synthetic. It illustrates decision support for a transplant coordinator.

## The idea in one picture (numbers filled in by `make verify`)
Patients with a willing but incompatible donor can swap donors in cycles. Allowing 3-way swaps instead of only 2-way swaps matches more patients. Same question, two swap lengths:

| pool | 2-way only | up to 3-way | any cycle length | max-flow bound (also allows chains) |
|---|---|---|---|---|
| _to be generated_ | | | | |

## Status
Early build. See `AI_USE.md` for how AI assistance is used.

## Graph data model
_TODO: nodes, relationships, indexes._

## The Cypher and algorithms the product depends on
_TODO: generated from the tool layer._

## Run it
_TODO: `make up && make run`, live demo link, demo video link._

## Honest limits
Synthetic data; the 2-way vs 3-way effect is a known result (Roth, Sönmez, Ünver 2007); choosing the best set of cycles is an integer program solved by SciPy, fed by cycles that FalkorDB finds; the max-flow figure is an upper bound that also allows chains; the generator omits spouse-pair crossmatch adjustments; no clinical validation.

## AI-use disclosure
See `AI_USE.md`.
