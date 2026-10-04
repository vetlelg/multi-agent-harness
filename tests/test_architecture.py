"""The dependency rules from CLAUDE.md, checked statically.

These are what keep the harness a harness: if a rule here fails, a domain has
leaked into shared code (or one orchestrator into the other), and adding the
next domain would mean editing the harness. Test code is exempt.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SKIP = {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "runs", "data", "docs"}

PROVIDER_SDKS = {"anthropic", "ollama", "openai", "google"}
AGENT_FRAMEWORKS = {"langgraph", "langchain", "langchain_core", "crewai", "autogen"}


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@cache
def _sources() -> dict[str, Path]:
    """Every non-test module in the repo, by dotted name."""
    found = {}
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).parts
        if _SKIP.intersection(parts) or "tests" in parts:
            continue
        found[_module_name(path)] = path
    return found


@cache
def _tree(name: str) -> ast.Module:
    return ast.parse(_sources()[name].read_text(encoding="utf-8"))


@cache
def _imports(name: str) -> frozenset[str]:
    """Dotted names a module imports, including ``from pkg import submodule``."""
    found: set[str] = set()
    for node in ast.walk(_tree(name)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{name}: use absolute imports"
            module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return frozenset(found)


def _within(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _top(name: str) -> str:
    return name.split(".")[0]


def _modules_in(package: str) -> list[str]:
    return sorted(m for m in _sources() if _within(m, package))


def _violations(modules: list[str], forbidden) -> list[str]:
    return [
        f"{module} imports {imported}"
        for module in modules
        for imported in sorted(_imports(module))
        if forbidden(module, imported)
    ]


def _domains() -> list[str]:
    return sorted({m.split(".")[1] for m in _modules_in("domains") if m.count(".") >= 1})


# --------------------------------------------------------------------------- #
# Dependency direction                                                          #
# --------------------------------------------------------------------------- #


def test_core_imports_nothing_above_it() -> None:
    above = ("domains", "orchestrator_scratch", "orchestrator_lg", "cli")
    assert not _violations(_modules_in("core"), lambda m, i: any(_within(i, p) for p in above))


@pytest.mark.parametrize(
    ("package", "other"),
    [("orchestrator_scratch", "orchestrator_lg"), ("orchestrator_lg", "orchestrator_scratch")],
)
def test_orchestrators_reach_domains_only_through_the_registry(package: str, other: str) -> None:
    assert not _violations(
        _modules_in(package), lambda m, i: _within(i, "domains") or _within(i, other)
    )


@pytest.mark.parametrize("domain", _domains())
def test_domains_import_only_core_and_themselves(domain: str) -> None:
    own = f"domains.{domain}"

    def forbidden(module: str, imported: str) -> bool:
        if _within(imported, "domains") and imported != "domains":
            return not _within(imported, own)
        return _top(imported) in {"orchestrator_scratch", "orchestrator_lg", "cli"}

    assert not _violations(_modules_in(own), forbidden)


# --------------------------------------------------------------------------- #
# Where third-party code may appear                                             #
# --------------------------------------------------------------------------- #


def test_provider_sdks_only_in_the_llm_wrapper() -> None:
    assert not _violations(
        sorted(_sources()),
        lambda m, i: _top(i) in PROVIDER_SDKS and m != "core.llm",
    )


def test_agent_frameworks_only_in_orchestrator_lg() -> None:
    assert not _violations(
        sorted(_sources()),
        lambda m, i: _top(i) in AGENT_FRAMEWORKS and not _within(m, "orchestrator_lg"),
    )


def test_orchestrator_lg_uses_no_langchain_model_wrappers() -> None:
    assert not _violations(
        _modules_in("orchestrator_lg"),
        lambda m, i: _top(i) in AGENT_FRAMEWORKS - {"langgraph"},
    )


def test_environment_is_read_only_through_settings() -> None:
    offenders = []
    for name in _sources():
        for node in ast.walk(_tree(name)):
            reads_os = (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in {"getenv", "environ", "putenv"}
            )
            imports_os = isinstance(node, ast.ImportFrom) and node.module == "os"
            if reads_os or imports_os:
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders


# --------------------------------------------------------------------------- #
# Tool servers stay small                                                       #
# --------------------------------------------------------------------------- #


def _closure(start: str) -> set[str]:
    """Every name reachable through imports from ``start``, following repo modules."""
    sources = _sources()
    seen: set[str] = set()
    external: set[str] = set()
    stack = [start]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        # Importing a.b.c runs a/__init__ and a/b/__init__ first.
        parts = module.split(".")
        stack.extend(
            ".".join(parts[:i]) for i in range(1, len(parts)) if ".".join(parts[:i]) in sources
        )
        for imported in _imports(module):
            if imported in sources:
                stack.append(imported)
            elif not any(_within(imported, s) for s in sources if "." not in s):
                external.add(_top(imported))
    return external


@pytest.mark.parametrize(
    "tool_server",
    [m for m in _sources() if ".tools." in m and m.endswith(".app")],
)
def test_tool_servers_need_only_requirements_tool(tool_server: str) -> None:
    """Tool servers are built from requirements-tool.txt: no LLM SDKs, no httpx."""
    assert not _closure(tool_server) & (PROVIDER_SDKS | AGENT_FRAMEWORKS | {"httpx"})
