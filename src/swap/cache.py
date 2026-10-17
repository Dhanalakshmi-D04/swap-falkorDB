"""A small in-process memo of best-set answers, keyed by a fingerprint FalkorDB computes.

The integer program takes seconds on a large pool; the fingerprint (counts and sums over the stored
swaps and pairs) takes milliseconds and changes whenever anything the packing reads changes: a pool
reload, a failed crossmatch (fewer live swaps), an approval (more taken swaps), a priority change
(the stored hard counts) or an altruist. Loading or dropping a graph forgets its entries.
"""

from __future__ import annotations

from typing import Any

LIMIT = 64

_entries: dict[tuple[Any, ...], dict[str, Any]] = {}


def get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    return _entries.get(key)


def put(key: tuple[Any, ...], value: dict[str, Any]) -> None:
    if len(_entries) >= LIMIT:
        _entries.pop(next(iter(_entries)))
    _entries[key] = value


def forget(graph_name: str) -> None:
    for key in [key for key in _entries if key[0] == graph_name]:
        _entries.pop(key, None)
