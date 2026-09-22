from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI

from core.config import settings
from core.events import ModelCall, emit
from core.llm import LLMRequest, get_client
from core.models import GenerateSqlRequest, GenerateSqlResponse, SqlOut

app = FastAPI()

_SYSTEM_PROMPT = (
    Path(__file__).resolve().parents[2] / "core" / "prompts" / "generate_sql.txt"
).read_text()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/generate-sql")
def generate_sql(request: GenerateSqlRequest) -> GenerateSqlResponse:
    user_parts = [
        f"## Schema\n{request.schema_text}",
        f"## Question\n{request.question}",
        f"## Attempt\n{request.attempt}",
    ]
    if request.previous_sql:
        user_parts.append(f"## Previous SQL (failed)\n{request.previous_sql}")
    if request.error:
        user_parts.append(f"## Error\n{request.error}")

    client = get_client(settings.sql_model)
    llm_req = LLMRequest(system=_SYSTEM_PROMPT, user="\n\n".join(user_parts), output_model=SqlOut)

    start = time.perf_counter()
    result = client.complete(llm_req)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    emit(
        ModelCall(
            run_id=request.run_id,
            service="query_agent",
            role="sql",
            provider=result.provider,
            model=result.model,
            prompt=llm_req.user,
            response=result.raw_text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            stop_reason=result.stop_reason.value,
            elapsed_ms=elapsed_ms,
        )
    )

    return GenerateSqlResponse(sql=result.parsed.sql)
