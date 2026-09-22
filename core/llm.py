"""Provider-agnostic model calls.

The only module in the repo permitted to import a provider SDK.

Every provider does the same thing -- take a Pydantic class, return a validated
instance -- and every one names it differently:

    Anthropic   output_format=    ->  response.parsed_output
    OpenAI      text_format=      ->  response.output_parsed
    Gemini      response_schema=  ->  response.parsed
    Ollama      format=           ->  nothing; you validate the text yourself

Anthropic and OpenAI use the same two words in opposite orders, in both the
parameter and the result. Hence the adapters.

Adapters also normalise token counts, stop reasons, and which parameters are
legal, so the run log is identical regardless of who answered. Anthropic and
Ollama are implemented; OpenAI and Gemini are a class each when wanted.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from core.config import settings


class StopReason(str, Enum):
    """Why generation stopped, in one vocabulary."""

    FINISHED = "finished"
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


class LLMClient(ABC):
    """One model, one provider. Construct via :func:`get_client`."""

    provider: ClassVar[str]

    def __init__(self, model: str) -> None:
        self.model = model

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResult:
        """Run the request and return a validated result, or raise LLMError."""

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

        # Thinking is on by default, so content may open with a thinking block.
        # Select by type; never index content[0].
        raw_text = "".join(b.text for b in response.content if b.type == "text")

        stop_reason = _ANTHROPIC_STOP.get(response.stop_reason or "", StopReason.FAILED)
        detail = ""
        if response.stop_details is not None:
            detail = getattr(response.stop_details, "explanation", "") or ""
        self._check(stop_reason, detail)

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

        done_reason = response.done_reason or ""
        if done_reason:
            stop_reason = _OLLAMA_STOP.get(done_reason, StopReason.FAILED)
        else:
            # Some builds omit done_reason on a clean non-streaming response.
            stop_reason = StopReason.FINISHED if response.done else StopReason.FAILED
        self._check(stop_reason, done_reason)

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
