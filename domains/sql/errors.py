"""SQL error classification.

The single authority on what kind of failure a SQL error is. The executor
classifies at the source and the pipeline's route branches on the result, so
both orchestrators route retries from the same value.

Every prefix below was produced by running a failing query against the real
Chinook database. Do not add a prefix you have not observed.
"""

from __future__ import annotations

from enum import Enum


class ErrorType(str, Enum):
    """Categories of execution failure, in the vocabulary the router speaks."""

    MISSING_OBJECT = "missing_object"
    SYNTAX = "syntax"
    GUARD_REJECTED = "guard_rejected"
    TIMEOUT = "timeout"
    OTHER = "other"


class GuardRejection(Exception):
    """Raised by the executor's guard when SQL is refused before execution.

    This never originates from SQLite. It means our own rules said no.
    """


# Ordered longest-first where prefixes could overlap. Matching is on the start
# of the message so that a question quoting an error phrase cannot trigger it.
_PREFIXES: tuple[tuple[str, ErrorType], ...] = (
    ("no such table:", ErrorType.MISSING_OBJECT),
    ("no such column:", ErrorType.MISSING_OBJECT),
    ("no such function:", ErrorType.SYNTAX),
    ('near "', ErrorType.SYNTAX),
    ("wrong number of arguments to function", ErrorType.SYNTAX),
    ("interrupted", ErrorType.TIMEOUT),
)

# MISSING_OBJECT rewinds to schema selection; SYNTAX rewinds to SQL generation.
# The rest mean our own machinery refused or gave up, and retrying repeats it.
_RETRYABLE: frozenset[ErrorType] = frozenset({ErrorType.MISSING_OBJECT, ErrorType.SYNTAX})


def classify_sql_error(error: Exception | str) -> ErrorType:
    """Categorise a SQL failure.

    Accepts either the exception or its message. Callers catching SQLite errors
    must catch ``sqlite3.DatabaseError``, not ``sqlite3.OperationalError`` --
    multi-statement input raises ``ProgrammingError``, which is a sibling class.
    """
    if isinstance(error, GuardRejection):
        return ErrorType.GUARD_REJECTED

    message = (error if isinstance(error, str) else str(error)).strip()
    for prefix, error_type in _PREFIXES:
        if message.startswith(prefix):
            return error_type
    return ErrorType.OTHER


def is_retryable(error_type: ErrorType) -> bool:
    """Whether this failure is worth another attempt."""
    return error_type in _RETRYABLE
