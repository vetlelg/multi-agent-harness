"""Every tunable value in the system, read once and validated once.

This module sits at the bottom of the dependency graph: everything imports it,
it imports nothing of ours. No logic, no I/O, no clients.

``os.getenv`` must not appear anywhere else in the repo. Reading the environment
at call time lets one test leak into the next, producing failures that depend on
the order tests happen to run in.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Providers the ``provider:model`` syntax accepts. A provider listed here but
#: without an adapter in ``core.llm`` fails later, at client construction, with
#: a message saying so.
PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai", "gemini", "ollama"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- provider credentials -------------------------------------------------
    # SecretStr so a stray repr or traceback cannot print the key. Values are
    # read from .env, which pydantic-settings does NOT export into os.environ,
    # so adapters must pass these explicitly rather than relying on the SDKs
    # picking them up from the environment.
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    ollama_host: str = "http://localhost:11434"

    # --- model per role, as "provider:model" ----------------------------------
    schema_model: str = "anthropic:claude-opus-5"
    sql_model: str = "anthropic:claude-opus-5"
    answer_model: str = "anthropic:claude-opus-5"

    # --- model parameters -----------------------------------------------------
    # Thinking is on by default on current Claude models, so a low ceiling
    # truncates the response rather than shortening it.
    max_tokens: int = 16000
    # Model calls are far slower than service calls; they get their own budget.
    llm_timeout_s: float = 120.0

    # --- pipeline behaviour ---------------------------------------------------
    # Number of executions permitted per run: 3 executions, 2 retries. Both
    # orchestrators read this, so they cannot disagree about the retry budget.
    max_attempts: int = 3
    row_limit: int = 50
    query_timeout_s: float = 5.0
    http_timeout_s: float = 30.0
    db_path: Path = Path("data/chinook.db")

    # --- service addresses ----------------------------------------------------
    executor_url: str = "http://localhost:8000"
    schema_agent_url: str = "http://localhost:8010"
    query_agent_url: str = "http://localhost:8020"
    orchestrator_scratch_url: str = "http://localhost:8100"
    orchestrator_lg_url: str = "http://localhost:8200"

    # --- observability --------------------------------------------------------
    #: When set, whole runs are appended to ``<runs_dir>/<run_id>.jsonl``.
    #: Left unset in the cluster, where ``kubectl logs`` is the source of truth.
    runs_dir: Path | None = None

    @field_validator("schema_model", "sql_model", "answer_model")
    @classmethod
    def _validate_model_spec(cls, value: str) -> str:
        provider, separator, model = value.partition(":")
        if not separator or not model:
            raise ValueError(
                f"expected 'provider:model', got {value!r}. Known providers: {sorted(PROVIDERS)}"
            )
        if provider not in PROVIDERS:
            raise ValueError(
                f"unknown provider {provider!r} in {value!r}. Known providers: {sorted(PROVIDERS)}"
            )
        return value

    @field_validator("runs_dir", mode="before")
    @classmethod
    def _blank_runs_dir_is_off(cls, value: object) -> object:
        """``RUNS_DIR=`` in a .env file means off, not the current directory."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


#: Built once at import. Import this, do not construct your own.
settings = Settings()
