from __future__ import annotations

import time
from pathlib import Path

import httpx
from fastapi import FastAPI

from core.config import settings
from core.errors import ErrorType, is_retryable
from core.events import HttpCall, ModelCall, RouteDecision, RunEnd, RunStart, emit, new_run_id
from core.llm import LLMRequest, get_client
from core.models import (
    AnswerOut,
    AskRequest,
    AskResponse,
    ExecuteRequest,
    ExecuteResponse,
    GenerateSqlRequest,
    GenerateSqlResponse,
    RunState,
    SelectSchemaRequest,
    SelectSchemaResponse,
)

app = FastAPI()

_ANSWER_PROMPT = (
    Path(__file__).resolve().parents[1] / "core" / "prompts" / "answer.txt"
).read_text()
_http = httpx.Client(timeout=settings.llm_timeout_s + settings.http_timeout_s)
_SERVICE = "orchestrator_scratch"


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


def _emit_seq(event, counter: list[int]) -> None:
    counter[0] += 1
    event.seq = counter[0]
    emit(event)


@app.post("/ask")
def ask(request: AskRequest) -> AskResponse:
    run_id = request.run_id or new_run_id()
    state = RunState(run_id=run_id, question=request.question)
    seq: list[int] = [0]

    _emit_seq(
        RunStart(
            run_id=run_id, service=_SERVICE, question=request.question, orchestrator="scratch"
        ),
        seq,
    )

    while state.attempts < settings.max_attempts:
        # --- Schema ---
        if state.schema_text is None or state.error_type is ErrorType.MISSING_OBJECT:
            schema_req = SelectSchemaRequest(
                run_id=run_id,
                question=state.question,
                previous_sql=state.sql,
                error=state.error,
            )
            start = time.perf_counter()
            resp = _http.post(
                f"{settings.schema_agent_url}/select-schema",
                json=schema_req.model_dump(),
            )
            resp.raise_for_status()
            elapsed_ms = int((time.perf_counter() - start) * 1000)

            schema_resp = SelectSchemaResponse.model_validate(resp.json())
            state.schema_text = schema_resp.schema_text

            _emit_seq(
                HttpCall(
                    run_id=run_id,
                    service=_SERVICE,
                    target="schema_agent",
                    method="POST",
                    path="/select-schema",
                    status=resp.status_code,
                    elapsed_ms=elapsed_ms,
                ),
                seq,
            )

        # --- SQL ---
        sql_req = GenerateSqlRequest(
            run_id=run_id,
            question=state.question,
            schema_text=state.schema_text,
            previous_sql=state.sql,
            error=state.error,
            attempt=state.attempts + 1,
        )
        start = time.perf_counter()
        resp = _http.post(
            f"{settings.query_agent_url}/generate-sql",
            json=sql_req.model_dump(),
        )
        resp.raise_for_status()
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        sql_resp = GenerateSqlResponse.model_validate(resp.json())
        state.sql = sql_resp.sql

        _emit_seq(
            HttpCall(
                run_id=run_id,
                service=_SERVICE,
                target="query_agent",
                method="POST",
                path="/generate-sql",
                status=resp.status_code,
                elapsed_ms=elapsed_ms,
            ),
            seq,
        )

        # --- Execute ---
        exec_req = ExecuteRequest(run_id=run_id, sql=state.sql)
        start = time.perf_counter()
        resp = _http.post(
            f"{settings.executor_url}/execute",
            json=exec_req.model_dump(),
        )
        resp.raise_for_status()
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        exec_resp = ExecuteResponse.model_validate(resp.json())

        _emit_seq(
            HttpCall(
                run_id=run_id,
                service=_SERVICE,
                target="executor",
                method="POST",
                path="/execute",
                status=resp.status_code,
                elapsed_ms=elapsed_ms,
            ),
            seq,
        )

        state.attempts += 1

        if exec_resp.ok:
            state.columns = exec_resp.columns
            state.rows = exec_resp.rows
            state.error = None
            state.error_type = None
            _emit_seq(
                RouteDecision(
                    run_id=run_id,
                    service=_SERVICE,
                    attempt=state.attempts,
                    error_type=None,
                    branch="answer",
                ),
                seq,
            )
            break
        else:
            state.error = exec_resp.error
            state.error_type = exec_resp.error_type

            if is_retryable(state.error_type) and state.attempts < settings.max_attempts:
                branch = (
                    "get_schema" if state.error_type is ErrorType.MISSING_OBJECT else "generate_sql"
                )
                _emit_seq(
                    RouteDecision(
                        run_id=run_id,
                        service=_SERVICE,
                        attempt=state.attempts,
                        error_type=state.error_type,
                        branch=branch,
                    ),
                    seq,
                )
                continue
            else:
                _emit_seq(
                    RouteDecision(
                        run_id=run_id,
                        service=_SERVICE,
                        attempt=state.attempts,
                        error_type=state.error_type,
                        branch="answer",
                    ),
                    seq,
                )
                break

    # --- Answer ---
    if state.rows is not None:
        user_parts = [
            f"## Question\n{state.question}",
            f"## SQL Executed\n{state.sql}",
            f"## Results\nColumns: {state.columns}\nRows:\n{state.rows}",
        ]
    else:
        user_parts = [
            f"## Question\n{state.question}",
            f"## SQL Attempted\n{state.sql or 'none'}",
            f"## Error\nThe query could not produce results after {state.attempts} attempt(s).\nLast error: {state.error}",
        ]

    client = get_client(settings.answer_model)
    llm_req = LLMRequest(
        system=_ANSWER_PROMPT, user="\n\n".join(user_parts), output_model=AnswerOut
    )

    start = time.perf_counter()
    result = client.complete(llm_req)
    llm_elapsed = int((time.perf_counter() - start) * 1000)

    state.answer = result.parsed.answer

    _emit_seq(
        ModelCall(
            run_id=run_id,
            service=_SERVICE,
            role="answer",
            provider=result.provider,
            model=result.model,
            prompt=llm_req.user,
            response=result.raw_text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            stop_reason=result.stop_reason.value,
            elapsed_ms=llm_elapsed,
        ),
        seq,
    )

    _emit_seq(
        RunEnd(
            run_id=run_id,
            service=_SERVICE,
            attempts=state.attempts,
            ok=state.error is None,
            answer=state.answer,
            error=state.error,
        ),
        seq,
    )

    return AskResponse(
        run_id=run_id,
        orchestrator="scratch",
        question=state.question,
        sql=state.sql,
        columns=state.columns,
        rows=state.rows,
        answer=state.answer,
        attempts=state.attempts,
        error=state.error,
    )
