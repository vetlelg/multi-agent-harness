from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.models import (
    AskRequest,
    AskResponse,
    ColumnInfo,
    ExecuteRequest,
    ExecuteResponse,
    ForeignKeyInfo,
    GenerateSqlRequest,
    GenerateSqlResponse,
    RunState,
    SchemaResponse,
    SelectSchemaRequest,
    SelectSchemaResponse,
    TableInfo,
)

_COLUMN = ColumnInfo(name="id", type="INTEGER", nullable=False, pk=True)
_FK = ForeignKeyInfo(column="artist_id", references_table="Artist", references_column="ArtistId")
_TABLE = TableInfo(name="Album", columns=[_COLUMN], foreign_keys=[_FK])

_ROUND_TRIP_CASES = [
    AskRequest(question="How many?"),
    AskResponse(
        run_id="r1",
        orchestrator="scratch",
        question="How many?",
        sql="SELECT 1",
        columns=["c"],
        rows=[[1]],
        answer="One.",
        attempts=1,
        error=None,
    ),
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
    RunState(run_id="r1", question="How many?"),
]


@pytest.mark.parametrize("model", _ROUND_TRIP_CASES, ids=lambda m: type(m).__name__)
def test_round_trip(model):
    cls = type(model)
    json_bytes = model.model_dump_json()
    rebuilt = cls.model_validate_json(json_bytes)
    assert rebuilt == model


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        AskRequest(question="ok", bogus_field="should fail")
