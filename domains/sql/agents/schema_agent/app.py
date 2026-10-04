from __future__ import annotations

from core import http, llm
from core.events import RunContext
from core.prompts import load_prompt
from core.service import create_app
from domains.sql.config import PROMPTS, settings
from domains.sql.models import (
    SchemaResponse,
    SelectedSchemaOut,
    SelectSchemaRequest,
    SelectSchemaResponse,
    TableInfo,
)

SERVICE = "schema_agent"
app = create_app(SERVICE)

_SYSTEM_PROMPT = load_prompt(PROMPTS / "schema_select.txt")


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
    full_text = _render_schema(full_schema.tables)

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
