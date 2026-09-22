from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable

from core.config import settings
from core.errors import GuardRejection

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|PRAGMA)\b",
    re.IGNORECASE,
)
_LIMIT = re.compile(r"\bLIMIT\b", re.IGNORECASE)
_FIRST_WORD = re.compile(r"\S+")

_ALLOWED_ACTIONS: frozenset[int] = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        33,  # SQLITE_RECURSIVE — not exposed in all Python builds
    }
)


def validate_and_rewrite(sql: str, *, row_limit: int = settings.row_limit) -> str:
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()

    if not sql:
        raise GuardRejection("empty query")

    if ";" in sql:
        raise GuardRejection("multiple statements are not allowed")

    match = _FIRST_WORD.match(sql)
    first_keyword = match.group().upper() if match else ""
    if first_keyword not in ("SELECT", "WITH"):
        raise GuardRejection(f"only SELECT or WITH ... SELECT is allowed, got {first_keyword!r}")

    found = _FORBIDDEN.search(sql)
    if found:
        raise GuardRejection(f"forbidden keyword: {found.group().upper()}")

    if not _LIMIT.search(sql):
        sql = f"{sql} LIMIT {row_limit}"

    return sql


def make_authorizer() -> Callable:
    def authorizer(
        action: int, _arg1: str | None, _arg2: str | None, _db: str | None, _trigger: str | None
    ) -> int:
        if action in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorizer
