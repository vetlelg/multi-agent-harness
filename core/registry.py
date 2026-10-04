"""Finding domains by name.

The only way harness code reaches a domain. Orchestrators and the CLI name a
domain as a string -- from ``DOMAIN`` or ``--domain`` -- and get it from here,
so no harness module ever imports ``domains.<name>`` statically. That is what
keeps adding a domain from touching the harness.

Two lookups, deliberately separate: ``load_domain_settings`` imports only the
domain's ``config`` module (cheap -- enough for the CLI to find a URL), while
``load_domain`` imports its pipeline (what an orchestrator needs).
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil

from core.config import DomainSettings
from core.pipeline import Domain

DOMAINS_PACKAGE = "domains"


def _check_name(name: str) -> None:
    if not name.isidentifier():
        raise ValueError(f"invalid domain name {name!r}")


def load_domain(name: str, package: str = DOMAINS_PACKAGE) -> Domain:
    """Import ``<package>.<name>.domain`` and return its ``DOMAIN``."""
    _check_name(name)
    try:
        module = importlib.import_module(f"{package}.{name}.domain")
    except ModuleNotFoundError as exc:
        if exc.name not in {f"{package}.{name}", f"{package}.{name}.domain"}:
            raise
        raise LookupError(f"no domain {name!r}; available: {available_domains(package)}") from exc

    domain = getattr(module, "DOMAIN", None)
    if not isinstance(domain, Domain):
        raise TypeError(f"{module.__name__}.DOMAIN must be a core.pipeline.Domain")
    if domain.name != name:
        raise ValueError(f"{module.__name__}.DOMAIN is named {domain.name!r}, expected {name!r}")
    return domain


def load_domain_settings(name: str, package: str = DOMAINS_PACKAGE) -> DomainSettings:
    """Import ``<package>.<name>.config`` and return its ``settings``."""
    _check_name(name)
    try:
        module = importlib.import_module(f"{package}.{name}.config")
    except ModuleNotFoundError as exc:
        if exc.name not in {f"{package}.{name}", f"{package}.{name}.config"}:
            raise
        raise LookupError(f"no domain {name!r}; available: {available_domains(package)}") from exc

    settings = getattr(module, "settings", None)
    if not isinstance(settings, DomainSettings):
        raise TypeError(f"{module.__name__}.settings must be a core.config.DomainSettings")
    return settings


def available_domains(package: str = DOMAINS_PACKAGE) -> list[str]:
    """Every subpackage of ``package`` that has a ``domain`` module."""
    root = importlib.import_module(package)
    names = []
    for info in pkgutil.iter_modules(root.__path__):
        if not info.ispkg:
            continue
        if importlib.util.find_spec(f"{package}.{info.name}.domain") is not None:
            names.append(info.name)
    return sorted(names)
