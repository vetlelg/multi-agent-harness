"""A minimal domain with a retry route, for testing the harness without SQL.

    work -(route)-> answer -> END
      ^      |
      +------+  failed, attempts left

Steps read ``config.settings`` at call time, so a test can swap it with
monkeypatch and build a matching ``Domain`` with :func:`build`.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from core.events import RunContext
from core.models import Evidence, ToolEvidence
from core.pipeline import END, Domain, Pipeline, Route, RunState
from tests.fixtures.toy import config


class ToyState(RunState):
    worked: list[int] = Field(default_factory=list)


def work(state: ToyState, ctx: RunContext) -> dict[str, Any]:
    attempt = state.attempts + 1
    failed = attempt <= config.settings.failures
    return {
        "attempts": attempt,
        "worked": [*state.worked, attempt],
        "error": f"attempt {attempt} failed" if failed else None,
        "error_type": "flaky" if failed else None,
    }


def answer(state: ToyState, ctx: RunContext) -> dict[str, Any]:
    return {"answer": f"done after {state.attempts}"}


def route_after_work(state: ToyState) -> str:
    if state.error is not None and state.attempts < config.settings.max_attempts:
        return "work"
    return "answer"


def evidence(state: ToyState) -> list[Evidence]:
    return [
        ToolEvidence(tool="toy.work", input={}, output=str(state.worked), ok=state.error is None)
    ]


PIPELINE = Pipeline(
    state=ToyState,
    entry="work",
    steps={"work": work, "answer": answer},
    edges={"answer": END},
    routes={"work": Route(decide=route_after_work, targets=frozenset({"work", "answer"}))},
)


def build(settings: config.ToySettings) -> Domain:
    return Domain(name="toy", settings=settings, pipeline=PIPELINE, evidence=evidence)


DOMAIN = build(config.settings)
