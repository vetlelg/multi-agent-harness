"""Harness-wide settings, read once and validated once.

This module sits at the bottom of the dependency graph: everything imports it,
it imports nothing of ours. No logic, no I/O, no clients.

Only values every domain shares live here: provider credentials, model-call
budgets, the run log. A domain's own values -- its models, its service
addresses, its data paths -- live in ``domains/<name>/config.py``, as a
subclass of :class:`DomainSettings` with its own env prefix, so two domains
can never collide on a variable name.

``os.getenv`` must not appear anywhere in the repo. Reading the environment
at call time lets one test leak into the next, producing failures that depend on
the order tests happen to run in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Providers the ``provider:model`` syntax accepts. A provider listed here but
#: without an adapter in ``core.llm`` fails later, at client construction, with
#: a message saying so.
PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai", "gemini", "ollama"})


def _validate_model_spec(value: str) -> str:
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


#: A ``provider:model`` string, validated when settings load. Use it for every
#: model setting, so a typo fails at startup rather than on the first call.
ModelSpec = Annotated[str, AfterValidator(_validate_model_spec)]

_ENV = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)


class Settings(BaseSettings):
    model_config = _ENV

    # --- provider credentials -------------------------------------------------
    # SecretStr so a stray repr or traceback cannot print the key. Values are
    # read from .env, which pydantic-settings does NOT export into os.environ,
    # so adapters must pass these explicitly rather than relying on the SDKs
    # picking them up from the environment.
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    ollama_host: str = "http://localhost:11434"

    # --- model parameters -----------------------------------------------------
    # Thinking is on by default on current Claude models, so a low ceiling
    # truncates the response rather than shortening it.
    max_tokens: int = 16000
    # Model calls are far slower than service calls; they get their own budget.
    llm_timeout_s: float = 120.0
    http_timeout_s: float = 30.0

    # --- which domain this process serves -------------------------------------
    #: Read by orchestrators (and as the CLI default). Agents and tool servers
    #: belong to exactly one domain already and ignore it.
    domain: str | None = None

    # --- observability --------------------------------------------------------
    #: When set, whole runs are appended to ``<runs_dir>/<run_id>.jsonl``.
    #: Left unset in the cluster, where ``kubectl logs`` is the source of truth.
    runs_dir: Path | None = None

    @field_validator("runs_dir", "domain", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """``RUNS_DIR=`` in a .env file means off, not the current directory."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


class DomainSettings(BaseSettings):
    """Base for every domain's settings.

    A subclass sets ``model_config = SettingsConfigDict(env_prefix="<NAME>_")``
    and adds the domain's own fields; the .env handling below is inherited.
    The fields declared here are the ones the harness itself reads from a domain.
    """

    model_config = _ENV

    #: Where this domain's two orchestrators listen. Read by the CLI.
    scratch_url: str
    lg_url: str

    #: Executions permitted per run. Routes read it, so both engines enforce
    #: the same budget.
    max_attempts: int = 3
    #: Hard ceiling on steps per run -- a guard against a route that never
    #: reaches END. The LangGraph engine maps it to its recursion limit.
    max_steps: int = 25


#: Built once at import. Import this, do not construct your own.
settings = Settings()
