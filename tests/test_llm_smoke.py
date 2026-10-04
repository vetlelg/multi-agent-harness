"""Live calls against whichever providers are reachable. Skipped when none are."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from core.agent_loop import Tool, run_tool_loop
from core.config import settings
from core.events import Event, RunContext, ToolCall
from core.llm import LLMRequest, LLMResult, StopReason, get_client
from core.prompts import Prompt

_ANTHROPIC_MODEL = "anthropic:claude-opus-5"


class SimpleOut(BaseModel):
    answer: str


def _providers() -> list[pytest.param]:
    params = []
    if settings.anthropic_api_key and settings.anthropic_api_key.get_secret_value().strip():
        params.append(pytest.param(_ANTHROPIC_MODEL, id="anthropic"))
    try:
        import ollama

        installed = ollama.Client(host=settings.ollama_host).list().models
        if installed:
            params.append(pytest.param(f"ollama:{installed[0].model}", id="ollama"))
    except (ImportError, OSError, ConnectionError):
        pass
    return params


@pytest.mark.parametrize("spec", _providers())
def test_llm_smoke(spec: str) -> None:
    client = get_client(spec)
    request = LLMRequest(
        system="You answer questions briefly.",
        user="What is 2 + 2?",
        output_model=SimpleOut,
        max_tokens=1024,
    )
    result: LLMResult = client.complete(request)

    assert isinstance(result.parsed, SimpleOut)
    assert result.stop_reason == StopReason.FINISHED
    assert result.input_tokens is not None


class _Key(BaseModel):
    key: str


@pytest.mark.parametrize("spec", _providers())
def test_tool_loop_smoke(spec: str) -> None:
    events: list[Event] = []
    result = run_tool_loop(
        RunContext(run_id="smoke", service="test", emit=events.append),
        role="agent",
        model=spec,
        system=Prompt(
            id="smoke",
            text="Answer using the tools. Never guess a value a tool can look up.",
            sha="smoke",
        ),
        user="What is the value stored under the key 'alpha'?",
        tools=[
            Tool(
                name="lookup_value",
                description="Return the value stored under a key.",
                input_model=_Key,
                handler=lambda args: {"key": args.key, "value": "7731"},
            )
        ],
        max_turns=4,
        max_tokens=4096,
    )

    assert result.finished
    assert any(isinstance(e, ToolCall) and e.tool == "lookup_value" for e in events)
    assert "7731" in result.answer
