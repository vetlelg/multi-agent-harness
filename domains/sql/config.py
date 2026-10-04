"""Settings for the sql domain. Environment variables are prefixed ``SQL_``."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import SettingsConfigDict

from core.config import DomainSettings, ModelSpec

#: This domain's prompt files.
PROMPTS = Path(__file__).resolve().parent / "prompts"


class SqlSettings(DomainSettings):
    model_config = SettingsConfigDict(env_prefix="SQL_")

    # --- where the orchestrators listen (read by the CLI) ---------------------
    scratch_url: str = "http://localhost:8100"
    lg_url: str = "http://localhost:8200"

    # --- model per role, as "provider:model" ----------------------------------
    schema_model: ModelSpec = "anthropic:claude-opus-5"
    query_model: ModelSpec = "anthropic:claude-opus-5"
    answer_model: ModelSpec = "anthropic:claude-opus-5"

    # --- the database and its guard -------------------------------------------
    db_path: Path = Path("data/chinook.db")
    row_limit: int = 50
    query_timeout_s: float = 5.0

    # --- service addresses ----------------------------------------------------
    executor_url: str = "http://localhost:8000"
    schema_agent_url: str = "http://localhost:8010"
    query_agent_url: str = "http://localhost:8020"


#: Built once at import. Import this, do not construct your own.
settings = SqlSettings()
