"""M10: the agent loop - the model picks a tool from a fixed catalogue and fills typed arguments.

The model NEVER writes Cypher. Its OpenAI tool-calling schema is generated from the typed tool
signatures below (see `tool_schemas`), it may only choose one of those tools and supply typed
arguments, and those arguments run against the same FalkorDB tool layer the MCP server exposes.
Tool results are the four-outcome envelopes, fed back verbatim.

After the model's final answer is produced, a sentence check scans it for pair ids (`pair 17`,
`#17`, ids in lists) and rejects the answer if it names an id that appears in no tool result of
the run (ids the user typed in the question do not count as invented) - so an id the graph never
returned cannot reach the coordinator.

If no LLM endpoint is configured (`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` empty) the agent falls
back to a deterministic keyword router, "tool mode", which answers the same questions without a
model and is what the demo relies on.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from datetime import date
from types import NoneType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from swap.config import DEFAULT_ACTIVE_GRAPH, LLMSettings, llm_daily_limit, llm_settings
from swap.tools import SwapTools

TOOL_ORDER: tuple[str, ...] = (
    "find_cycles",
    "flow_bound",
    "best_set",
    "true_cycle_optimum",
    "explain_unmatched",
    "what_if",
    "draft_plan",
    "failure_impact",
    "draft_repair",
)

MODE_LLM = "llm"
MODE_TOOL = "tool"

SYSTEM_PROMPT = (
    "You answer questions about a kidney paired-exchange pool held in a FalkorDB graph, using "
    "only the tools provided. Never write Cypher or invent graph facts. Choose one tool, fill its "
    "typed arguments, and read the returned envelope. Mention a pair id only when a tool result "
    "of this run contains it. The max-flow bound is an upper bound that also allows chains, not "
    "the cycle optimum. Never call a patient safe or unsafe and never give medical or clinical "
    "judgments: report only what the tools returned. Synthetic patients only; this is decision "
    "support, not medical advice."
)

REJECTION = (
    "[rejected] this answer named pair id(s) {ids} that appear in no tool result of this run, so "
    "the graph does not support them; the answer is withheld."
)

# Sentence check: explicit "pair 17" / "#17" / ids in a bracketed list.
_ID_PREFIX_RE = re.compile(r"\b(?:pairs?|patients?|ids?|pair ids?)\s*#?\s*(\d+)", re.IGNORECASE)
_ID_HASH_RE = re.compile(r"#\s*(\d+)")
_ID_LIST_RE = re.compile(r"\[([^\[\]]*)\]")
_NUMBER_RE = re.compile(r"\d+")

# Tool mode: length mentions like "2-way", "up to 3", "cycle length 3".
_LENGTH_RE = re.compile(r"(?:\b([234])\s*-?\s*(?:way|cycle|cycles)\b|length\s*([234])\b|up to\s*([234])\b)")
_PAIR_RE = re.compile(r"\bpairs?\s*#?\s*(\d+)")
_SIMPLE_TYPES: dict[Any, str] = {int: "integer", float: "number", str: "string", bool: "boolean"}
_ABO_TYPES: tuple[str, ...] = ("O", "A", "B", "AB")


def _schema_for(annotation: Any) -> dict[str, Any]:
    """Map one typed annotation to a JSON-schema fragment (`{}` when it cannot be described)."""
    if annotation is Any or annotation is inspect.Parameter.empty:
        return {}
    origin = get_origin(annotation)
    if origin is UnionType or origin is Union:
        inner = [arg for arg in get_args(annotation) if arg is not NoneType]
        return _schema_for(inner[0]) if inner else {}
    if origin is Literal:
        values = list(get_args(annotation))
        kinds = {_SIMPLE_TYPES.get(type(value), "string") for value in values}
        return {"type": kinds.pop() if len(kinds) == 1 else "string", "enum": values}
    return {"type": _SIMPLE_TYPES[annotation]} if annotation in _SIMPLE_TYPES else {}


def _tool_schema(name: str, func: Any) -> dict[str, Any]:
    """One OpenAI function tool, its parameter schema generated from `func`'s typed signature."""
    hints = get_type_hints(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in inspect.signature(func).parameters.items():
        properties[param_name] = _schema_for(hints.get(param_name, param.annotation))
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": " ".join((func.__doc__ or "").split()),
            "parameters": parameters,
        },
    }


class ToolCatalogue:
    """Typed wrappers over `SwapTools`; their signatures generate the OpenAI tool schema.

    `SwapTools` methods carry no annotations, so the model-facing schema is generated from these
    annotated wrappers (the product logic stays in `SwapTools`). `dispatch` is the only executor.
    """

    def __init__(self, tools: SwapTools) -> None:
        self.tools = tools

    def find_cycles(self, max_len: Literal[2, 3, 4] = 3) -> dict[str, Any]:
        """Cycles of length 2..max_len, produced BY FalkorDB, as ordered pair-id lists and counts."""
        return self.tools.find_cycles(int(max_len))

    def flow_bound(self) -> dict[str, Any]:
        """The algo.maxFlow bound: an upper bound that also allows chains, NOT the cycle optimum."""
        return self.tools.flow_bound()

    def best_set(
        self, max_len: Literal[2, 3, 4] = 3, priority: float | None = None
    ) -> dict[str, Any]:
        """Choose vertex-disjoint swaps (cycles 2..max_len) maximising matched patients.

        `priority` above 1 weights highly sensitised (PRA 0.90) patients when choosing.
        """
        return self.tools.best_set(int(max_len), priority)

    def true_cycle_optimum(self) -> dict[str, Any]:
        """The unbounded cycle optimum (cycles of any length), for the proof panel."""
        return self.tools.true_cycle_optimum()

    def explain_unmatched(self, pair_id: int, plan_id: int | None = None) -> dict[str, Any]:
        """Why this pair is or is not in a swap: a verdict with graph evidence.

        Blocked by chosen swaps (named), a shortest ring longer than 3 pairs, a donor who can give
        to nobody, or swaps killed by failed crossmatches. Explained against `plan_id`, else the
        latest plan, else the best set. An unknown id answers outcome `unknown_entity`.
        """
        return self.tools.explain_unmatched(int(pair_id), None if plan_id is None else int(plan_id))

    def what_if(
        self,
        kind: Literal["add_altruistic_donor", "remove_pair", "set_priority"],
        donor_abo: Literal["O", "A", "B", "AB"] | None = None,
        pair_id: int | None = None,
        weight: float | None = None,
        max_len: Literal[2, 3, 4] = 3,
    ) -> dict[str, Any]:
        """Re-run the tools on a modified copy of the pool and report what changes."""
        args: dict[str, Any] = {"max_len": int(max_len)}
        if donor_abo is not None:
            args["donor_abo"] = donor_abo
        if pair_id is not None:
            args["pair_id"] = int(pair_id)
        if weight is not None:
            args["weight"] = float(weight)
        return self.tools.what_if(kind, args)

    def draft_plan(
        self, max_len: Literal[2, 3] = 3, priority: float | None = None
    ) -> dict[str, Any]:
        """Draft a swap plan for a coordinator and record it in the graph as a DRAFT.

        Uses only swaps still available (none killed by a failed crossmatch, no pair committed by an
        approved plan); FalkorDB checks the set before writing. You can only draft: approving or
        rejecting a plan is a human action you cannot perform.
        """
        return self.tools.draft_plan(int(max_len), priority)

    def failure_impact(self, crossmatch_id: int | None = None) -> dict[str, Any]:
        """What a recorded crossmatch result broke, from the graph (default: the latest result).

        Returns the swaps it killed, the planned swaps that broke, the stranded patients, the 2-way
        swap still alive inside a broken 3-way swap, and the options left. You cannot record a
        result: that is a human action.
        """
        return self.tools.failure_impact(self._crossmatch(crossmatch_id))

    def draft_repair(
        self, crossmatch_id: int | None = None, max_len: Literal[2, 3] = 3
    ) -> dict[str, Any]:
        """Draft a REPAIR plan after a failed crossmatch (default: the latest result).

        Uses only swaps still alive in the graph and re-matches the stranded patients first. It is
        a draft: approving it is a human action you cannot perform.
        """
        return self.tools.draft_repair(self._crossmatch(crossmatch_id), int(max_len))

    def _crossmatch(self, crossmatch_id: int | None) -> int:
        if crossmatch_id is not None:
            return int(crossmatch_id)
        tests = self.tools.list_crossmatches()["result"]["crossmatches"]
        return tests[-1]["id"] if tests else 0

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute one tool by name with already-parsed arguments."""
        if name not in TOOL_ORDER:
            raise ValueError(f"unknown tool {name!r}; expected one of {list(TOOL_ORDER)}")
        return getattr(self, name)(**args)


def tool_schemas(catalogue: ToolCatalogue) -> list[dict[str, Any]]:
    """The OpenAI `tools` payload, generated from the catalogue's typed signatures."""
    return [_tool_schema(name, getattr(catalogue, name)) for name in TOOL_ORDER]


# --- the sentence check ---------------------------------------------------------------------


def extract_pair_ids(text: str) -> list[int]:
    """Pair ids a sentence names: `pair 17`, `#17`, and ids inside a bracketed list."""
    found: set[int] = set()
    for match in _ID_PREFIX_RE.finditer(text):
        found.add(int(match.group(1)))
    for match in _ID_HASH_RE.finditer(text):
        found.add(int(match.group(1)))
    for match in _ID_LIST_RE.finditer(text):
        content = match.group(1)
        if "." in content:
            continue
        found.update(int(number) for number in _NUMBER_RE.findall(content))
    return sorted(found)


def _known_ids(env: dict[str, Any]) -> set[int]:
    """Pair ids a tool result actually puts on the record: its path, candidates and the pair."""
    ids: set[int] = set()
    for step in env.get("path") or []:
        ids.update(int(value) for value in step)
    result = env.get("result") or {}
    for key in ("incoming_candidates", "outgoing_targets"):
        values = result.get(key)
        if isinstance(values, list):
            ids.update(int(value) for value in values if isinstance(value, int))
    for key in ("still_waiting",):
        values = result.get(key)
        if isinstance(values, list):
            ids.update(int(value) for value in values if isinstance(value, int))
    for stranded in result.get("stranded") or []:
        if isinstance(stranded, dict) and isinstance(stranded.get("id"), int):
            ids.add(stranded["id"])
    test = result.get("crossmatch")
    if isinstance(test, dict):
        ids.update(test[key] for key in ("donor_pair", "patient_pair") if isinstance(test.get(key), int))
    pair = result.get("pair")
    if isinstance(pair, dict) and isinstance(pair.get("id"), int):
        ids.add(pair["id"])
    entity = result.get("entity")  # an id the graph was asked about and does not hold
    if isinstance(entity, dict) and isinstance(entity.get("id"), int):
        ids.add(entity["id"])
    return ids


def check_ids(text: str, known: set[int]) -> list[int]:
    """Ids mentioned in `text` that no tool result of the run supports."""
    return [pair_id for pair_id in extract_pair_ids(text) if pair_id not in known]


def _reject(answer: str, rejected: list[int]) -> str:
    return REJECTION.format(ids=", ".join(str(pair_id) for pair_id in rejected))


def _call_record(name: str, args: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
    trace = env.get("trace") or {}
    return {
        "tool": name,
        "args": dict(args),
        "ms": trace.get("ms", 0.0),
        "rows": trace.get("rows", 0),
        "outcome": env.get("outcome", ""),
    }


# --- the LLM loop ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Step:
    name: str
    args: dict[str, Any]
    env: dict[str, Any]


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _assistant_turn(message: Any, tool_calls: list[Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": getattr(message, "content", "") or "",
        "tool_calls": [
            {
                "id": getattr(call, "id", ""),
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments or "{}",
                },
            }
            for call in tool_calls
        ],
    }


def _error_envelope(name: str, args: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "outcome": "not_in_graph",
        "result": {"error": f"tool {name!r} failed: {exc}"},
        "path": [],
        "trace": {"tool": name, "args": dict(args), "ms": 0.0, "rows": 0},
    }


def _run_tool(catalogue: ToolCatalogue, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool call; any failure becomes an error envelope the caller can read."""
    try:
        return catalogue.dispatch(name, args)
    except Exception as exc:  # noqa: BLE001 - a bad tool name or args must not crash the loop
        return _error_envelope(name, args, exc)


DEFAULT_AZURE_API_VERSION = "2024-10-21"


def _api_root(url: str) -> str:
    """The API root for an OpenAI client: a pasted full route (`.../chat/completions`) is trimmed."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        path = path.removesuffix(suffix)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _build_client(cfg: LLMSettings) -> Any:
    """An OpenAI client, or an Azure OpenAI client when the endpoint is an Azure resource.

    The `/openai/v1` route of an Azure resource speaks the plain OpenAI protocol; any other Azure
    endpoint (including a pasted "target URI" with a deployment path) needs the Azure client, which
    takes the resource root and an api-version.
    """
    from urllib.parse import parse_qs, urlsplit

    from openai import AzureOpenAI, OpenAI

    parts = urlsplit(cfg.base_url)
    is_azure = cfg.provider.startswith("azure") or "azure" in parts.netloc.lower()
    if is_azure and "/openai/v1" not in parts.path:
        version = (
            cfg.api_version
            or (parse_qs(parts.query).get("api-version") or [""])[0]
            or DEFAULT_AZURE_API_VERSION
        )
        return AzureOpenAI(
            azure_endpoint=f"{parts.scheme}://{parts.netloc}",
            api_key=cfg.api_key,
            api_version=version,
        )
    return OpenAI(base_url=_api_root(cfg.base_url), api_key=cfg.api_key)


def _complete(client: Any, extra: dict[str, Any], **kwargs: Any) -> Any:
    """One chat completion. Some reasoning models refuse tools unless `reasoning_effort` is "none";
    when the server says so, switch it off once and remember that for the rest of the run."""
    try:
        return client.chat.completions.create(**kwargs, **extra)
    except Exception as exc:
        if "reasoning_effort" not in str(exc) or "reasoning_effort" in extra:
            raise
        extra["reasoning_effort"] = "none"
        return client.chat.completions.create(**kwargs, **extra)


def _run_llm(
    question: str,
    catalogue: ToolCatalogue,
    client: Any,
    model: str,
    max_steps: int,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    schemas = tool_schemas(catalogue)
    extra: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    known: set[int] = set(extract_pair_ids(question))  # an id you typed is not an invented id
    answer = ""

    for _ in range(max_steps):
        response = _complete(
            client, extra, model=model, messages=messages, tools=schemas, tool_choice="auto"
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            answer = (getattr(message, "content", "") or "").strip()
            break

        messages.append(_assistant_turn(message, tool_calls))
        for call in tool_calls:
            name = call.function.name
            args = _parse_arguments(call.function.arguments)
            env = _run_tool(catalogue, name, args)
            known |= _known_ids(env)
            calls.append(_call_record(name, args, env))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(call, "id", ""),
                    "name": name,
                    "content": json.dumps(env, default=str),
                }
            )
    else:
        answer = "I could not produce an answer from the graph within the step budget."

    rejected = check_ids(answer, known)
    if rejected:
        answer = _reject(answer, rejected)
    return {"answer": answer, "tool_calls": calls, "rejected_ids": rejected, "mode": MODE_LLM}


# --- tool mode (no LLM) ---------------------------------------------------------------------


def _mentioned_lengths(question: str) -> list[int]:
    lengths: set[int] = set()
    for match in _LENGTH_RE.finditer(question):
        lengths.update(int(group) for group in match.groups() if group)
    return sorted(lengths)


def _find_pair_id(question: str) -> int | None:
    match = _PAIR_RE.search(question)
    return int(match.group(1)) if match else None


def _find_donor_abo(question: str) -> str:
    for abo in _ABO_TYPES:
        if re.search(rf"\b{abo}\b", question):
            return abo
    return "O"


def route(question: str) -> list[tuple[str, dict[str, Any]]]:
    """Deterministic keyword -> tool route used when no LLM is configured."""
    text = question.lower()
    pair_id = _find_pair_id(text)
    lengths = _mentioned_lengths(text)
    max_len = lengths[-1] if lengths else 3

    if pair_id is not None and any(word in text for word in ("explain", "why", "unmatched")):
        return [("explain_unmatched", {"pair_id": pair_id})]
    if "repair" in text or "re-match" in text or "rematch" in text:
        return [("draft_repair", {"max_len": min(max_len, 3)})]
    if any(word in text for word in ("crossmatch", "cross-match", "failed", "stranded", "broke")):
        return [("failure_impact", {})]
    if any(word in text for word in ("draft", "sign-off", "sign off", "signoff")):
        return [("draft_plan", {"max_len": max_len})]
    if any(word in text for word in ("bound", "flow", "maxflow", "max flow")):
        return [("flow_bound", {})]
    if any(word in text for word in ("what if", "what-if", "whatif", "altruist", "remove", "priorit")):
        return [_what_if_route(text, pair_id, max_len)]
    if "optimum" in text or "cycle cover" in text:
        return [("true_cycle_optimum", {})]
    if any(word in text for word in ("cycle", "cycles", "swap")):
        return [("find_cycles", {"max_len": max_len})]
    matched = ("matched" in text or "match" in text or "who can be matched" in text
               or "how many" in text)
    if matched and len(lengths) > 1:
        return [("best_set", {"max_len": length}) for length in lengths]
    return [("best_set", {"max_len": max_len})]


def _what_if_route(text: str, pair_id: int | None, max_len: int) -> tuple[str, dict[str, Any]]:
    if "remove" in text:
        return ("what_if", {"kind": "remove_pair", "pair_id": pair_id or 0, "max_len": max_len})
    if "priorit" in text or "sensit" in text:
        return ("what_if", {"kind": "set_priority", "pair_id": pair_id or 0, "max_len": max_len})
    return (
        "what_if",
        {"kind": "add_altruistic_donor", "donor_abo": _find_donor_abo(text), "max_len": max_len},
    )


def summarize(name: str, env: dict[str, Any]) -> str:
    """A deterministic sentence from one envelope (tool mode's answer text)."""
    result = env.get("result") or {}
    outcome = env.get("outcome", "")
    if name == "find_cycles":
        counts = result.get("counts") or {}
        detail = ", ".join(f"{length}-cycles {count}" for length, count in sorted(counts.items()))
        return f"FalkorDB found {result.get('total', 0)} swap cycles ({detail}). Outcome: {outcome}."
    if name == "best_set":
        cycles = (result.get("counts") or {}).get("cycles", 0)
        return (
            f"at swap length up to {result.get('max_len')}, the best set matches "
            f"{result.get('matched', 0)} patients in {cycles} disjoint cycles. Outcome: {outcome}."
        )
    if name == "flow_bound":
        return (
            f"the max-flow bound is {result.get('flow_bound', 0)} patients; this is an upper bound "
            f"that also allows chains, not the cycle optimum. Outcome: {outcome}."
        )
    if name == "true_cycle_optimum":
        return (
            f"the unbounded cycle optimum is {result.get('unbounded_cycle_cover', 0)} patients. "
            f"Outcome: {outcome}."
        )
    if name == "explain_unmatched":
        pair = result.get("pair") or {}
        if not pair:
            return f"the graph does not hold that pair. Outcome: {outcome}."
        incoming = len(result.get("incoming_candidates") or [])
        outgoing = len(result.get("outgoing_targets") or [])
        return (
            f"pair {pair.get('id')} (patient {pair.get('patient_abo')}, donor "
            f"{pair.get('donor_abo')}): {incoming} donors could give to this patient and this pair's "
            f"donor could give to {outgoing} patients. {result.get('summary', '')} "
            f"Outcome: {outcome}."
        )
    if name == "draft_plan":
        plan = result.get("plan") or {}
        if not plan:
            return f"there is nothing left to plan: {result.get('note', '')}. Outcome: {outcome}."
        return (
            f"drafted plan {plan.get('id')}: {plan.get('swaps')} swaps covering "
            f"{plan.get('matched')} patients. It is only a draft until a person approves it. "
            f"Outcome: {outcome}."
        )
    if name == "failure_impact":
        test = result.get("crossmatch") or {}
        if not test:
            return f"the graph does not know of that crossmatch result. Outcome: {outcome}."
        broken = ", ".join(b["key"] for b in result.get("broken_planned_swaps") or []) or "none"
        waiting = ", ".join(str(v) for v in result.get("still_waiting") or []) or "nobody"
        rescue = ", ".join(r["rescue"] for r in result.get("rescue") or []) or "none"
        return (
            f"the {test.get('result')} crossmatch from donor of pair {test.get('donor_pair')} to "
            f"patient of pair {test.get('patient_pair')} killed {result.get('swaps_killed', 0)} "
            f"stored swaps; planned swaps broken: {broken}; still waiting: {waiting}; 2-way swap "
            f"still alive inside a broken 3-way: {rescue}. Outcome: {outcome}."
        )
    if name == "draft_repair":
        repair = result.get("repair") or {}
        plan = result.get("plan") or {}
        if not plan:
            return f"no repair was drafted: {result.get('note', '')}. Outcome: {outcome}."
        rematched = ", ".join(str(v) for v in repair.get("rematched") or []) or "nobody"
        waiting = ", ".join(str(v) for v in repair.get("still_waiting") or []) or "nobody"
        return (
            f"drafted repair plan {plan.get('id')}: re-matches {rematched}; still waiting: "
            f"{waiting}. It is only a draft until a person approves it. Outcome: {outcome}."
        )
    if name == "what_if":
        before = (result.get("before") or {}).get("matched")
        after = (result.get("after") or {}).get("matched")
        return f"after this change, matched patients go {before} to {after}. Outcome: {outcome}."
    return f"{name}: outcome {outcome}."


def run_tool_mode(question: str, catalogue: ToolCatalogue) -> dict[str, Any]:
    """Answer without a model: route by keyword, run the tools, summarize their envelopes."""
    calls: list[dict[str, Any]] = []
    known: set[int] = set(extract_pair_ids(question))
    sentences: list[str] = []
    for name, args in route(question):
        env = _run_tool(catalogue, name, args)
        known |= _known_ids(env)
        calls.append(_call_record(name, args, env))
        sentences.append(summarize(name, env))

    answer = " ".join(sentences)
    rejected = check_ids(answer, known)
    if rejected:
        answer = _reject(answer, rejected)
    return {"answer": answer, "tool_calls": calls, "rejected_ids": rejected, "mode": MODE_TOOL}


# --- entry point ----------------------------------------------------------------------------


def run_agent(
    question: str,
    *,
    catalogue: ToolCatalogue | None = None,
    client: Any | None = None,
    llm: LLMSettings | None = None,
    graph_name: str = DEFAULT_ACTIVE_GRAPH,
    max_steps: int = 6,
) -> dict[str, Any]:
    """Answer a question: a model picks a tool (or, with no LLM configured, a keyword router does).

    Returns `{answer, tool_calls, rejected_ids, mode}`. `tool_calls` are `{tool, args, ms, rows,
    outcome}` records; `rejected_ids` are pair ids the answer named that no tool result supports;
    `mode` is `"llm"` or `"tool"`.
    """
    catalogue = catalogue if catalogue is not None else ToolCatalogue(SwapTools(graph_name))
    cfg = llm if llm is not None else llm_settings()
    if client is None and not cfg.configured():
        return run_tool_mode(question, catalogue)
    if client is None and not _llm_budget_left():
        answer = run_tool_mode(question, catalogue)
        answer["note"] = "today's model budget for this demo is used up; answered in tool mode"
        return answer
    client = client if client is not None else _build_client(cfg)
    return _run_llm(question, catalogue, client, cfg.model, max_steps)


_llm_calls: dict[str, int] = {}


def _llm_budget_left() -> bool:
    """A public demo caps model-backed answers per day (`LLM_DAILY_LIMIT`, default 200)."""
    today = date.today().isoformat()
    used = _llm_calls.get(today, 0)
    if used >= llm_daily_limit():
        return False
    _llm_calls.clear()
    _llm_calls[today] = used + 1
    return True
