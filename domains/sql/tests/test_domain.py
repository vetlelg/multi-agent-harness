"""The sql pipeline's retry routing, run end to end with the services stubbed out.

Each scenario scripts the executor's responses and checks which route the run
took. Parametrized over engines: the LangGraph engine joins the list when it
exists, and must produce the same routes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import http, llm
from core.events import Event, RouteDecision
from core.models import AskRequest, QueryEvidence
from domains.sql import domain as sql
from domains.sql.config import settings
from domains.sql.errors import ErrorType
from domains.sql.models import (
    AnswerOut,
    ExecuteResponse,
    GenerateSqlResponse,
    SelectSchemaResponse,
    SqlState,
)
from orchestrator_scratch import engine as scratch

ENGINES = [pytest.param(scratch.run, id="scratch")]

_OK = ExecuteResponse(
    ok=True, sql_executed="SELECT 1 LIMIT 50", columns=["n"], rows=[[1]], row_count=1, elapsed_ms=1
)


def _fail(error_type: ErrorType) -> ExecuteResponse:
    return ExecuteResponse(
        ok=False, sql_executed="SELECT bad", error_type=error_type, error="boom", elapsed_ms=1
    )


@pytest.fixture
def stub(monkeypatch):
    """Replace the three services and the answer model. Returns the call log."""
    calls: list[str] = []
    executions: list[ExecuteResponse] = []

    def post(ctx, *, target, base_url, path, body, response_model):
        calls.append(path)
        if path == "/select-schema":
            return SelectSchemaResponse(tables=[], schema_text="T (n INTEGER)")
        if path == "/generate-sql":
            return GenerateSqlResponse(sql=f"SELECT {body.attempt}")
        if path == "/execute":
            return executions.pop(0)
        raise AssertionError(path)

    def call_model(ctx, *, role, model, system, user, output_model):
        calls.append(f"model:{role}")
        return SimpleNamespace(parsed=AnswerOut(answer="the answer"))

    monkeypatch.setattr(http, "post", post)
    monkeypatch.setattr(llm, "call_model", call_model)
    return SimpleNamespace(calls=calls, executions=executions)


def _run(engine, stub, *executions: ExecuteResponse):
    stub.executions.extend(executions)
    events: list[Event] = []
    response = engine(sql.DOMAIN, AskRequest(question="How many?"), emitter=events.append)
    routes = [e.branch for e in events if isinstance(e, RouteDecision)]
    return response, routes


@pytest.mark.parametrize("engine", ENGINES)
def test_success_first_time(engine, stub) -> None:
    response, routes = _run(engine, stub, _OK)

    assert routes == ["answer"]
    assert stub.calls == ["/select-schema", "/generate-sql", "/execute", "model:answer"]
    assert response.attempts == 1
    assert response.error is None
    assert response.answer == "the answer"
    assert response.evidence == [
        QueryEvidence(language="sql", query="SELECT 1 LIMIT 50", columns=["n"], rows=[[1]])
    ]


@pytest.mark.parametrize("engine", ENGINES)
def test_missing_object_reselects_schema(engine, stub) -> None:
    _, routes = _run(engine, stub, _fail(ErrorType.MISSING_OBJECT), _OK)

    assert routes == ["get_schema", "answer"]
    assert stub.calls.count("/select-schema") == 2


@pytest.mark.parametrize("engine", ENGINES)
def test_syntax_error_regenerates_sql_only(engine, stub) -> None:
    _, routes = _run(engine, stub, _fail(ErrorType.SYNTAX), _OK)

    assert routes == ["generate_sql", "answer"]
    assert stub.calls.count("/select-schema") == 1
    assert stub.calls.count("/generate-sql") == 2


@pytest.mark.parametrize("engine", ENGINES)
def test_attempts_exhausted_explains_failure(engine, stub) -> None:
    failures = [_fail(ErrorType.SYNTAX) for _ in range(settings.max_attempts)]
    response, routes = _run(engine, stub, *failures)

    assert routes == ["generate_sql"] * (settings.max_attempts - 1) + ["answer"]
    assert response.attempts == settings.max_attempts
    assert response.error == "boom"
    assert response.evidence[0].query == "SELECT bad"
    assert response.evidence[0].rows is None


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "error_type", [ErrorType.GUARD_REJECTED, ErrorType.TIMEOUT, ErrorType.OTHER]
)
def test_non_retryable_goes_straight_to_answer(engine, stub, error_type) -> None:
    response, routes = _run(engine, stub, _fail(error_type))

    assert routes == ["answer"]
    assert response.attempts == 1


@pytest.mark.parametrize(
    ("error", "error_type", "attempts", "expected"),
    [
        (None, None, 1, "answer"),
        ("x", "missing_object", 1, "get_schema"),
        ("x", "syntax", 1, "generate_sql"),
        ("x", "syntax", 3, "answer"),
        ("x", "guard_rejected", 1, "answer"),
        ("x", None, 1, "answer"),
    ],
)
def test_route_table(error, error_type, attempts, expected) -> None:
    state = SqlState(
        run_id="r", question="q", error=error, error_type=error_type, attempts=attempts
    )
    assert sql.route_after_execute(state) == expected


def test_no_evidence_before_any_sql() -> None:
    assert sql.evidence(SqlState(run_id="r", question="q")) == []
