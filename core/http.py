"""Calls between services, each one logged as an ``HttpCall`` event.

Every inter-service call in every domain goes through here, which is what makes
the HTTP side of the run log uniform -- and, in the observability milestone,
the one place the httpx client is instrumented.
"""

from __future__ import annotations

import time

import httpx
from pydantic import BaseModel

from core.config import settings
from core.events import HttpCall, RunContext

# An agent's response waits on a model call inside it, so the budget is a model
# call plus a service call.
_client = httpx.Client(timeout=settings.llm_timeout_s + settings.http_timeout_s)


def get[T: BaseModel](
    ctx: RunContext, *, target: str, base_url: str, path: str, response_model: type[T]
) -> T:
    return _call(ctx, "GET", target, base_url, path, None, response_model)


def post[T: BaseModel](
    ctx: RunContext,
    *,
    target: str,
    base_url: str,
    path: str,
    body: BaseModel,
    response_model: type[T],
) -> T:
    return _call(ctx, "POST", target, base_url, path, body, response_model)


def _call[T: BaseModel](
    ctx: RunContext,
    method: str,
    target: str,
    base_url: str,
    path: str,
    body: BaseModel | None,
    response_model: type[T],
) -> T:
    start = time.perf_counter()
    response = _client.request(
        method,
        f"{base_url}{path}",
        json=body.model_dump(mode="json") if body is not None else None,
    )
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    ctx.emit(
        HttpCall(
            run_id=ctx.run_id,
            service=ctx.service,
            target=target,
            method=method,
            path=path,
            status=response.status_code,
            elapsed_ms=elapsed_ms,
        )
    )
    response.raise_for_status()
    return response_model.model_validate(response.json())
