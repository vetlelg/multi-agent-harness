"""Settings for the toy domain. Not read from .env, so tests are hermetic."""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict

from core.config import DomainSettings


class ToySettings(DomainSettings):
    model_config = SettingsConfigDict(env_prefix="TOY_", env_file=None)

    scratch_url: str = "http://toy-scratch"
    lg_url: str = "http://toy-lg"
    #: How many times the ``work`` step fails before it succeeds.
    failures: int = 0


settings = ToySettings()
