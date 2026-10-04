"""Building a service.

Every agent, tool server and orchestrator is created with :func:`create_app`.
Cross-cutting behaviour -- the health probe now, OpenTelemetry instrumentation
in the observability milestone -- is added here once, and every service in
every domain inherits it.
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app(service: str) -> FastAPI:
    app = FastAPI(title=service)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": service}

    return app
