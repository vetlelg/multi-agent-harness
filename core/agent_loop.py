"""The tool loop: the model picks tools until it answers.

For agentic domains, where the next action is the model's choice rather than a
fixed pipeline edge -- a troubleshooting agent deciding whether to read logs or
query metrics next. A domain step calls :func:`run_tool_loop`; to the engines
it is one step like any other, so both orchestrators run it identically.

Written by hand, like the scratch orchestrator: no framework owns the loop.

Failure policy. A tool failure the model should see and recover from -- bad
arguments, a guard refusal, nothing found -- is raised as :class:`ToolFailed`
and goes back to the model as an error result. Anything else a handler raises
is a bug, and it fails the run rather than being narrated to the model.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from core.config import settings
from core.events import ModelCall, RunContext, ToolCall
from core.llm import (
    AssistantTurn,
    ChatRequest,
    ToolOutcome,
    ToolResults,
    ToolSpec,
    ToolUse,
    Turn,
    UserTurn,
    get_client,
)
from core.prompts import Prompt


class ToolFailed(Exception):
    """An expected tool failure, reported to the model as an error result."""

    def __init__(self, message: str, error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True, slots=True)
class Tool:
    """A tool: what the model is told (name, description, input) and what runs."""

    #: Namespaced by domain, e.g. ``ops.get_pods`` -- the name lands in ToolCall events.
    name: str
    description: str
    input_model: type[BaseModel]
    #: Receives the validated input. Returns a string, a model, or anything JSON-able.
    handler: Callable[[Any], Any]

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, input_model=self.input_model)


@dataclass(frozen=True, slots=True)
class ToolRecord:
    """One tool run, kept for the domain to turn into evidence."""

    tool: str
    input: dict[str, Any]
    ok: bool
    #: What the model was shown: the output, or the error message.
    output: str


@dataclass(frozen=True, slots=True)
class LoopResult:
    #: The model's final text. Empty when the loop ran out of turns.
    answer: str
    #: False when ``max_turns`` ran out before the model stopped calling tools.
    finished: bool
    turns: int
    tool_calls: tuple[ToolRecord, ...]


def run_tool_loop(
    ctx: RunContext,
    *,
    role: str,
    model: str,
    system: Prompt,
    user: str,
    tools: Sequence[Tool],
    max_turns: int,
    max_tokens: int | None = None,
) -> LoopResult:
    """Alternate model turns and tool runs until the model answers or turns run out."""
    client = get_client(model)
    by_name = {tool.name: tool for tool in tools}
    specs = tuple(tool.spec for tool in tools)
    transcript: list[Turn] = [UserTurn(text=user)]
    records: list[ToolRecord] = []

    for turn_number in range(1, max_turns + 1):
        request = ChatRequest(
            system=system.text,
            turns=tuple(transcript),
            tools=specs,
            max_tokens=max_tokens or settings.max_tokens,
        )
        result = client.chat(request)
        ctx.emit(
            ModelCall(
                run_id=ctx.run_id,
                service=ctx.service,
                role=role,
                provider=result.provider,
                model=result.model,
                prompt_id=system.id,
                prompt_sha=system.sha,
                system=system.text,
                prompt=_render(transcript),
                response=_render([result.turn]),
                params={"max_tokens": request.max_tokens, "tools": [s.name for s in specs]},
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                stop_reason=result.stop_reason.value,
                elapsed_ms=result.elapsed_ms,
            )
        )
        transcript.append(result.turn)

        if not result.turn.tool_uses:
            return LoopResult(
                answer=result.turn.text,
                finished=True,
                turns=turn_number,
                tool_calls=tuple(records),
            )

        outcomes = []
        for use in result.turn.tool_uses:
            outcome, record = _run_tool(ctx, by_name, use)
            outcomes.append(outcome)
            records.append(record)
        transcript.append(ToolResults(outcomes=tuple(outcomes)))

    return LoopResult(answer="", finished=False, turns=max_turns, tool_calls=tuple(records))


def _run_tool(
    ctx: RunContext, by_name: dict[str, Tool], use: ToolUse
) -> tuple[ToolOutcome, ToolRecord]:
    start = time.perf_counter()
    output: Any = None
    error: str | None = None
    error_type: str | None = None

    tool = by_name.get(use.name)
    if tool is None:
        error = f"unknown tool {use.name!r}; available: {sorted(by_name)}"
        error_type = "unknown_tool"
    else:
        try:
            args = tool.input_model.model_validate(use.input)
        except ValidationError as exc:
            error = f"invalid input for {use.name}: {exc}"
            error_type = "invalid_input"
        else:
            try:
                output = tool.handler(args)
            except ToolFailed as exc:
                error = str(exc)
                error_type = exc.error_type

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    ok = error is None
    content = _to_text(output) if ok else (error or "")

    ctx.emit(
        ToolCall(
            run_id=ctx.run_id,
            service=ctx.service,
            tool=use.name,
            input=use.input,
            ok=ok,
            output=content if ok else None,
            error_type=error_type,
            error=error,
            elapsed_ms=elapsed_ms,
        )
    )
    outcome = ToolOutcome(tool_use_id=use.id, name=use.name, content=content, is_error=not ok)
    record = ToolRecord(tool=use.name, input=use.input, ok=ok, output=content)
    return outcome, record


def _to_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        return output.model_dump_json()
    return json.dumps(output, default=str)


def _render(turns: Sequence[Turn]) -> str:
    """The transcript as JSON for the run log -- neutral fields only, no native blocks."""
    rendered: list[dict[str, Any]] = []
    for turn in turns:
        if isinstance(turn, UserTurn):
            rendered.append({"role": "user", "text": turn.text})
        elif isinstance(turn, AssistantTurn):
            rendered.append(
                {
                    "role": "assistant",
                    "text": turn.text,
                    "tool_uses": [
                        {"id": u.id, "name": u.name, "input": u.input} for u in turn.tool_uses
                    ],
                }
            )
        else:
            rendered.append(
                {
                    "role": "tool_results",
                    "results": [
                        {
                            "id": o.tool_use_id,
                            "name": o.name,
                            "content": o.content,
                            "error": o.is_error,
                        }
                        for o in turn.outcomes
                    ],
                }
            )
    return json.dumps(rendered, default=str)
