"""The run log: typed, append-only events.

One JSON object per line, to stdout always and to ``runs/<run_id>.jsonl`` when
``RUNS_DIR`` is set.

These are events rather than log lines because the hand-built orchestrator later
becomes the event-sourced one. A typed, numbered, validated record today makes
that a refactor; free-form text would make it a rewrite. This is a decision about
the *shape of the log* only -- there is no event store, no projections and no
replay here, and there should not be until that milestone.

The event types are the same for every domain. A domain's tool activity is a
:class:`ToolCall` with a namespaced tool name (``sql.execute``), never a new
event type -- so parsers, dashboards and evals never learn about domains.

Sequence numbers: a run crosses several processes, which cannot share a counter.
Orchestrators number their events and own the ordered log of record. Agents and
tool servers emit the same run_id for correlation but leave ``seq`` unset and
make no claim to be replayable.

Segments: a resumed run is executed by more than one ``/ask`` call, possibly in
more than one process, and a new process cannot know how far the last one
numbered -- events emitted after the last checkpoint were never persisted
anywhere it can read. So each call is a segment with its own ``seq`` from 1, and
a run's log of record is ordered by ``(segment, seq)``.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from core.config import settings

#: Bump when an event's fields change incompatibly, so consumers can tell.
#: 2: domain on RunStart; prompt id/sha, system and params on ModelCall;
#:    ToolCall replaced SqlExecute; error_type became a plain string.
#: 3: segment on every event, set with seq.
SCHEMA_VERSION = 3


def _utc_now() -> str:
    """UTC, millisecond precision, ``Z`` suffix."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    #: Set by orchestrators only: which ``/ask`` call of the run emitted this.
    #: 1 for a new run, one more for each resume.
    segment: int | None = None
    #: Set by orchestrators only, starting at 1 and incrementing per segment.
    seq: int | None = None


class RunStart(Event):
    type: Literal["run_start"] = "run_start"
    question: str
    domain: str
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
    #: Which role in the domain made the call, e.g. schema, query, answer.
    role: str
    provider: str
    model: str
    #: Which prompt file, and which version of its text, was the system prompt.
    prompt_id: str | None = None
    prompt_sha: str | None = None
    #: Full prompt and response, deliberately. This is what makes a run
    #: reproducible -- and why no credential may ever reach a prompt. For a
    #: tool-use turn, ``prompt`` is the whole transcript so far, as JSON.
    system: str
    prompt: str
    response: str
    #: What was sent besides the prompt: max_tokens and any extras.
    params: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str
    elapsed_ms: int


class ToolCall(Event):
    """A tool ran: a tool server's operation, or a tool in an agent loop."""

    type: Literal["tool_call"] = "tool_call"
    #: Namespaced by domain, e.g. ``sql.execute``.
    tool: str
    input: dict[str, Any]
    ok: bool
    output: Any = None
    #: The domain's own classification, as a string.
    error_type: str | None = None
    error: str | None = None
    elapsed_ms: int


class RouteDecision(Event):
    """The event that makes retry routing provable rather than believed."""

    type: Literal["route_decision"] = "route_decision"
    attempt: int
    error_type: str | None = None
    #: The step chosen next.
    branch: str


class RunEnd(Event):
    type: Literal["run_end"] = "run_end"
    attempts: int
    ok: bool
    answer: str | None = None
    error: str | None = None


AnyEvent = Annotated[
    RunStart | HttpCall | ModelCall | ToolCall | RouteDecision | RunEnd,
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


Emitter = Callable[[Event], None]


def sequenced(emitter: Emitter = emit, *, segment: int = 1) -> Emitter:
    """An emitter that stamps ``segment`` and numbers events 1, 2, 3 ... before passing them on.

    One per segment, created by the orchestrator: ``segment=1`` for a new run, the
    persisted next number when resuming. Agents never use it.
    """
    if segment < 1:
        raise ValueError(f"segments are numbered from 1, got {segment}")
    counter = itertools.count(1)

    def _emit(event: Event) -> None:
        emitter(event.model_copy(update={"segment": segment, "seq": next(counter)}))

    return _emit


@dataclass(frozen=True, slots=True)
class RunContext:
    """Which run, which service, and where its events go.

    Passed to every instrumented call (``core.http``, ``core.llm.call_model``,
    ``core.agent_loop``) so events carry ``run_id`` and ``service`` without
    each caller restating them. Orchestrators pass a :func:`sequenced` emitter;
    agents and tool servers use the plain :func:`emit`.
    """

    run_id: str
    service: str
    emit: Emitter = emit
