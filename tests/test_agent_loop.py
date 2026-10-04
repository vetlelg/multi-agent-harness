from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from core import agent_loop
from core.agent_loop import Tool, ToolFailed, run_tool_loop
from core.events import Event, ModelCall, RunContext, ToolCall
from core.llm import (
    AssistantTurn,
    ChatRequest,
    ChatResult,
    LLMClient,
    LLMRequest,
    LLMResult,
    StopReason,
    ToolResults,
    ToolUse,
)
from core.prompts import Prompt

_SYSTEM = Prompt(id="test/system.txt", text="Use tools.", sha="abc123abc123")


class _Scripted(LLMClient):
    """Replays prepared assistant turns and records every request."""

    provider = "fake"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        super().__init__("scripted")
        self._turns = list(turns)
        self.requests: list[ChatRequest] = []

    def complete(self, request: LLMRequest) -> LLMResult:
        raise NotImplementedError

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        turn = self._turns.pop(0)
        return ChatResult(
            turn=turn,
            provider=self.provider,
            model=self.model,
            stop_reason=StopReason.TOOL_USE if turn.tool_uses else StopReason.FINISHED,
            elapsed_ms=1,
        )


class _Lookup(BaseModel):
    key: str


def _lookup(args: _Lookup) -> dict[str, Any]:
    if args.key == "missing":
        raise ToolFailed("no such key", error_type="not_found")
    if args.key == "bug":
        raise KeyError("bug in the handler")
    return {"key": args.key, "value": 42}


_TOOLS = [
    Tool(name="test.lookup", description="Look up a key.", input_model=_Lookup, handler=_lookup)
]


def _call(name: str, **args: Any) -> AssistantTurn:
    return AssistantTurn(text="", tool_uses=(ToolUse(id="t1", name=name, input=args),))


def _run(monkeypatch, turns: list[AssistantTurn], max_turns: int = 5):
    client = _Scripted(turns)
    monkeypatch.setattr(agent_loop, "get_client", lambda spec: client)
    events: list[Event] = []
    ctx = RunContext(run_id="r1", service="test", emit=events.append)
    result = run_tool_loop(
        ctx,
        role="agent",
        model="fake:scripted",
        system=_SYSTEM,
        user="What is x?",
        tools=_TOOLS,
        max_turns=max_turns,
    )
    return result, events, client


def test_tool_then_answer(monkeypatch) -> None:
    result, events, client = _run(
        monkeypatch, [_call("test.lookup", key="x"), AssistantTurn(text="x is 42")]
    )

    assert result.finished is True
    assert result.answer == "x is 42"
    assert result.turns == 2
    assert [r.tool for r in result.tool_calls] == ["test.lookup"]
    assert result.tool_calls[0].ok is True
    assert [type(e) for e in events] == [ModelCall, ToolCall, ModelCall]

    model_call = events[0]
    assert model_call.prompt_id == "test/system.txt"
    assert model_call.prompt_sha == "abc123abc123"

    # The second request carries the first turn and its results.
    second = client.requests[1].turns
    assert isinstance(second[1], AssistantTurn)
    assert isinstance(second[2], ToolResults)
    assert second[2].outcomes[0].tool_use_id == "t1"
    assert '"value": 42' in second[2].outcomes[0].content


@pytest.mark.parametrize(
    ("turn", "error_type"),
    [
        (_call("test.nope"), "unknown_tool"),
        (_call("test.lookup", wrong="x"), "invalid_input"),
        (_call("test.lookup", key="missing"), "not_found"),
    ],
    ids=["unknown-tool", "invalid-input", "tool-failed"],
)
def test_expected_failures_go_back_to_the_model(monkeypatch, turn, error_type) -> None:
    result, events, client = _run(monkeypatch, [turn, AssistantTurn(text="cannot tell")])

    assert result.finished is True
    assert result.tool_calls[0].ok is False
    tool_event = next(e for e in events if isinstance(e, ToolCall))
    assert tool_event.ok is False
    assert tool_event.error_type == error_type
    outcome = client.requests[1].turns[2].outcomes[0]
    assert outcome.is_error is True


def test_handler_bug_fails_the_run(monkeypatch) -> None:
    with pytest.raises(KeyError, match="bug in the handler"):
        _run(monkeypatch, [_call("test.lookup", key="bug")])


def test_running_out_of_turns(monkeypatch) -> None:
    result, _events, _client = _run(
        monkeypatch, [_call("test.lookup", key="x"), _call("test.lookup", key="y")], max_turns=2
    )

    assert result.finished is False
    assert result.answer == ""
    assert result.turns == 2
    assert len(result.tool_calls) == 2
