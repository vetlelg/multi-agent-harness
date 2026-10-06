"""The schema agent's selection: from table names to the schema the query agent sees.

Pure code, no model. The hand-built schema pins the rules; the Chinook tests pin
the joins the eval set depends on (they need ``make db``).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from domains.sql.agents.schema_agent.selection import render_schema, select_tables
from domains.sql.config import settings
from domains.sql.models import ColumnInfo, ForeignKeyInfo, SchemaResponse, TableInfo


def _table(name: str, *columns: str, fks: dict[str, str] | None = None) -> TableInfo:
    return TableInfo(
        name=name,
        columns=[
            ColumnInfo(name=c, type="INTEGER", nullable=c != "id", pk=c == "id")
            for c in ("id", *columns)
        ],
        foreign_keys=[
            ForeignKeyInfo(column=column, references_table=table, references_column="id")
            for column, table in (fks or {}).items()
        ],
    )


#: A <- B <- C is a chain; D stands alone; E references itself.
SCHEMA = [
    _table("A", "label"),
    _table("B", "a_id", "label", fks={"a_id": "A"}),
    _table("C", "b_id", fks={"b_id": "B"}),
    _table("D"),
    _table("E", "parent_id", fks={"parent_id": "E"}),
]


def _names(tables: list[TableInfo]) -> list[str]:
    return [t.name for t in tables]


def _fks(tables: list[TableInfo]) -> dict[str, list[str]]:
    return {t.name: [fk.references_table for fk in t.foreign_keys] for t in tables}


def test_names_match_ignoring_case_once_each() -> None:
    assert _names(select_tables(SCHEMA, ["a", "A", "nope"])) == ["A"]


@pytest.mark.parametrize("names", [[], ["nope"]], ids=["none", "unknown"])
def test_nothing_named_gives_nothing(names: list[str]) -> None:
    assert select_tables(SCHEMA, names) == []


def test_the_join_path_is_added() -> None:
    tables = select_tables(SCHEMA, ["C", "A"])

    assert _names(tables) == ["A", "B", "C"]
    assert _fks(tables) == {"A": [], "B": ["A"], "C": ["B"]}


def test_foreign_keys_are_kept_even_to_tables_not_returned() -> None:
    assert _fks(select_tables(SCHEMA, ["B"])) == {"B": ["A"]}


def test_an_unreachable_table_is_kept_alone() -> None:
    assert _names(select_tables(SCHEMA, ["D", "A"])) == ["A", "D"]


def test_a_self_reference_is_kept_but_adds_nothing() -> None:
    assert _fks(select_tables(SCHEMA, ["E"])) == {"E": ["E"]}


def test_every_column_is_returned() -> None:
    tables = select_tables(SCHEMA, ["A", "C"])
    assert [[c.name for c in t.columns] for t in tables] == [
        ["id", "label"],
        ["id", "a_id", "label"],
        ["id", "b_id"],
    ]


def test_render_labels_every_key_but_joins_only_shown_tables() -> None:
    alone = render_schema(select_tables(SCHEMA, ["B"]))
    assert "a_id INTEGER REFERENCES A(id)" in alone
    assert alone.endswith("Joins: none")

    path = render_schema(select_tables(SCHEMA, ["A", "C"]))
    assert path.endswith("Joins:\n  B.a_id = A.id\n  C.b_id = B.id")


def test_render_of_nothing_is_empty() -> None:
    assert render_schema([]) == ""


# --------------------------------------------------------------------------- #
# Chinook                                                                       #
# --------------------------------------------------------------------------- #

needs_db = pytest.mark.skipif(not settings.db_path.exists(), reason="run `make db` first")


@pytest.fixture(scope="module")
def chinook() -> list[TableInfo]:
    from domains.sql.tools.executor.app import app

    return SchemaResponse.model_validate(TestClient(app).get("/schema").json()).tables


@needs_db
def test_invoices_reach_artists_through_their_lines(chinook) -> None:
    tables = select_tables(chinook, ["invoice", "ARTIST"])
    assert _names(tables) == ["Album", "Artist", "Invoice", "InvoiceLine", "Track"]


@needs_db
def test_genres_come_with_their_names(chinook) -> None:
    tables = select_tables(chinook, ["Genre", "Track"])

    assert _names(tables) == ["Genre", "Track"]
    assert "Name" in [c.name for c in tables[0].columns]
