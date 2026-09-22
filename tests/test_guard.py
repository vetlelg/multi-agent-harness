from __future__ import annotations

import pytest

from agents.executor.guard import validate_and_rewrite
from core.errors import GuardRejection


@pytest.mark.parametrize(
    ("sql", "check"),
    [
        ("SELECT 1", lambda r: r == "SELECT 1 LIMIT 50"),
        ("  SELECT 1 ;  ", lambda r: r == "SELECT 1 LIMIT 50"),
        (
            "WITH cte AS (SELECT 1) SELECT * FROM cte",
            lambda r: r.startswith("WITH") and "LIMIT 50" in r,
        ),
        ("select * from Track", lambda r: r.endswith("LIMIT 50")),
        ("SELECT * FROM Track LIMIT 10", lambda r: "LIMIT 10" in r and "LIMIT 50" not in r),
    ],
    ids=["simple", "stripped", "cte", "case-insensitive", "existing-limit"],
)
def test_valid_sql(sql: str, check) -> None:
    result = validate_and_rewrite(sql)
    assert check(result), f"unexpected result: {result!r}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "INSERT INTO T VALUES (1)",
        "UPDATE T SET x=1",
        "DELETE FROM T",
        "DROP TABLE T",
        "ALTER TABLE T ADD x",
        "ATTACH DATABASE ':memory:' AS m",
        "PRAGMA table_info(T)",
        "",
        "   ",
    ],
    ids=[
        "multi-statement",
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "attach",
        "pragma",
        "empty",
        "whitespace",
    ],
)
def test_rejected_sql(sql: str) -> None:
    with pytest.raises(GuardRejection):
        validate_and_rewrite(sql)
