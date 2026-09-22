from __future__ import annotations

import pytest
from pydantic import BaseModel

from core.config import settings
from core.llm import LLMRequest, LLMResult, StopReason, get_client


class SimpleOut(BaseModel):
    answer: str


def _providers() -> list[pytest.param]:
    params = []
    if settings.anthropic_api_key:
        params.append(pytest.param(settings.schema_model, id="anthropic"))
    try:
        import ollama

        ollama.Client(host=settings.ollama_host).list()
        params.append(pytest.param("ollama:llama3.2:1b", id="ollama"))
    except (ImportError, OSError):
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
