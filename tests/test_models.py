from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.models import (
    AgentRequest,
    AskRequest,
    AskResponse,
    CitationEvidence,
    QueryEvidence,
    ToolEvidence,
)

_EVIDENCE = [
    QueryEvidence(language="sql", query="SELECT 1", columns=["1"], rows=[[1]]),
    CitationEvidence(source="docs/guide.md", locator="L10-20", excerpt="Restart the pod."),
    ToolEvidence(tool="ops.get_pods", input={"namespace": "default"}, output="3 pods", ok=True),
]

_ROUND_TRIP_CASES = [
    AskRequest(question="How many?"),
    AskRequest(question="How many?", run_id="r1"),
    AskResponse(
        run_id="r1",
        domain="sql",
        orchestrator="scratch",
        question="How many?",
        answer="One.",
        evidence=_EVIDENCE,
        attempts=1,
        error=None,
    ),
    AgentRequest(run_id="r1"),
    *_EVIDENCE,
]


@pytest.mark.parametrize("model", _ROUND_TRIP_CASES, ids=lambda m: type(m).__name__)
def test_round_trip(model) -> None:
    cls = type(model)
    rebuilt = cls.model_validate_json(model.model_dump_json())
    assert rebuilt == model


def test_evidence_kind_selects_the_type() -> None:
    response = AskResponse.model_validate(
        {
            "run_id": "r1",
            "domain": "docs",
            "orchestrator": "lg",
            "question": "q",
            "answer": "a",
            "evidence": [{"kind": "citation", "source": "x.md", "excerpt": "y"}],
            "attempts": 1,
            "error": None,
        }
    )
    assert isinstance(response.evidence[0], CitationEvidence)


def test_unknown_evidence_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryEvidence.model_validate({"kind": "nope", "language": "sql", "query": "x"})


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        AskRequest(question="ok", bogus_field="should fail")


def test_agent_request_requires_run_id() -> None:
    with pytest.raises(ValidationError):
        AgentRequest()
