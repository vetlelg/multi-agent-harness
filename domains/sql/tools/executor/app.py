from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from core.events import ToolCall, emit
from core.service import create_app
from domains.sql.config import settings
from domains.sql.errors import ErrorType, GuardRejection, classify_sql_error
from domains.sql.models import (
    ColumnInfo,
    ExecuteRequest,
    ExecuteResponse,
    ForeignKeyInfo,
    SchemaResponse,
    TableInfo,
)
from domains.sql.tools.executor.guard import make_authorizer, validate_and_rewrite

SERVICE = "executor"
app = create_app(SERVICE)

_db_uri = f"file:{settings.db_path}?mode=ro"

_intro_conn = sqlite3.connect(_db_uri, uri=True, check_same_thread=False)
_intro_conn.row_factory = sqlite3.Row

_exec_conn = sqlite3.connect(_db_uri, uri=True, check_same_thread=False)
_exec_conn.set_authorizer(make_authorizer())


def _introspect() -> SchemaResponse:
    cursor = _intro_conn.cursor()
    tables: list[TableInfo] = []

    for row in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall():
        name = row["name"]
        if name.startswith("sqlite_"):
            continue

        columns: list[ColumnInfo] = []
        for col in cursor.execute(f"PRAGMA table_info({name})").fetchall():
            columns.append(
                ColumnInfo(
                    name=col["name"],
                    type=col["type"] or "",
                    nullable=col["notnull"] == 0,
                    pk=col["pk"] > 0,
                )
            )

        foreign_keys: list[ForeignKeyInfo] = []
        for fk in cursor.execute(f"PRAGMA foreign_key_list({name})").fetchall():
            foreign_keys.append(
                ForeignKeyInfo(
                    column=fk["from"],
                    references_table=fk["table"],
                    references_column=fk["to"],
                )
            )

        tables.append(TableInfo(name=name, columns=columns, foreign_keys=foreign_keys))

    return SchemaResponse(tables=tables)


_cached_schema: SchemaResponse | None = None


@app.get("/schema")
def schema() -> SchemaResponse:
    global _cached_schema
    if _cached_schema is None:
        _cached_schema = _introspect()
    return _cached_schema


@contextmanager
def _timeout_guard(timeout_s: float):
    start = time.monotonic()

    def handler() -> int:
        return 1 if (time.monotonic() - start) > timeout_s else 0

    _exec_conn.set_progress_handler(handler, 1000)
    try:
        yield
    finally:
        _exec_conn.set_progress_handler(None, 0)


def _emit(request: ExecuteRequest, response: ExecuteResponse) -> ExecuteResponse:
    emit(
        ToolCall(
            run_id=request.run_id,
            service=SERVICE,
            tool="sql.execute",
            input={"sql": request.sql, "sql_executed": response.sql_executed},
            ok=response.ok,
            output=(
                {
                    "columns": response.columns,
                    "rows": response.rows,
                    "row_count": response.row_count,
                }
                if response.ok
                else None
            ),
            error_type=response.error_type,
            error=response.error,
            elapsed_ms=response.elapsed_ms,
        )
    )
    return response


@app.post("/execute")
def execute(request: ExecuteRequest) -> ExecuteResponse:
    start = time.perf_counter()

    try:
        rewritten = validate_and_rewrite(request.sql)
    except GuardRejection as e:
        return _emit(
            request,
            ExecuteResponse(
                ok=False,
                error_type=ErrorType.GUARD_REJECTED,
                error=str(e),
                elapsed_ms=0,
                sql_executed=None,
            ),
        )

    try:
        with _timeout_guard(settings.query_timeout_s):
            cursor = _exec_conn.execute(rewritten)
            if cursor.description:
                columns = [col[0] for col in cursor.description]
                raw_rows = cursor.fetchall()
                rows = [list(r) for r in raw_rows]
            else:
                columns = []
                rows = []
    except sqlite3.DatabaseError as e:
        return _emit(
            request,
            ExecuteResponse(
                ok=False,
                error_type=classify_sql_error(e),
                error=str(e),
                elapsed_ms=int((time.perf_counter() - start) * 1000),
                sql_executed=rewritten,
            ),
        )

    row_count = len(rows)
    return _emit(
        request,
        ExecuteResponse(
            ok=True,
            sql_executed=rewritten,
            columns=columns,
            rows=rows,
            row_count=row_count,
            truncated=row_count == settings.row_limit,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        ),
    )
