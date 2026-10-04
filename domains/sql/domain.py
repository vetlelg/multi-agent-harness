"""The sql domain as the engines see it: steps, the retry route, and evidence.

Everything both orchestrators do for a SQL question is defined here, once.
The engines only decide how it is executed.

    get_schema -> generate_sql -> execute -(route)-> answer -> END
                       ^             |  |
                       |  syntax     |  |  missing table/column
                       +-------------+  +-> get_schema
"""

from __future__ import annotations

from typing import Any

from core import http, llm
from core.events import RunContext
from core.models import Evidence, QueryEvidence
from core.pipeline import END, Domain, Pipeline, Route
from core.prompts import load_prompt
from domains.sql.config import PROMPTS, settings
from domains.sql.errors import ErrorType, is_retryable
from domains.sql.models import (
    AnswerOut,
    ExecuteRequest,
    ExecuteResponse,
    GenerateSqlRequest,
    GenerateSqlResponse,
    SelectSchemaRequest,
    SelectSchemaResponse,
    SqlState,
)

_ANSWER_PROMPT = load_prompt(PROMPTS / "answer.txt")


# --------------------------------------------------------------------------- #
# Steps                                                                         #
# --------------------------------------------------------------------------- #


def get_schema(state: SqlState, ctx: RunContext) -> dict[str, Any]:
    """Select the relevant schema. On a retry the agent sees what failed."""
    response = http.post(
        ctx,
        target="schema_agent",
        base_url=settings.schema_agent_url,
        path="/select-schema",
        body=SelectSchemaRequest(
            run_id=state.run_id,
            question=state.question,
            previous_sql=state.sql,
            error=state.error,
        ),
        response_model=SelectSchemaResponse,
    )
    return {"schema_text": response.schema_text}


def generate_sql(state: SqlState, ctx: RunContext) -> dict[str, Any]:
    response = http.post(
        ctx,
        target="query_agent",
        base_url=settings.query_agent_url,
        path="/generate-sql",
        body=GenerateSqlRequest(
            run_id=state.run_id,
            question=state.question,
            schema_text=state.schema_text,
            previous_sql=state.sql,
            error=state.error,
            attempt=state.attempts + 1,
        ),
        response_model=GenerateSqlResponse,
    )
    return {"sql": response.sql}


def execute(state: SqlState, ctx: RunContext) -> dict[str, Any]:
    result = http.post(
        ctx,
        target="executor",
        base_url=settings.executor_url,
        path="/execute",
        body=ExecuteRequest(run_id=state.run_id, sql=state.sql),
        response_model=ExecuteResponse,
    )
    update: dict[str, Any] = {
        "attempts": state.attempts + 1,
        "sql_executed": result.sql_executed,
    }
    if result.ok:
        return update | {
            "columns": result.columns,
            "rows": result.rows,
            "truncated": result.truncated,
            "error": None,
            "error_type": None,
        }
    return update | {
        "columns": None,
        "rows": None,
        "truncated": False,
        "error": result.error,
        "error_type": result.error_type,
    }


def answer(state: SqlState, ctx: RunContext) -> dict[str, Any]:
    """Turn rows -- or the last failure -- into a plain-language answer."""
    if state.rows is not None:
        parts = [
            f"## Question\n{state.question}",
            f"## SQL Executed\n{state.sql_executed or state.sql}",
            f"## Results\nColumns: {state.columns}\nRows:\n{state.rows}",
        ]
    else:
        parts = [
            f"## Question\n{state.question}",
            f"## SQL Attempted\n{state.sql_executed or state.sql or 'none'}",
            (
                f"## Error\nThe query could not produce results after {state.attempts} "
                f"attempt(s).\nLast error: {state.error}"
            ),
        ]

    result = llm.call_model(
        ctx,
        role="answer",
        model=settings.answer_model,
        system=_ANSWER_PROMPT,
        user="\n\n".join(parts),
        output_model=AnswerOut,
    )
    return {"answer": result.parsed.answer}


# --------------------------------------------------------------------------- #
# Route                                                                         #
# --------------------------------------------------------------------------- #


def route_after_execute(state: SqlState) -> str:
    """Success -> answer. Retryable failure with attempts left -> the stage that caused it.

    A missing table or column means the schema selection was wrong, so it is
    redone; any other retryable error means the SQL was wrong, so only that is
    regenerated. Everything else goes to ``answer``, which explains the failure.
    """
    if state.error is None:
        return "answer"
    error_type = ErrorType(state.error_type) if state.error_type else ErrorType.OTHER
    if state.attempts >= settings.max_attempts or not is_retryable(error_type):
        return "answer"
    if error_type is ErrorType.MISSING_OBJECT:
        return "get_schema"
    return "generate_sql"


# --------------------------------------------------------------------------- #
# Evidence                                                                      #
# --------------------------------------------------------------------------- #


def evidence(state: SqlState) -> list[Evidence]:
    """The SQL behind the answer -- what ran, or what was last attempted."""
    sql = state.sql_executed or state.sql
    if sql is None:
        return []
    return [
        QueryEvidence(
            language="sql",
            query=sql,
            columns=state.columns,
            rows=state.rows,
            truncated=state.truncated,
        )
    ]


PIPELINE = Pipeline(
    state=SqlState,
    entry="get_schema",
    steps={
        "get_schema": get_schema,
        "generate_sql": generate_sql,
        "execute": execute,
        "answer": answer,
    },
    edges={
        "get_schema": "generate_sql",
        "generate_sql": "execute",
        "answer": END,
    },
    routes={
        "execute": Route(
            decide=route_after_execute,
            targets=frozenset({"get_schema", "generate_sql", "answer"}),
        ),
    },
)

DOMAIN = Domain(name="sql", settings=settings, pipeline=PIPELINE, evidence=evidence)
