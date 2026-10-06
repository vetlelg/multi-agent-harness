"""Every sql-domain message that crosses a process boundary, plus its run state.

Built on the harness contract in ``core.models``: ``Strict`` (unknown fields are
an error) and ``AgentRequest`` (every internal request carries the run_id).
"""

from __future__ import annotations

from pydantic import Field, model_validator

from core.models import AgentRequest, Row, Strict
from core.pipeline import RunState
from domains.sql.errors import ErrorType

# --------------------------------------------------------------------------- #
# Database introspection                                                        #
# --------------------------------------------------------------------------- #


class ColumnInfo(Strict):
    name: str
    type: str
    nullable: bool
    pk: bool


class ForeignKeyInfo(Strict):
    column: str
    references_table: str
    references_column: str


class TableInfo(Strict):
    name: str
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)


class SchemaResponse(Strict):
    """Body of ``GET /schema`` on the executor."""

    tables: list[TableInfo]


# --------------------------------------------------------------------------- #
# Internal calls                                                                #
# --------------------------------------------------------------------------- #


class SelectSchemaRequest(AgentRequest):
    question: str
    previous_sql: str | None = None
    error: str | None = None


class SelectSchemaResponse(Strict):
    tables: list[TableInfo]
    #: Rendered once, here, and passed onward verbatim, so that the text the
    #: model sees is identical across both orchestrators and reproducible from
    #: the run log.
    schema_text: str


class GenerateSqlRequest(AgentRequest):
    question: str
    schema_text: str
    previous_sql: str | None = None
    error: str | None = None
    attempt: int


class GenerateSqlResponse(Strict):
    """The query, or why the agent declined to write one. Exactly one is set."""

    #: None when the agent found that the schema cannot answer the question.
    sql: str | None = None
    #: What the question needs that the schema lacks.
    missing: str | None = None

    @model_validator(mode="after")
    def _sql_or_missing(self) -> GenerateSqlResponse:
        if (self.sql is None) == (self.missing is None):
            raise ValueError("give exactly one of sql or missing")
        return self


class ExecuteRequest(AgentRequest):
    sql: str


class ExecuteResponse(Strict):
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
# Handed to call_model as output_model. Keep these flat: optionals, unions and
# deep nesting are exactly where the providers' schema support diverges.


class SqlOut(Strict):
    """What the query agent's model call returns.

    Field order is generation order: the model decides whether the schema can
    answer, and says what is missing, before it writes any SQL.
    """

    answerable: bool
    #: What the question needs that the schema lacks; "" when answerable.
    missing: str
    #: "" when not answerable.
    sql: str


class SelectedSchemaOut(Strict):
    """What the schema agent's model call returns: table names only.

    Columns, types, keys and the joins between the tables come from the real
    introspected schema rather than from the model, so they cannot be
    hallucinated or left out.
    """

    tables: list[str]


class AnswerOut(Strict):
    answer: str


# --------------------------------------------------------------------------- #
# Run state                                                                     #
# --------------------------------------------------------------------------- #


class SqlState(RunState):
    """The sql domain's run state: the harness fields plus what this pipeline carries.

    The scratch loop's state and, unchanged, the LangGraph state schema -- which
    is what turns "both orchestrators behave the same" from an assertion into a
    type.
    """

    schema_text: str | None = None
    #: As the query agent wrote it.
    sql: str | None = None
    #: As the executor ran it, after the guard's rewrite.
    sql_executed: str | None = None
    columns: list[str] | None = None
    rows: list[Row] | None = None
    truncated: bool = False
