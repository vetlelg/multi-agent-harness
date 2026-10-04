from __future__ import annotations

from core.config import settings
from core.models import AskRequest, AskResponse
from core.registry import available_domains, load_domain
from core.service import create_app
from orchestrator_scratch import engine

if settings.domain is None:
    raise RuntimeError(
        f"Set DOMAIN to the domain this orchestrator serves; available: {available_domains()}"
    )

_DOMAIN = load_domain(settings.domain)

app = create_app(engine.SERVICE)


@app.post("/ask")
def ask(request: AskRequest) -> AskResponse:
    return engine.run(_DOMAIN, request)
