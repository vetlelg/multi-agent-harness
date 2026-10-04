"""Provider-agnostic model calls.

The only module in the repo permitted to import a provider SDK.

Two kinds of call, each with one vocabulary across providers:

``complete`` -- one prompt in, one validated Pydantic instance out. Every
provider does this, and every one names it differently:

    Anthropic   output_format=    ->  response.parsed_output
    OpenAI      text_format=      ->  response.output_parsed
    Gemini      response_schema=  ->  response.parsed
    Ollama      format=           ->  nothing; you validate the text yourself

Anthropic and OpenAI use the same two words in opposite orders, in both the
parameter and the result. Hence the adapters.

``chat`` -- one turn of a tool-use conversation: a transcript and tool specs
in, the model's text and tool calls out. The transcript is provider-neutral
(:data:`Turn`), so the loop that drives it (``core.agent_loop``) never sees a
provider's message format.

Adapters also normalise token counts, stop reasons, and which parameters are
legal, so the run log is identical regardless of who answered. Anthropic and
Ollama are implemented; OpenAI and Gemini are a class each when wanted.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from core.config import settings
from core.events import ModelCall, RunContext
from core.prompts import Prompt


class StopReason(str, Enum):
    """Why generation stopped, in one vocabulary."""

    FINISHED = "finished"
    #: The model is waiting for tool results. Only ``chat`` returns it.
    TOOL_USE = "tool_use"
    TRUNCATED = "truncated"
    REFUSED = "refused"
    FAILED = "failed"


class LLMError(RuntimeError):
    """Base for every failure this module raises."""


class LLMTruncated(LLMError):
    """Hit the token ceiling.

    Raised rather than returned: a truncated response is a valid HTTP 200 with
    text in it, and returning it means the SQL agent executes half a statement
    and blames the model for a syntax error.
    """


class LLMRefused(LLMError):
    """The provider declined on policy grounds."""


class LLMUnparsable(LLMError):
    """The response did not validate against the requested schema."""


# --------------------------------------------------------------------------- #
# Structured output                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One model call. In-process only; never crosses a service boundary."""

    system: str
    user: str
    output_model: type[BaseModel]
    max_tokens: int = field(default_factory=lambda: settings.max_tokens)
    #: Passed to the provider untouched, minus anything it rejects.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResult:
    """A normalised response. These fields are what land in the run log."""

    parsed: BaseModel
    raw_text: str
    provider: str
    model: str
    stop_reason: StopReason
    elapsed_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None


# --------------------------------------------------------------------------- #
# Tool use                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What the model is told about a tool. What runs is ``core.agent_loop``'s business."""

    name: str
    description: str
    input_model: type[BaseModel]


@dataclass(frozen=True, slots=True)
class ToolUse:
    """The model asking for a tool to run."""

    #: Pairs the result with the request. Providers without ids get positional ones.
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What a tool run produced, as handed back to the model."""

    tool_use_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class UserTurn:
    text: str


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    text: str
    tool_uses: tuple[ToolUse, ...] = ()
    #: Which adapter produced this turn, and its native content. The same
    #: adapter replays ``native`` verbatim on the next call -- Claude's thinking
    #: blocks must come back unchanged. Any other adapter rebuilds the turn from
    #: the neutral fields.
    provider: str = ""
    native: Any = None


@dataclass(frozen=True, slots=True)
class ToolResults:
    outcomes: tuple[ToolOutcome, ...]


Turn = UserTurn | AssistantTurn | ToolResults


@dataclass(frozen=True, slots=True)
class ChatRequest:
    system: str
    turns: tuple[Turn, ...]
    tools: tuple[ToolSpec, ...]
    max_tokens: int = field(default_factory=lambda: settings.max_tokens)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatResult:
    turn: AssistantTurn
    provider: str
    model: str
    stop_reason: StopReason
    elapsed_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None


# --------------------------------------------------------------------------- #
# Client interface                                                              #
# --------------------------------------------------------------------------- #


class LLMClient(ABC):
    """One model, one provider. Construct via :func:`get_client`."""

    provider: ClassVar[str]

    def __init__(self, model: str) -> None:
        self.model = model

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResult:
        """Run the request and return a validated result, or raise LLMError."""

    def chat(self, request: ChatRequest) -> ChatResult:
        """Run one tool-use turn, or raise LLMError."""
        raise LLMError(f"the {self.provider} adapter has no tool-use support yet")

    def _check(self, stop_reason: StopReason, detail: str = "") -> None:
        suffix = f": {detail}" if detail else ""
        if stop_reason is StopReason.TRUNCATED:
            raise LLMTruncated(
                f"{self.provider}/{self.model} hit max_tokens{suffix}. "
                "Raise max_tokens; do not use the partial output."
            )
        if stop_reason is StopReason.REFUSED:
            raise LLMRefused(f"{self.provider}/{self.model} refused{suffix}")
        if stop_reason is StopReason.FAILED:
            raise LLMError(f"{self.provider}/{self.model} stopped unexpectedly{suffix}")


# --------------------------------------------------------------------------- #
# Anthropic                                                                     #
# --------------------------------------------------------------------------- #

# Sampling parameters were removed on current Claude models; sending one is a
# 400. Dropped here rather than at the call site so callers stay provider-blind.
_ANTHROPIC_REJECTS = frozenset({"temperature", "top_p", "top_k"})

_ANTHROPIC_STOP: dict[str, StopReason] = {
    "end_turn": StopReason.FINISHED,
    "stop_sequence": StopReason.FINISHED,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.TRUNCATED,
    "refusal": StopReason.REFUSED,
}


class AnthropicAdapter(LLMClient):
    provider: ClassVar[str] = "anthropic"

    def __init__(self, model: str) -> None:
        super().__init__(model)
        import anthropic

        key = settings.anthropic_api_key
        self._client = anthropic.Anthropic(
            api_key=key.get_secret_value() if key else None,
            timeout=settings.llm_timeout_s,
        )

    def complete(self, request: LLMRequest) -> LLMResult:
        extras = {k: v for k, v in request.extras.items() if k not in _ANTHROPIC_REJECTS}

        start = time.perf_counter()
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": "user", "content": request.user}],
            output_format=request.output_model,
            **extras,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        raw_text = self._text(response)
        stop_reason = self._stop_reason(response)
        self._check(stop_reason, self._stop_detail(response))

        parsed = response.parsed_output
        if parsed is None:
            raise LLMUnparsable(
                f"anthropic/{self.model} returned no parsed output for "
                f"{request.output_model.__name__}; raw text was {raw_text[:200]!r}"
            )

        return LLMResult(
            parsed=parsed,
            raw_text=raw_text,
            provider=self.provider,
            model=response.model or self.model,
            stop_reason=stop_reason,
            elapsed_ms=elapsed_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def chat(self, request: ChatRequest) -> ChatResult:
        extras = {k: v for k, v in request.extras.items() if k not in _ANTHROPIC_REJECTS}
        tools = [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_model.model_json_schema(),
            }
            for spec in request.tools
        ]
        if tools:
            extras["tools"] = tools

        start = time.perf_counter()
        response = self._client.messages.create(
            model=self.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[self._message(turn) for turn in request.turns],
            **extras,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        stop_reason = self._stop_reason(response)
        self._check(stop_reason, self._stop_detail(response))

        tool_uses = tuple(
            ToolUse(id=block.id, name=block.name, input=dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        )
        turn = AssistantTurn(
            text=self._text(response),
            tool_uses=tool_uses,
            provider=self.provider,
            native=list(response.content),
        )
        return ChatResult(
            turn=turn,
            provider=self.provider,
            model=response.model or self.model,
            stop_reason=stop_reason,
            elapsed_ms=elapsed_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def _message(self, turn: Turn) -> dict[str, Any]:
        if isinstance(turn, UserTurn):
            return {"role": "user", "content": turn.text}
        if isinstance(turn, AssistantTurn):
            if turn.provider == self.provider and turn.native is not None:
                return {"role": "assistant", "content": turn.native}
            content: list[dict[str, Any]] = []
            if turn.text:
                content.append({"type": "text", "text": turn.text})
            content.extend(
                {"type": "tool_use", "id": use.id, "name": use.name, "input": use.input}
                for use in turn.tool_uses
            )
            return {"role": "assistant", "content": content}
        # Every result of a turn goes back in one user message; splitting them
        # teaches the model to stop making parallel calls.
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": outcome.tool_use_id,
                    "content": outcome.content,
                    "is_error": outcome.is_error,
                }
                for outcome in turn.outcomes
            ],
        }

    @staticmethod
    def _text(response: Any) -> str:
        # Thinking is on by default, so content may open with a thinking block.
        # Select by type; never index content[0].
        return "".join(b.text for b in response.content if b.type == "text")

    @staticmethod
    def _stop_reason(response: Any) -> StopReason:
        return _ANTHROPIC_STOP.get(response.stop_reason or "", StopReason.FAILED)

    @staticmethod
    def _stop_detail(response: Any) -> str:
        if response.stop_details is None:
            return ""
        return getattr(response.stop_details, "explanation", "") or ""


# --------------------------------------------------------------------------- #
# Ollama                                                                        #
# --------------------------------------------------------------------------- #

_OLLAMA_STOP: dict[str, StopReason] = {
    "stop": StopReason.FINISHED,
    "length": StopReason.TRUNCATED,
}


class OllamaAdapter(LLMClient):
    """Local models.

    Ollama constrains generation to the schema but hands back a plain string, so
    this adapter does the validation the hosted providers do for you.
    """

    provider: ClassVar[str] = "ollama"

    def __init__(self, model: str) -> None:
        super().__init__(model)
        import ollama

        self._client = ollama.Client(host=settings.ollama_host)

    def complete(self, request: LLMRequest) -> LLMResult:
        # Ollama has no max_tokens argument; the ceiling is options.num_predict.
        options: dict[str, Any] = {"num_predict": request.max_tokens, **request.extras}

        start = time.perf_counter()
        response = self._client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            format=request.output_model.model_json_schema(),
            options=options,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        raw_text = response.message.content or ""
        stop_reason = self._stop_reason(response)
        self._check(stop_reason, response.done_reason or "")

        try:
            parsed = request.output_model.model_validate_json(raw_text)
        except ValidationError as exc:
            raise LLMUnparsable(
                f"ollama/{self.model} output did not match {request.output_model.__name__}: {exc}"
            ) from exc

        return LLMResult(
            parsed=parsed,
            raw_text=raw_text,
            provider=self.provider,
            model=response.model or self.model,
            stop_reason=stop_reason,
            elapsed_ms=elapsed_ms,
            input_tokens=response.prompt_eval_count,
            output_tokens=response.eval_count,
        )

    def chat(self, request: ChatRequest) -> ChatResult:
        options: dict[str, Any] = {"num_predict": request.max_tokens, **request.extras}
        tools = [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_model.model_json_schema(),
                },
            }
            for spec in request.tools
        ]
        messages: list[Any] = [{"role": "system", "content": request.system}]
        for turn in request.turns:
            messages.extend(self._messages(turn))

        start = time.perf_counter()
        response = self._client.chat(
            model=self.model,
            messages=messages,
            tools=tools or None,
            options=options,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        message = response.message
        # Ollama assigns no ids. Positional ones are unique within the turn,
        # which is all the pairing needs.
        tool_uses = tuple(
            ToolUse(id=f"call_{i}", name=call.function.name, input=dict(call.function.arguments))
            for i, call in enumerate(message.tool_calls or ())
        )
        stop_reason = self._stop_reason(response)
        # Ollama reports "stop" whether or not it called a tool.
        if stop_reason is StopReason.FINISHED and tool_uses:
            stop_reason = StopReason.TOOL_USE
        self._check(stop_reason, response.done_reason or "")

        turn = AssistantTurn(
            text=message.content or "",
            tool_uses=tool_uses,
            provider=self.provider,
            native=message,
        )
        return ChatResult(
            turn=turn,
            provider=self.provider,
            model=response.model or self.model,
            stop_reason=stop_reason,
            elapsed_ms=elapsed_ms,
            input_tokens=response.prompt_eval_count,
            output_tokens=response.eval_count,
        )

    def _messages(self, turn: Turn) -> list[Any]:
        if isinstance(turn, UserTurn):
            return [{"role": "user", "content": turn.text}]
        if isinstance(turn, AssistantTurn):
            if turn.provider == self.provider and turn.native is not None:
                return [turn.native]
            return [
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {"function": {"name": use.name, "arguments": use.input}}
                        for use in turn.tool_uses
                    ],
                }
            ]
        # Ollama pairs results with calls by tool name, one message per result.
        return [
            {"role": "tool", "content": outcome.content, "tool_name": outcome.name}
            for outcome in turn.outcomes
        ]

    @staticmethod
    def _stop_reason(response: Any) -> StopReason:
        done_reason = response.done_reason or ""
        if done_reason:
            return _OLLAMA_STOP.get(done_reason, StopReason.FAILED)
        # Some builds omit done_reason on a clean non-streaming response.
        return StopReason.FINISHED if response.done else StopReason.FAILED


# --------------------------------------------------------------------------- #
# Factory                                                                       #
# --------------------------------------------------------------------------- #

_ADAPTERS: dict[str, type[LLMClient]] = {
    AnthropicAdapter.provider: AnthropicAdapter,
    OllamaAdapter.provider: OllamaAdapter,
}


@cache
def get_client(spec: str) -> LLMClient:
    """Resolve a ``provider:model`` string to a client.

    Cached, so each spec builds exactly one SDK client for the process rather
    than one per call. Split on the first colon only -- Ollama model names
    contain colons themselves, as in ``ollama:llama3.1:8b``.
    """
    provider, separator, model = spec.partition(":")
    if not separator or not model:
        raise ValueError(f"expected 'provider:model', got {spec!r}")

    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(f"no adapter for provider {provider!r}; implemented: {sorted(_ADAPTERS)}")
    return adapter(model)


# --------------------------------------------------------------------------- #
# Logged calls                                                                  #
# --------------------------------------------------------------------------- #


def call_model(
    ctx: RunContext,
    *,
    role: str,
    model: str,
    system: Prompt,
    user: str,
    output_model: type[BaseModel],
    max_tokens: int | None = None,
    extras: Mapping[str, Any] | None = None,
) -> LLMResult:
    """Make one structured call and emit its ``ModelCall`` event.

    The way every agent and every answer step calls a model, so the run log
    records the same fields whichever domain made the call.
    """
    request = LLMRequest(
        system=system.text,
        user=user,
        output_model=output_model,
        max_tokens=max_tokens or settings.max_tokens,
        extras=dict(extras or {}),
    )
    result = get_client(model).complete(request)

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
            prompt=user,
            response=result.raw_text,
            params={"max_tokens": request.max_tokens, **request.extras},
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            stop_reason=result.stop_reason.value,
            elapsed_ms=result.elapsed_ms,
        )
    )
    return result
