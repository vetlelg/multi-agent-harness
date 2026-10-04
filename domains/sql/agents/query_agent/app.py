from __future__ import annotations

from core import llm
from core.events import RunContext
from core.prompts import load_prompt
from core.service import create_app
from domains.sql.config import PROMPTS, settings
from domains.sql.models import GenerateSqlRequest, GenerateSqlResponse, SqlOut

SERVICE = "query_agent"
app = create_app(SERVICE)

_SYSTEM_PROMPT = load_prompt(PROMPTS / "generate_sql.txt")


@app.post("/generate-sql")
def generate_sql(request: GenerateSqlRequest) -> GenerateSqlResponse:
    ctx = RunContext(run_id=request.run_id, service=SERVICE)

    user_parts = [
        f"## Schema\n{request.schema_text}",
        f"## Question\n{request.question}",
        f"## Attempt\n{request.attempt}",
    ]
    if request.previous_sql:
        user_parts.append(f"## Previous SQL (failed)\n{request.previous_sql}")
    if request.error:
        user_parts.append(f"## Error\n{request.error}")

    result = llm.call_model(
        ctx,
        role="query",
        model=settings.query_model,
        system=_SYSTEM_PROMPT,
        user="\n\n".join(user_parts),
        output_model=SqlOut,
    )
    return GenerateSqlResponse(sql=result.parsed.sql)
