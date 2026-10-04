from __future__ import annotations

import argparse
import json
import sys

import httpx

from core.config import settings
from core.models import (
    AskRequest,
    AskResponse,
    CitationEvidence,
    Evidence,
    QueryEvidence,
    ToolEvidence,
)
from core.registry import load_domain_settings

_TARGETS = ("scratch", "lg")


def _render(item: Evidence) -> str:
    if isinstance(item, QueryEvidence):
        return item.query
    if isinstance(item, CitationEvidence):
        where = f" ({item.locator})" if item.locator else ""
        return f"[{item.source}{where}] {item.excerpt}"
    if isinstance(item, ToolEvidence):
        status = "ok" if item.ok else "failed"
        return f"{item.tool}({json.dumps(item.input)}) -> {status}"
    raise TypeError(f"unknown evidence kind: {item!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask a domain's orchestrator a question.")
    parser.add_argument(
        "--domain",
        default=settings.domain,
        required=settings.domain is None,
        help="Which domain to ask (default: the DOMAIN setting).",
    )
    parser.add_argument("--target", required=True, choices=_TARGETS, help="Which orchestrator.")
    parser.add_argument("question", help="Natural-language question.")
    args = parser.parse_args()

    domain_settings = load_domain_settings(args.domain)
    url = domain_settings.scratch_url if args.target == "scratch" else domain_settings.lg_url
    body = AskRequest(question=args.question)

    resp = httpx.post(f"{url}/ask", json=body.model_dump(), timeout=settings.llm_timeout_s * 4)
    resp.raise_for_status()

    result = AskResponse.model_validate(resp.json())

    for item in result.evidence:
        print(_render(item))
        print()
    print(result.answer)

    sys.exit(0 if result.error is None else 1)


if __name__ == "__main__":
    main()
