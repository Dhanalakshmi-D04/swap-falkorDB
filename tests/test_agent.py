"""M10 agent tests: the tool-calling loop, the sentence check, and tool mode.

No network and no FalkorDB: the OpenAI client is a scripted fake and the tool catalogue runs over a
`FakeTools` object that returns canned envelopes. What is pinned here: the model's tool call is
routed to the right tool with typed arguments, a final answer that names a pair id no tool result
supports is rejected, and with no LLM configured the keyword router still answers.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from swap.agent import (
    TOOL_ORDER,
    ToolCatalogue,
    check_ids,
    extract_pair_ids,
    route,
    run_agent,
    tool_schemas,
)
from swap.config import LLMSettings


def _env(tool: str, result: dict[str, Any], path: list[list[int]] | None = None) -> dict[str, Any]:
    return {
        "outcome": "answered_with_path",
        "result": result,
        "path": path or [],
        "trace": {"tool": tool, "args": {}, "ms": 0.5, "rows": len(path or [])},
    }


class FakeTools:
    """Canned SwapTools stand-in: the same methods, fixed envelopes, ids 0 and 1 on the path."""

    def find_cycles(self, max_len: int = 3) -> dict[str, Any]:
        return _env(
            "find_cycles",
            {"cycles": [[0, 1]], "counts": {2: 1, 3: 0}, "total": 1, "max_len": max_len},
            [[0, 1]],
        )

    def flow_bound(self) -> dict[str, Any]:
        return _env("flow_bound", {"flow_bound": 2, "matched": [[0, 1]]}, [[0, 1]])

    def best_set(self, max_len: int = 3, priority: float | None = None) -> dict[str, Any]:
        return _env(
            "best_set",
            {"matched": 2, "max_len": max_len, "counts": {"cycles": 1}, "priority_weight": priority},
            [[0, 1]],
        )

    def true_cycle_optimum(self) -> dict[str, Any]:
        return _env("true_cycle_optimum", {"unbounded_cycle_cover": 2}, [[0, 1]])

    def explain_unmatched(self, pair_id: int) -> dict[str, Any]:
        return _env(
            "explain_unmatched",
            {
                "pair": {"id": pair_id, "patient_abo": "A", "donor_abo": "B", "pra": 0.05},
                "incoming_candidates": [1],
                "outgoing_targets": [],
            },
            [[1, pair_id]],
        )

    def what_if(self, kind: str, args: dict[str, Any]) -> dict[str, Any]:
        return _env(
            "what_if",
            {"kind": kind, "before": {"matched": 2}, "after": {"matched": 2}, "gain": 0},
            [],
        )


def catalogue() -> ToolCatalogue:
    return ToolCatalogue(FakeTools())


# --- the fake OpenAI client -----------------------------------------------------------------


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


def tool_call(name: str, arguments: str, call_id: str = "call_1") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="", tool_calls=[_FakeToolCall(call_id, name, arguments)]
                )
            )
        ]
    )


def final(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))]
    )


class FakeClient:
    """Replays a script of responses; ignores the request (no network) but records it."""

    def __init__(self, scripted: list[Any]) -> None:
        self._scripted = list(scripted)
        self.requests: list[dict[str, Any]] = []

    @property
    def chat(self) -> FakeClient:
        return self

    @property
    def completions(self) -> FakeClient:
        return self

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if not self._scripted:
            raise AssertionError("FakeClient ran out of scripted responses")
        return self._scripted.pop(0)


# --- schema generation ----------------------------------------------------------------------


def test_schemas_are_generated_from_typed_signatures() -> None:
    schemas = tool_schemas(catalogue())
    assert [schema["function"]["name"] for schema in schemas] == list(TOOL_ORDER)

    by_name = {schema["function"]["name"]: schema for schema in schemas}
    find_cycles = by_name["find_cycles"]["function"]["parameters"]
    assert find_cycles["properties"]["max_len"]["enum"] == [2, 3, 4]
    assert find_cycles["properties"]["max_len"]["type"] == "integer"
    assert "required" not in find_cycles  # max_len has a default

    explain = by_name["explain_unmatched"]["function"]["parameters"]
    assert explain["required"] == ["pair_id"]
    assert explain["properties"]["pair_id"]["type"] == "integer"

    what_if = by_name["what_if"]["function"]["parameters"]
    assert what_if["required"] == ["kind"]
    assert what_if["properties"]["kind"]["enum"] == [
        "add_altruistic_donor",
        "remove_pair",
        "set_priority",
    ]
    assert what_if["properties"]["donor_abo"]["enum"] == ["O", "A", "B", "AB"]
    assert what_if["properties"]["weight"]["type"] == "number"
    assert set(what_if["properties"]) == {"kind", "donor_abo", "pair_id", "weight", "max_len"}


def test_every_tool_schema_is_a_function_object() -> None:
    for schema in tool_schemas(catalogue()):
        assert schema["type"] == "function"
        assert schema["function"]["description"]
        assert schema["function"]["parameters"]["type"] == "object"


# --- the tool-calling loop ------------------------------------------------------------------


def test_model_tool_call_is_routed_with_typed_arguments() -> None:
    client = FakeClient([tool_call("find_cycles", '{"max_len": 3}'), final("Two cycles, ids 0 and 1.")])
    result = run_agent(
        "how many swap cycles are there?",
        catalogue=catalogue(),
        client=client,
        llm=LLMSettings(model="fake"),
    )

    assert result["mode"] == "llm"
    assert result["rejected_ids"] == []
    assert result["tool_calls"][0]["tool"] == "find_cycles"
    assert result["tool_calls"][0]["outcome"] == "answered_with_path"
    assert result["tool_calls"][0]["args"] == {"max_len": 3}
    # the second request carried the tool result back to the model
    assert len(client.requests) == 2
    tool_messages = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert tool_messages and "cycles" in tool_messages[0]["content"]


def test_llm_loop_passes_the_generated_tool_schemas() -> None:
    client = FakeClient([final("Nothing to add.")])
    run_agent("hello", catalogue=catalogue(), client=client, llm=LLMSettings(model="fake"))
    sent = client.requests[0]["tools"]
    assert [tool["function"]["name"] for tool in sent] == list(TOOL_ORDER)


def test_hallucinated_pair_id_is_rejected() -> None:
    client = FakeClient(
        [
            tool_call("find_cycles", "{}"),
            final("Pair 99999 can donate to pair 0, so both are matched."),
        ]
    )
    result = run_agent(
        "look at the cycles", catalogue=catalogue(), client=client, llm=LLMSettings(model="fake")
    )

    assert result["rejected_ids"] == [99999]
    assert result["answer"].startswith("[rejected]")
    assert "99999" in result["answer"]


def test_answer_with_only_graph_ids_is_accepted() -> None:
    client = FakeClient(
        [tool_call("find_cycles", "{}"), final("Pairs 0 and 1 form a 2-cycle, e.g. [0, 1].")]
    )
    result = run_agent(
        "which pairs cycle", catalogue=catalogue(), client=client, llm=LLMSettings(model="fake")
    )
    assert result["rejected_ids"] == []
    assert not result["answer"].startswith("[rejected]")


def test_counts_in_prose_are_not_read_as_pair_ids() -> None:
    client = FakeClient(
        [tool_call("best_set", "{}"), final("70 patients are matched in 28 disjoint cycles.")]
    )
    result = run_agent(
        "who can be matched", catalogue=catalogue(), client=client, llm=LLMSettings(model="fake")
    )
    assert result["rejected_ids"] == []


def test_a_bad_tool_name_does_not_crash_the_loop() -> None:
    client = FakeClient([tool_call("drop_database", "{}"), final("I could not use that tool.")])
    result = run_agent("do something", catalogue=catalogue(), client=client, llm=LLMSettings(model="fake"))
    assert result["tool_calls"][0]["tool"] == "drop_database"
    assert result["tool_calls"][0]["outcome"] == "not_in_graph"


# --- tool mode (no LLM) ---------------------------------------------------------------------


def test_tool_mode_works_without_env() -> None:
    result = run_agent("who can be matched at up to 3-way?", catalogue=catalogue(), llm=LLMSettings())
    assert result["mode"] == "tool"
    assert result["rejected_ids"] == []
    assert result["tool_calls"][0]["tool"] == "best_set"
    assert "matches 2 patients" in result["answer"]


def test_tool_mode_routes_each_intent() -> None:
    assert route("how many cycles are there?")[0][0] == "find_cycles"
    assert route("what is the max-flow bound?")[0] == ("flow_bound", {})
    assert route("explain pair 4")[0] == ("explain_unmatched", {"pair_id": 4})
    assert route("what if I add an altruistic O donor?")[0][0] == "what_if"
    assert route("what if I add an altruistic O donor?")[0][1]["kind"] == "add_altruistic_donor"
    assert route("remove pair 3 and re-run")[0][1]["kind"] == "remove_pair"
    assert route("what is the true cycle optimum?")[0][0] == "true_cycle_optimum"
    assert route("who can be matched?")[0][0] == "best_set"
    # two lengths named -> both are asked, so the answer can compare them
    compare = route("who can be matched at 2-way vs up to 3-way?")
    assert [call for call, _ in compare] == ["best_set", "best_set"]
    assert [args["max_len"] for _, args in compare] == [2, 3]


def test_tool_mode_answers_each_route() -> None:
    for question in (
        "how many cycles are there?",
        "what is the max-flow bound?",
        "explain pair 1",
        "what if I add an altruistic O donor?",
        "who can be matched?",
    ):
        result = run_agent(question, catalogue=catalogue(), llm=LLMSettings())
        assert result["mode"] == "tool"
        assert result["answer"], question
        assert result["rejected_ids"] == [], question
        assert result["tool_calls"], question


# --- the sentence check itself --------------------------------------------------------------


def test_extract_pair_ids_patterns() -> None:
    assert extract_pair_ids("pair 17 and #18 and pairs 19") == [17, 18, 19]
    assert extract_pair_ids("the cycles are [3, 4, 5] and [0, 1]") == [0, 1, 3, 4, 5]
    assert extract_pair_ids("70 patients matched in 28 cycles") == []


def test_check_ids_flags_only_unknown_ids() -> None:
    known = {0, 1}
    assert check_ids("pair 0 and pair 1 are matched", known) == []
    assert check_ids("pair 7 can donate", known) == [7]
    assert check_ids("pair 0 and pair 9", known) == [9]
