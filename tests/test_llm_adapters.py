"""Adapter translation, checked offline against stand-in SDK responses."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from core.llm import (
    AnthropicAdapter,
    AssistantTurn,
    ChatRequest,
    LLMTruncated,
    OllamaAdapter,
    StopReason,
    ToolOutcome,
    ToolResults,
    ToolSpec,
    ToolUse,
    UserTurn,
)


class _Args(BaseModel):
    key: str


_SPEC = ToolSpec(name="test.lookup", description="Look up a key.", input_model=_Args)
_TRANSCRIPT = (
    UserTurn(text="What is x?"),
    AssistantTurn(
        text="Checking.", tool_uses=(ToolUse(id="t1", name="test.lookup", input={"key": "x"}),)
    ),
    ToolResults(outcomes=(ToolOutcome(tool_use_id="t1", name="test.lookup", content="42"),)),
)


class _Recorder:
    def __init__(self, response) -> None:
        self.response = response
        self.kwargs: dict = {}

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _bare(adapter_cls, recorder: _Recorder):
    """An adapter with its SDK client replaced -- no network, no credentials."""
    adapter = object.__new__(adapter_cls)
    adapter.model = "m"
    if adapter_cls is AnthropicAdapter:
        adapter._client = SimpleNamespace(messages=SimpleNamespace(create=recorder))
    else:
        adapter._client = SimpleNamespace(chat=recorder)
    return adapter


# --------------------------------------------------------------------------- #
# Anthropic                                                                     #
# --------------------------------------------------------------------------- #


def _anthropic_response(stop_reason: str, *blocks) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        stop_details=None,
        model="claude-x",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def test_anthropic_tool_use_turn() -> None:
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    text = SimpleNamespace(type="text", text="Let me look.")
    use = SimpleNamespace(type="tool_use", id="tu_1", name="test.lookup", input={"key": "x"})
    recorder = _Recorder(_anthropic_response("tool_use", thinking, text, use))
    adapter = _bare(AnthropicAdapter, recorder)

    result = adapter.chat(
        ChatRequest(system="sys", turns=_TRANSCRIPT, tools=(_SPEC,), max_tokens=100)
    )

    assert result.stop_reason is StopReason.TOOL_USE
    assert result.turn.text == "Let me look."
    assert result.turn.tool_uses == (ToolUse(id="tu_1", name="test.lookup", input={"key": "x"}),)
    # Native content is kept whole, thinking block included, for replay.
    assert result.turn.native == [thinking, text, use]

    sent = recorder.kwargs
    assert sent["tools"][0]["name"] == "test.lookup"
    assert sent["tools"][0]["input_schema"]["required"] == ["key"]
    assert sent["messages"][0] == {"role": "user", "content": "What is x?"}
    assert sent["messages"][1]["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "test.lookup",
        "input": {"key": "x"},
    }
    assert sent["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "42", "is_error": False}
    ]


def test_anthropic_replays_its_own_native_content() -> None:
    native = [SimpleNamespace(type="thinking", thinking="", signature="sig")]
    turn = AssistantTurn(text="", provider="anthropic", native=native)
    message = _bare(AnthropicAdapter, _Recorder(None))._message(turn)
    assert message == {"role": "assistant", "content": native}


def test_anthropic_drops_sampling_params() -> None:
    recorder = _Recorder(_anthropic_response("end_turn", SimpleNamespace(type="text", text="hi")))
    adapter = _bare(AnthropicAdapter, recorder)
    adapter.chat(
        ChatRequest(system="s", turns=(UserTurn(text="hi"),), tools=(), extras={"temperature": 0})
    )
    assert "temperature" not in recorder.kwargs
    assert "tools" not in recorder.kwargs


def test_anthropic_truncation_raises() -> None:
    recorder = _Recorder(_anthropic_response("max_tokens"))
    with pytest.raises(LLMTruncated):
        _bare(AnthropicAdapter, recorder).chat(
            ChatRequest(system="s", turns=(UserTurn(text="hi"),), tools=())
        )


# --------------------------------------------------------------------------- #
# Ollama                                                                        #
# --------------------------------------------------------------------------- #


def _ollama_response(content: str, calls=None, done_reason: str = "stop") -> SimpleNamespace:
    message = SimpleNamespace(role="assistant", content=content, tool_calls=calls)
    return SimpleNamespace(
        message=message,
        done_reason=done_reason,
        done=True,
        model="llama",
        prompt_eval_count=10,
        eval_count=5,
    )


def test_ollama_tool_use_turn() -> None:
    call = SimpleNamespace(function=SimpleNamespace(name="test.lookup", arguments={"key": "x"}))
    recorder = _Recorder(_ollama_response("", calls=[call]))
    adapter = _bare(OllamaAdapter, recorder)

    result = adapter.chat(
        ChatRequest(system="sys", turns=_TRANSCRIPT, tools=(_SPEC,), max_tokens=100)
    )

    # Ollama says "stop" either way; a turn with calls is a tool-use turn.
    assert result.stop_reason is StopReason.TOOL_USE
    assert result.turn.tool_uses == (ToolUse(id="call_0", name="test.lookup", input={"key": "x"}),)

    sent = recorder.kwargs
    assert sent["tools"][0]["function"]["name"] == "test.lookup"
    assert sent["options"]["num_predict"] == 100
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["messages"][2]["tool_calls"] == [
        {"function": {"name": "test.lookup", "arguments": {"key": "x"}}}
    ]
    assert sent["messages"][3] == {"role": "tool", "content": "42", "tool_name": "test.lookup"}


def test_ollama_plain_answer() -> None:
    recorder = _Recorder(_ollama_response("x is 42"))
    result = _bare(OllamaAdapter, recorder).chat(
        ChatRequest(system="s", turns=(UserTurn(text="hi"),), tools=())
    )
    assert result.stop_reason is StopReason.FINISHED
    assert result.turn.text == "x is 42"
    assert recorder.kwargs["tools"] is None
