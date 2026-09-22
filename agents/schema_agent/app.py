from __future__ import annotations

import time
from pathlib import Path

import httpx
from fastapi import FastAPI

from core.config import settings
from core.events import HttpCall, ModelCall, emit
from core.llm import LLMRequest, get_client
from core.models import (
    SchemaResponse,
    SelectedSchemaOut,
    SelectSchemaRequest,
    SelectSchemaResponse,
    TableInfo,
)

app = FastAPI()

_SYSTEM_PROMPT = (
    Path(__file__).resolve().parents[2] / "core" / "prompts" / "schema_select.txt"
).read_text()
_http = httpx.Client(timeout=settings.http_timeout_s)


def _render_schema(tables: list[TableInfo]) -> str:
    parts: list[str] = []
    for t in tables:
        fk_map = {fk.column: fk for fk in t.foreign_keys}
        lines: list[str] = []
        for col in t.columns:
            frags = [f"  {col.name} {col.type}"]
            if col.pk:
                frags.append("PRIMARY KEY")
            if not col.nullable:
                frags.append("NOT NULL")
            if col.name in fk_map:
                fk = fk_map[col.name]
                frags.append(f"REFERENCES {fk.references_table}({fk.references_column})")
            lines.append(" ".join(frags))
        parts.append(f"{t.name} (\n" + ",\n".join(lines) + "\n)")
    return "\n\n".join(parts)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/select-schema")
def select_schema(request: SelectSchemaRequest) -> SelectSchemaResponse:
    start = time.perf_counter()
    resp = _http.get(f"{settings.executor_url}/schema")
    resp.raise_for_status()
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    full_schema = SchemaResponse.model_validate(resp.json())

    emit(
        HttpCall(
            run_id=request.run_id,
            service="schema_agent",
            target="executor",
            method="GET",
            path="/schema",
            status=resp.status_code,
            elapsed_ms=elapsed_ms,
        )
    )

    full_text = _render_schema(full_schema.tables)

    user_parts = [f"## Full Database Schema\n{full_text}", f"## Question\n{request.question}"]
    if request.previous_sql:
        user_parts.append(f"## Previous SQL (failed)\n{request.previous_sql}")
    if request.error:
        user_parts.append(f"## Error\n{request.error}")

    client = get_client(settings.schema_model)
    llm_req = LLMRequest(
        system=_SYSTEM_PROMPT, user="\n\n".join(user_parts), output_model=SelectedSchemaOut
    )

    start = time.perf_counter()
    result = client.complete(llm_req)
    llm_elapsed = int((time.perf_counter() - start) * 1000)

    emit(
        ModelCall(
            run_id=request.run_id,
            service="schema_agent",
            role="schema",
            provider=result.provider,
            model=result.model,
            prompt=llm_req.user,
            response=result.raw_text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            stop_reason=result.stop_reason.value,
            elapsed_ms=llm_elapsed,
        )
    )

    table_lookup = {t.name: t for t in full_schema.tables}
    filtered_tables: list[TableInfo] = []

    for selected in result.parsed.tables:
        real_table = table_lookup.get(selected.name)
        if real_table is None:
            continue
        selected_cols = set(selected.columns)
        columns = [c for c in real_table.columns if c.name in selected_cols]
        col_names = {c.name for c in columns}
        fks = [fk for fk in real_table.foreign_keys if fk.column in col_names]
        filtered_tables.append(TableInfo(name=real_table.name, columns=columns, foreign_keys=fks))

    schema_text = _render_schema(filtered_tables)
    return SelectSchemaResponse(tables=filtered_tables, schema_text=schema_text)
