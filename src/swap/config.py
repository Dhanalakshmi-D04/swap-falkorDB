"""Environment-driven configuration (`.env` at the repo root is loaded when python-dotenv is present)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 16400
DEFAULT_GRAPH = "swap"
#: The graph a session's tools answer over: `generate_pool`/`POST /pool` load it, everything else reads it.
DEFAULT_ACTIVE_GRAPH = "pool"

#: Any OpenAI-compatible endpoint. All three must be set for the agent loop to call a model;
#: with any of them empty `run_agent` falls back to the deterministic tool mode.
LLM_BASE_URL_ENV = "LLM_BASE_URL"
LLM_API_KEY_ENV = "LLM_API_KEY"
LLM_MODEL_ENV = "LLM_MODEL"

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    def where(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """The optional OpenAI-compatible endpoint the agent loop uses (tool mode needs none)."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def where(self) -> str:
        return f"{self.base_url or 'unset'} model={self.model or 'unset'}"


def load_env(path: str | Path | None = None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(path) if path else _ENV_FILE, override=False)


def settings(env_file: str | Path | None = None) -> Settings:
    load_env(env_file)
    host = os.environ.get("FALKORDB_HOST", "").strip() or DEFAULT_HOST
    raw_port = os.environ.get("FALKORDB_PORT", "").strip()
    try:
        port = int(raw_port) if raw_port else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    return Settings(host=host, port=port)


def llm_settings(env_file: str | Path | None = None) -> LLMSettings:
    """Read the optional LLM endpoint from the environment; all three vars must be non-empty."""
    load_env(env_file)
    return LLMSettings(
        base_url=os.environ.get(LLM_BASE_URL_ENV, "").strip(),
        api_key=os.environ.get(LLM_API_KEY_ENV, "").strip(),
        model=os.environ.get(LLM_MODEL_ENV, "").strip(),
    )
