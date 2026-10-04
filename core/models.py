"""The public contract every domain and both orchestrators share.

These shapes are what make domains and orchestrators interchangeable behind one
CLI and one test runner. Written as models, a mismatch is a validation error at
the boundary it happened on. Written as dicts, a mismatch is a ``None`` that
travels two hops before failing somewhere unrelated -- hence ``extra="forbid"``
on everything.

Domain-specific messages live in ``domains/<name>/models.py`` and build on
:class:`Strict` and :class:`AgentRequest`.

This module imports nothing of ours.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Rows are returned positionally, paired with ``columns``.
Row = list[Any]


class Strict(BaseModel):
    """Base for every contract model: unknown fields are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid")


class AgentRequest(Strict):
    """Base for every request between services.

    Inheriting from it is what puts the ``run_id`` on every inter-service call
    by construction rather than by remembering to.
    """

    run_id: str


# --------------------------------------------------------------------------- #
# Evidence: what an answer rests on                                             #
# --------------------------------------------------------------------------- #
# Kinds are presentation types, not domain types -- a tabular query result looks
# the same whether the query was SQL or PromQL. A new domain picks from these;
# a genuinely new kind is a harness change, made once for every domain.


class QueryEvidence(Strict):
    """A query that was run and the table it returned."""

    kind: Literal["query"] = "query"
    #: e.g. "sql", "promql".
    language: str
    query: str
    columns: list[str] | None = None
    rows: list[Row] | None = None
    #: True when a row limit cut the result short.
    truncated: bool = False


class CitationEvidence(Strict):
    """A passage the answer quotes or relies on."""

    kind: Literal["citation"] = "citation"
    source: str
    #: Where in the source: a section, page or line range.
    locator: str | None = None
    excerpt: str


class ToolEvidence(Strict):
    """A tool call the agent made and what came back."""

    kind: Literal["tool"] = "tool"
    tool: str
    input: dict[str, Any]
    output: str
    ok: bool


Evidence = Annotated[
    QueryEvidence | CitationEvidence | ToolEvidence,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# Public contract: POST /ask, identical on every domain and both orchestrators   #
# --------------------------------------------------------------------------- #


class AskRequest(Strict):
    question: str
    #: Supply to resume a LangGraph run; it is also the checkpointer thread_id.
    #: Omitted means the orchestrator generates one.
    run_id: str | None = None


class AskResponse(Strict):
    run_id: str
    domain: str
    orchestrator: str
    question: str
    answer: str
    evidence: list[Evidence]
    attempts: int
    #: Set when the run finished without producing data. Still HTTP 200 -- "could
    #: not answer" and "the service is broken" are different events.
    error: str | None
