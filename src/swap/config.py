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

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    def where(self) -> str:
        return f"{self.host}:{self.port}"


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
