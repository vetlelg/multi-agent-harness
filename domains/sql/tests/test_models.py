from __future__ import annotations

import pytest
from pydantic import ValidationError

from domains.sql.config import SqlSettings
from domains.sql.errors import ErrorType
from domains.sql.models import (
    ColumnInfo,
    ExecuteRequest,
    ExecuteResponse,
    ForeignKeyInfo,
    GenerateSqlRequest,
    GenerateSqlResponse,
    SchemaResponse,
    SelectSchemaRequest,
    SelectSchemaResponse,
    SqlState,
    TableInfo,
)

_COLUMN = ColumnInfo(name="id", type="INTEGER", nullable=False, pk=True)
_FK = ForeignKeyInfo(column="artist_id", references_table="Artist", references_column="ArtistId")
_TABLE = TableInfo(name="Album", columns=[_COLUMN], foreign_keys=[_FK])

_ROUND_TRIP_CASES = [
    _COLUMN,
    _FK,
    _TABLE,
    SchemaResponse(tables=[_TABLE]),
    SelectSchemaRequest(run_id="r1", question="How many?"),
    SelectSchemaResponse(tables=[_TABLE], schema_text="Album(id INTEGER PK)"),
    GenerateSqlRequest(run_id="r1", question="How many?", schema_text="...", attempt=1),
    GenerateSqlResponse(sql="SELECT 1"),
    ExecuteRequest(run_id="r1", sql="SELECT 1"),
    ExecuteResponse(ok=True, elapsed_ms=5),
    ExecuteResponse(ok=False, elapsed_ms=5, error_type=ErrorType.SYNTAX, error='near "x"'),
    SqlState(run_id="r1", question="How many?"),
]


@pytest.mark.parametrize("model", _ROUND_TRIP_CASES, ids=lambda m: type(m).__name__)
def test_round_trip(model) -> None:
    cls = type(model)
    assert cls.model_validate_json(model.model_dump_json()) == model


def test_internal_requests_carry_run_id() -> None:
    with pytest.raises(ValidationError):
        ExecuteRequest(sql="SELECT 1")


def test_state_stores_error_type_as_plain_string() -> None:
    state = SqlState(run_id="r1", question="q", error_type=ErrorType.MISSING_OBJECT)
    assert state.error_type == "missing_object"
    assert type(state.error_type) is str


def test_settings_defaults() -> None:
    defaults = SqlSettings(_env_file=None)
    assert defaults.max_attempts == 3
    assert defaults.row_limit == 50
    assert defaults.query_timeout_s == 5.0


def test_invalid_model_spec_raises() -> None:
    with pytest.raises(ValidationError):
        SqlSettings(_env_file=None, query_model="nonsense")
