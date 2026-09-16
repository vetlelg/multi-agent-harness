"""Every message that crosses a process boundary, plus the shared run state.

These shapes are the contract that makes the two orchestrators interchangeable.
Written as models, a mismatch is a validation error at the boundary it happened
on. Written as dicts, a mismatch is a ``None`` that travels two hops before
failing somewhere unrelated -- hence ``extra="forbid"`` on everything.

This module imports only ``core.errors``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.errors import ErrorType

#: Rows are returned positionally, paired with ``columns``.
Row = list[Any]


class _Strict(BaseModel):
    """Base for every contract model: unknown fields are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Database introspection                                                        #
# --------------------------------------------------------------------------- #


class ColumnInfo(_Strict):
    name: str
    type: str
    nullable: bool
    pk: bool


class ForeignKeyInfo(_Strict):
    column: str
    references_table: str
    references_column: str


class TableInfo(_Strict):
    name: str
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)


class SchemaResponse(_Strict):
    """Body of ``GET /schema`` on the executor."""

    tables: list[TableInfo]


# --------------------------------------------------------------------------- #
# Public contract: POST /ask, identical on both orchestrators                    #
# --------------------------------------------------------------------------- #


class AskRequest(_Strict):
    question: str
    #: Supply to resume a LangGraph run; it is also the checkpointer thread_id.
    #: Omitted means the orchestrator generates one.
    run_id: str | None = None


class AskResponse(_Strict):
    run_id: str
    orchestrator: str
    question: str
    sql: str | None
    columns: list[str] | None
    rows: list[Row] | None
    answer: str
    attempts: int
    #: Set when the run finished without producing data. Still HTTP 200 -- "could
    #: not answer" and "the service is broken" are different events.
    error: str | None


# --------------------------------------------------------------------------- #
# Internal calls                                                                #
# --------------------------------------------------------------------------- #


class SelectSchemaRequest(_Strict):
    run_id: str
    question: str
    previous_sql: str | None = None
    error: str | None = None


class SelectSchemaResponse(_Strict):
    tables: list[TableInfo]
    #: Rendered once, here, and passed onward verbatim, so that the text the
    #: model sees is identical across both orchestrators and reproducible from
    #: the run log.
    schema_text: str


class GenerateSqlRequest(_Strict):
    run_id: str
    question: str
    schema_text: str
    previous_sql: str | None = None
    error: str | None = None
    attempt: int


class GenerateSqlResponse(_Strict):
    sql: str


class ExecuteRequest(_Strict):
    run_id: str
    sql: str


class ExecuteResponse(_Strict):
    ok: bool
    #: The SQL actually run, after the guard rewrote it (e.g. added a LIMIT).
    sql_executed: str | None = None
    columns: list[str] | None = None
    rows: list[Row] | None = None
    row_count: int | None = None
    truncated: bool = False
    elapsed_ms: int
    #: Drives retry routing. Classified by the executor so both orchestrators
    #: branch on the same value.
    error_type: ErrorType | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Model output schemas                                                          #
# --------------------------------------------------------------------------- #
# Handed to LLMRequest.output_model. Keep these flat: optionals, unions and deep
# nesting are exactly where the four providers' schema support diverges.


class SqlOut(_Strict):
    sql: str


class SelectedTableOut(_Strict):
    name: str
    columns: list[str]


class SelectedSchemaOut(_Strict):
    """What the schema agent's model call returns: names only.

    Types, nullability and foreign keys are looked up from the real introspected
    schema rather than re-emitted by the model, so they cannot be hallucinated.
    """

    tables: list[SelectedTableOut]


class AnswerOut(_Strict):
    answer: str


# --------------------------------------------------------------------------- #
# Shared run state                                                              #
# --------------------------------------------------------------------------- #


class RunState(_Strict):
    """State of one run.

    Used by the hand-built loop and, in Step 6, directly as the LangGraph state
    schema -- which is what turns "both orchestrators behave the same" from an
    assertion into a type.
    """

    run_id: str
    question: str
    schema_text: str | None = None
    sql: str | None = None
    columns: list[str] | None = None
    rows: list[Row] | None = None
    error: str | None = None
    error_type: ErrorType | None = None
    #: Executions performed so far. Retry while ``attempts < settings.max_attempts``.
    attempts: int = 0
    answer: str | None = None
