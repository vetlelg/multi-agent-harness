from __future__ import annotations

import pytest

from domains.sql.errors import ErrorType, GuardRejection, classify_sql_error, is_retryable


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("no such table: Customer", ErrorType.MISSING_OBJECT),
        ("no such column: foo", ErrorType.MISSING_OBJECT),
        ("no such function: GROUP_CONCAT_DISTINCT", ErrorType.SYNTAX),
        ('near "INSERT": syntax error', ErrorType.SYNTAX),
        ("wrong number of arguments to function SUBSTR()", ErrorType.SYNTAX),
        ("ambiguous column name: ArtistId", ErrorType.SYNTAX),
        ("misuse of aggregate function SUM()", ErrorType.SYNTAX),
        (
            'no such column: "AC/DC" - should this be a string literal in single-quotes?',
            ErrorType.SYNTAX,
        ),
        ("no such column: T.Foo", ErrorType.MISSING_OBJECT),
        ("interrupted", ErrorType.TIMEOUT),
        ("attempt to write a readonly database", ErrorType.OTHER),
        ("You can only execute one statement at a time.", ErrorType.OTHER),
        ("something completely unexpected", ErrorType.OTHER),
    ],
)
def test_classify_sql_error(message: str, expected: ErrorType) -> None:
    assert classify_sql_error(message) == expected


def test_classify_guard_rejection() -> None:
    assert classify_sql_error(GuardRejection("DROP TABLE")) == ErrorType.GUARD_REJECTED


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (ErrorType.MISSING_OBJECT, True),
        (ErrorType.SYNTAX, True),
        (ErrorType.GUARD_REJECTED, False),
        (ErrorType.TIMEOUT, False),
        (ErrorType.OTHER, False),
        (ErrorType.UNANSWERABLE, False),
    ],
)
def test_is_retryable(error_type: ErrorType, expected: bool) -> None:
    assert is_retryable(error_type) is expected
