"""README drift guard: what the README quotes must be what the code runs.

The README's "Cypher and algorithms" section quotes Cypher verbatim and claims it lives in
`src/swap/tools.py`, `graph.py`, `failure.py`, `explain.py` and `whatif.py`. This test extracts every Cypher-looking line from
every fenced block in `README.md` and asserts each one is exactly a Cypher constant in the code
(whitespace-normalised), so a README that drifts from the Cypher fails here. It also pins the
honest-framing wording: no "theoretical limit", and "safe" appears only in the sentence that says
an empty traversal is not a claim of safety.
"""

from __future__ import annotations

import re
from pathlib import Path

from swap import envelope, explain, failure, graph, tools, whatif

README = Path(__file__).resolve().parents[1] / "README.md"
CYPHER_STARTS = ("MATCH", "CREATE", "UNWIND", "MERGE", "CALL", "WITH", "RETURN")
SAFE_MARKERS = ("never", "does not know", "not the same as safe", "not safe", "not a claim")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _is_cypher(text: str) -> bool:
    return text.lstrip().upper().startswith(CYPHER_STARTS)


def _constants(value: object) -> set[str]:
    """Every Cypher string reachable from a module-level constant (str, tuple/list/set, dict)."""
    if isinstance(value, str):
        return {_norm(value)} if _is_cypher(value) else set()
    if isinstance(value, dict):
        found: set[str] = set()
        for item in value.values():
            found |= _constants(item)
        return found
    if isinstance(value, (list, tuple, set, frozenset)):
        found = set()
        for item in value:
            found |= _constants(item)
        return found
    return set()


def _module_constants(*modules: object) -> set[str]:
    found: set[str] = set()
    for module in modules:
        for name, value in vars(module).items():
            if name.isupper() and not name.startswith("_"):
                found |= _constants(value)
    return found


def _readme_cypher(readme: str) -> list[tuple[str, str]]:
    """(raw line, normalised line) for every Cypher-looking line inside a fenced block."""
    statements: list[tuple[str, str]] = []
    for block in re.findall(r"```[^\n]*\n(.*?)```", readme, flags=re.DOTALL):
        for line in block.splitlines():
            stripped = line.strip()
            if stripped and _is_cypher(stripped):
                statements.append((stripped, _norm(stripped)))
    return statements


def test_readme_quotes_the_code_cypher_verbatim() -> None:
    known = _module_constants(tools, graph, envelope, failure, explain, whatif)
    statements = _readme_cypher(README.read_text(encoding="utf-8"))

    assert statements, "expected the README to quote some Cypher"
    unknown = [raw for raw, norm in statements if norm not in known]
    assert not unknown, "README Cypher not found as a constant in tools.py/graph.py:\n" + "\n".join(
        unknown
    )


def test_readme_has_no_theoretical_limit_claim() -> None:
    text = README.read_text(encoding="utf-8").lower()
    assert "theoretical limit" not in text
    assert "within k of" not in text


def test_readme_uses_safe_only_in_the_not_safe_sentence() -> None:
    readme = README.read_text(encoding="utf-8")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", readme) if re.search(r"\bsafe\b", s)]
    assert sentences, "expected the README to warn that an empty traversal is not 'safe'"
    for sentence in sentences:
        assert any(marker in sentence.lower() for marker in SAFE_MARKERS), sentence
