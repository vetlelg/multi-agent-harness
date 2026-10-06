from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager

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

_DB_URI = f"file:{settings.db_path}?mode=ro"


def _connect() -> sqlite3.Connection:
    """A new read-only connection. No connection is ever shared between requests."""
    return sqlite3.connect(_DB_URI, uri=True)


def _introspect() -> SchemaResponse:
    # No authorizer on this connection: introspection needs PRAGMA.
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
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


#: Read once at startup: the database is baked into the image and never changes.
_SCHEMA = _introspect()


@app.get("/schema")
def schema() -> SchemaResponse:
    return _SCHEMA


@contextmanager
def _query_connection(timeout_s: float) -> Iterator[sqlite3.Connection]:
    """A connection for one query: read-only, authorizer on, timeout armed from now.

    One per request, never shared. The authorizer and the progress handler belong
    to the connection: on a shared one, a request finishing cleared the timeout of
    another still running, and every query waited for the one ahead of it.

    Double-quoted names are identifiers only, never strings. With SQLite's legacy
    default, a quoted column that doesn't exist silently became a string literal,
    and a query against a missing column "succeeded".
    """
    conn = _connect()
    try:
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
        conn.set_authorizer(make_authorizer())
        start = time.monotonic()

        def handler() -> int:
            return 1 if (time.monotonic() - start) > timeout_s else 0

        conn.set_progress_handler(handler, 1000)
        yield conn
    finally:
        conn.close()


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
        with _query_connection(settings.query_timeout_s) as conn:
            cursor = conn.execute(rewritten)
            if cursor.description:
                columns = [col[0] for col in cursor.description]
                # The cap is enforced here, not by the guard: its LIMIT is skipped
                # whenever the SQL has a LIMIT of its own, even in a subquery.
                raw_rows = cursor.fetchmany(settings.row_limit)
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
