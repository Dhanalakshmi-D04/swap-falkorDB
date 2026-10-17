"""Environment-driven configuration (`.env` at the repo root is loaded when python-dotenv is present)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 16400
DEFAULT_GRAPH = "swap"
#: The graph a session's tools answer over: `generate_pool`/`POST /pool` load it, everything else reads it.
#: `SWAP_ACTIVE_GRAPH` overrides the name (the test suite sets it so it never touches the live pool).
DEFAULT_ACTIVE_GRAPH = os.environ.get("SWAP_ACTIVE_GRAPH", "").strip() or "pool"

#: Any OpenAI-compatible endpoint. All three must be set for the agent loop to call a model;
#: with any of them empty `run_agent` falls back to the deterministic tool mode.
LLM_BASE_URL_ENV = "LLM_BASE_URL"
LLM_API_KEY_ENV = "LLM_API_KEY"
LLM_MODEL_ENV = "LLM_MODEL"
LLM_PROVIDER_ENV = "LLM_PROVIDER"
LLM_API_VERSION_ENV = "LLM_API_VERSION"
#: The same settings under the names a shared `.env` may already use (`LLM_*` wins when both are set).
LLM_FALLBACK_ENV = {
    LLM_BASE_URL_ENV: "VISION_ENDPOINT",
    LLM_API_KEY_ENV: "VISION_API_KEY",
    LLM_MODEL_ENV: "VISION_MODEL",
    LLM_PROVIDER_ENV: "VISION_PROVIDER",
    LLM_API_VERSION_ENV: "VISION_API_VERSION",
}

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
    provider: str = ""
    api_version: str = ""

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
    """Read the optional LLM endpoint from the environment; url, key and model must be non-empty.

    `LLM_*` names win; the `VISION_*` names are accepted when the `LLM_*` one is unset.
    """
    load_env(env_file)

    def read(name: str) -> str:
        return (os.environ.get(name, "") or os.environ.get(LLM_FALLBACK_ENV[name], "")).strip()

    return LLMSettings(
        base_url=read(LLM_BASE_URL_ENV),
        api_key=read(LLM_API_KEY_ENV),
        model=read(LLM_MODEL_ENV),
        provider=read(LLM_PROVIDER_ENV).lower(),
        api_version=read(LLM_API_VERSION_ENV),
    )
