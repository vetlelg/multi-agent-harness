"""The run log: typed, append-only events.

One JSON object per line, to stdout always and to ``runs/<run_id>.jsonl`` when
``RUNS_DIR`` is set.

These are events rather than log lines because the hand-built orchestrator later
becomes the event-sourced one. A typed, numbered, validated record today makes
that a refactor; free-form text would make it a rewrite. This is a decision about
the *shape of the log* only -- there is no event store, no projections and no
replay here, and there should not be until that milestone.

Sequence numbers: a run crosses four processes, which cannot share a counter.
Orchestrators number their events and own the ordered log of record. Agents emit
the same run_id for correlation but leave ``seq`` unset and make no claim to be
replayable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from core.config import settings
from core.errors import ErrorType

#: Bump when an event's fields change incompatibly, so consumers can tell.
SCHEMA_VERSION = 1


def _utc_now() -> str:
    """UTC, millisecond precision, ``Z`` suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_run_id() -> str:
    return str(uuid.uuid4())


class Event(BaseModel):
    """Fields common to every event."""

    model_config = ConfigDict(extra="forbid")

    v: int = SCHEMA_VERSION
    ts: str = Field(default_factory=_utc_now)
    run_id: str
    service: str
    type: str
    #: Set by orchestrators only, starting at 1 and incrementing per run.
    seq: int | None = None


class RunStart(Event):
    type: Literal["run_start"] = "run_start"
    question: str
    orchestrator: str


class HttpCall(Event):
    type: Literal["http_call"] = "http_call"
    target: str
    method: str
    path: str
    status: int
    elapsed_ms: int


class ModelCall(Event):
    type: Literal["model_call"] = "model_call"
    #: Which pipeline role made the call: schema, sql or answer.
    role: str
    provider: str
    model: str
    #: Full prompt and response, deliberately. This is what makes a run
    #: reproducible -- and why no credential may ever reach a prompt.
    prompt: str
    response: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str
    elapsed_ms: int


class SqlExecute(Event):
    type: Literal["sql_execute"] = "sql_execute"
    sql: str
    ok: bool
    row_count: int | None = None
    error_type: ErrorType | None = None
    error: str | None = None
    elapsed_ms: int


class RouteDecision(Event):
    """The event that makes retry routing provable rather than believed."""

    type: Literal["route_decision"] = "route_decision"
    attempt: int
    error_type: ErrorType | None = None
    #: The node or step chosen next.
    branch: str


class RunEnd(Event):
    type: Literal["run_end"] = "run_end"
    attempts: int
    ok: bool
    answer: str | None = None
    error: str | None = None


AnyEvent = Annotated[
    RunStart | HttpCall | ModelCall | SqlExecute | RouteDecision | RunEnd,
    Field(discriminator="type"),
]

_EVENT_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


def parse_event(line: str) -> AnyEvent:
    """Read one line of a run file back into its event class."""
    return _EVENT_ADAPTER.validate_json(line)


def emit(event: Event) -> None:
    """Write one event: stdout always, run file when ``RUNS_DIR`` is set.

    Append only. Never rewrite, never delete -- a log you can edit is not
    evidence, and it lets the observability agent tail rather than re-read.
    """
    line = event.model_dump_json()
    print(line, flush=True)

    runs_dir = settings.runs_dir
    if runs_dir is None:
        return

    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{event.run_id}.jsonl"
    # newline="\n" so the file is identical on Windows and in the containers.
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
