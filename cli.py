from __future__ import annotations

import argparse
import sys

import httpx

from core.config import settings
from core.models import AskRequest, AskResponse

_TARGETS = {
    "scratch": lambda: settings.orchestrator_scratch_url,
    "lg": lambda: settings.orchestrator_lg_url,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask the SQL agent a question.")
    parser.add_argument(
        "--target", required=True, choices=list(_TARGETS), help="Which orchestrator to use."
    )
    parser.add_argument("question", help="Natural-language question.")
    args = parser.parse_args()

    url = _TARGETS[args.target]()
    body = AskRequest(question=args.question)

    resp = httpx.post(f"{url}/ask", json=body.model_dump(), timeout=settings.llm_timeout_s * 4)
    resp.raise_for_status()

    result = AskResponse.model_validate(resp.json())

    if result.sql:
        print(result.sql)
        print()
    print(result.answer)

    sys.exit(0 if result.error is None else 1)


if __name__ == "__main__":
    main()
