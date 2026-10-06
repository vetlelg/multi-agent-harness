from __future__ import annotations

from core import http, llm
from core.events import RunContext
from core.prompts import load_prompt
from core.service import create_app
from domains.sql.agents.schema_agent.selection import render_schema, select_tables
from domains.sql.config import PROMPTS, settings
from domains.sql.models import (
    SchemaResponse,
    SelectedSchemaOut,
    SelectSchemaRequest,
    SelectSchemaResponse,
)

SERVICE = "schema_agent"
app = create_app(SERVICE)

_SYSTEM_PROMPT = load_prompt(PROMPTS / "schema_select.txt")


@app.post("/select-schema")
def select_schema(request: SelectSchemaRequest) -> SelectSchemaResponse:
    ctx = RunContext(run_id=request.run_id, service=SERVICE)

    full_schema = http.get(
        ctx,
        target="executor",
        base_url=settings.executor_url,
        path="/schema",
        response_model=SchemaResponse,
    )
    full_text = render_schema(full_schema.tables)

    user_parts = [f"## Full Database Schema\n{full_text}", f"## Question\n{request.question}"]
    if request.previous_sql:
        user_parts.append(f"## Previous SQL (failed)\n{request.previous_sql}")
    if request.error:
        user_parts.append(f"## Error\n{request.error}")

    result = llm.call_model(
        ctx,
        role="schema",
        model=settings.schema_model,
        system=_SYSTEM_PROMPT,
        user="\n\n".join(user_parts),
        output_model=SelectedSchemaOut,
    )

    # An empty selection is an answer too: the pipeline reads it as "the data
    # cannot answer this".
    tables = select_tables(full_schema.tables, result.parsed.tables)
    return SelectSchemaResponse(tables=tables, schema_text=render_schema(tables))
