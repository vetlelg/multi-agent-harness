"""The scratch engine's rules (CLAUDE.md, "Engines"), pinned against the toy domain.

The LangGraph engine must pass the same assertions once it exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.events import Event, RouteDecision, RunEnd, RunStart
from core.models import AskRequest, ToolEvidence
from core.pipeline import END, Domain, Pipeline, PipelineError, Route
from orchestrator_scratch import engine
from tests.fixtures.toy import config
from tests.fixtures.toy import domain as toy
from tests.fixtures.toy.config import ToySettings


def _run(monkeypatch, domain: Domain | None = None, **overrides: Any):
    settings = ToySettings(**overrides)
    monkeypatch.setattr(config, "settings", settings)
    events: list[Event] = []
    response = engine.run(
        domain or toy.build(settings), AskRequest(question="go?"), emitter=events.append
    )
    return response, events


def _branches(events: list[Event]) -> list[str]:
    return [e.branch for e in events if isinstance(e, RouteDecision)]


def test_success_first_time(monkeypatch) -> None:
    response, events = _run(monkeypatch)

    assert response.answer == "done after 1"
    assert response.domain == "toy"
    assert response.orchestrator == "scratch"
    assert response.attempts == 1
    assert response.error is None
    assert response.evidence == [ToolEvidence(tool="toy.work", input={}, output="[1]", ok=True)]
    assert _branches(events) == ["answer"]


def test_events_are_bracketed_and_numbered(monkeypatch) -> None:
    response, events = _run(monkeypatch, failures=1)

    assert isinstance(events[0], RunStart)
    assert isinstance(events[-1], RunEnd)
    assert events[-1].ok is True
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert {e.run_id for e in events} == {response.run_id}


def test_retry_route_until_success(monkeypatch) -> None:
    response, events = _run(monkeypatch, failures=2, max_attempts=3)

    assert _branches(events) == ["work", "work", "answer"]
    assert response.attempts == 3
    assert response.error is None


def test_retries_exhausted_still_answers(monkeypatch) -> None:
    response, events = _run(monkeypatch, failures=5, max_attempts=3)

    assert _branches(events) == ["work", "work", "answer"]
    assert response.attempts == 3
    assert response.error == "attempt 3 failed"
    assert events[-1].ok is False


def test_supplied_run_id_is_kept(monkeypatch) -> None:
    monkeypatch.setattr(config, "settings", ToySettings())
    response = engine.run(
        toy.build(config.settings), AskRequest(question="q", run_id="fixed"), emitter=lambda e: None
    )
    assert response.run_id == "fixed"


def test_max_steps_bounds_a_run(monkeypatch) -> None:
    with pytest.raises(PipelineError, match="max_steps"):
        _run(monkeypatch, failures=99, max_attempts=99, max_steps=5)


def _domain_with(**changes: Any) -> Domain:
    parts: dict[str, Any] = {
        "state": toy.ToyState,
        "entry": "work",
        "steps": {"work": toy.work, "answer": toy.answer},
        "edges": {"answer": END},
        "routes": {
            "work": Route(decide=toy.route_after_work, targets=frozenset({"work", "answer"}))
        },
    }
    parts.update(changes)
    return Domain(
        name="toy", settings=ToySettings(), pipeline=Pipeline(**parts), evidence=toy.evidence
    )


def _run_capturing(domain: Domain, exc: type[Exception], match: str) -> list[Event]:
    events: list[Event] = []
    with pytest.raises(exc, match=match):
        engine.run(domain, AskRequest(question="q"), emitter=events.append)
    return events


def test_step_exception_still_ends_the_run() -> None:
    def broken(state, ctx):
        raise RuntimeError("agent unreachable")

    events = _run_capturing(
        _domain_with(steps={"work": broken, "answer": toy.answer}), RuntimeError, "unreachable"
    )
    end = events[-1]
    assert isinstance(end, RunEnd)
    assert end.ok is False
    assert "agent unreachable" in end.error


def test_unknown_update_field_is_an_error() -> None:
    def typo(state, ctx):
        return {"answr": "oops"}

    events = _run_capturing(
        _domain_with(steps={"work": typo, "answer": toy.answer}), PipelineError, "answr"
    )
    assert isinstance(events[-1], RunEnd)


def test_route_outside_its_targets_is_an_error() -> None:
    domain = _domain_with(
        routes={"work": Route(decide=lambda s: "answer", targets=frozenset({"work"}))}
    )
    _run_capturing(domain, PipelineError, "not one of")


def test_ending_without_an_answer_is_an_error() -> None:
    domain = _domain_with(
        steps={"work": toy.work, "answer": lambda s, c: {}},
    )
    _run_capturing(domain, PipelineError, "without setting an answer")
