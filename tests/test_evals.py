"""The eval scorer, its case format and its report -- and every domain's eval set.

The scorer is the ruler every eval result is measured with. If it is wrong, no
eval run can show it, so each decision about what counts as the same result is
pinned here.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from core.evals import (
    BASELINE,
    EVAL_SET,
    CaseResult,
    EvalCase,
    EvalReport,
    EvalSet,
    Trial,
    domains_with_eval_sets,
    evals_dir,
    render,
    rows_match,
    score,
)
from core.models import AskResponse, CitationEvidence, QueryEvidence

# --------------------------------------------------------------------------- #
# Matching rows                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("expected", "actual", "ordered", "result"),
    [
        pytest.param([[5]], [[5]], False, True, id="same"),
        pytest.param([["USA"]], [["USA", 13]], False, True, id="extra-column"),
        pytest.param([["Rock", 1297]], [[1297, "Rock"]], False, True, id="column-order"),
        pytest.param([["Rock", 1297]], [[1, 1297]], False, False, id="id-for-name"),
        pytest.param([["Rock", 1297]], [["Rock"]], False, False, id="missing-column"),
        pytest.param([["a"], ["b"]], [["b"], ["a"]], False, True, id="row-order"),
        pytest.param([["a"], ["b"]], [["b"], ["a"]], True, False, id="row-order-ordered"),
        pytest.param([["a"], ["b"]], [["a"], ["b"], ["c"]], False, False, id="extra-row"),
        pytest.param([[1], [1], [2]], [[1], [2], [2]], False, False, id="multiset"),
        pytest.param([[105.93]], [[105.92999999999999]], False, True, id="float-noise"),
        pytest.param([[5]], [[5.0]], False, True, id="int-and-float"),
        pytest.param([[469.58]], [[469.6]], False, False, id="rounded-too-far"),
        pytest.param([["USA"]], [["usa"]], False, False, id="strings-exact"),
        pytest.param([], [], False, True, id="both-empty"),
        pytest.param([], [[1]], False, False, id="expected-empty"),
    ],
)
def test_rows_match(expected, actual, ordered, result) -> None:
    assert rows_match(expected, actual, ordered=ordered) is result


# --------------------------------------------------------------------------- #
# The case format                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"id": "c", "question": "q"}, id="neither"),
        pytest.param({"id": "c", "question": "q", "rows": [[1]], "unanswerable": True}, id="both"),
        pytest.param(
            {"id": "c", "question": "q", "unanswerable": True, "ordered": True},
            id="ordered-unanswerable",
        ),
        pytest.param({"id": "c", "question": "q", "rows": [[1], [1, 2]]}, id="ragged"),
        pytest.param({"id": "c", "question": "q", "rows": [[]]}, id="empty-row"),
        pytest.param({"id": "Brazil customers", "question": "q", "rows": [[1]]}, id="bad-id"),
    ],
)
def test_invalid_cases_are_rejected(case) -> None:
    with pytest.raises(ValidationError):
        EvalCase.model_validate(case)


def test_case_ids_are_unique() -> None:
    case = {"id": "c", "question": "q", "rows": [[1]]}
    with pytest.raises(ValidationError, match="duplicate case id 'c'"):
        EvalSet.model_validate({"cases": [case, case]})


def test_an_eval_set_has_cases() -> None:
    with pytest.raises(ValidationError):
        EvalSet.model_validate({"cases": []})


# --------------------------------------------------------------------------- #
# Scoring                                                                       #
# --------------------------------------------------------------------------- #

_COUNT = EvalCase(id="count", question="How many?", rows=[[5]])
_NOPE = EvalCase(id="nope", question="How old?", unanswerable=True)


def _response(*evidence, error: str | None = None) -> AskResponse:
    return AskResponse(
        run_id="r",
        domain="d",
        orchestrator="scratch",
        question="q",
        answer="a",
        evidence=list(evidence),
        attempts=1,
        error=error,
    )


def _query(columns: list[str], rows: list[list] | None) -> QueryEvidence:
    return QueryEvidence(language="sql", query="SELECT ...", columns=columns, rows=rows)


def test_unanswerable_passes_when_the_run_declines() -> None:
    verdict = score(_NOPE, _response(error="no such column: Age"))
    assert verdict.passed
    assert "no such column: Age" in verdict.reason


def test_unanswerable_fails_when_the_run_answers() -> None:
    verdict = score(_NOPE, _response(_query(["avg"], [[None]])))
    assert not verdict.passed
    assert "cannot answer" in verdict.reason


def test_an_error_fails_an_answerable_case() -> None:
    verdict = score(_COUNT, _response(_query(["n"], None), error='near "SELEC"'))
    assert not verdict.passed
    assert 'near "SELEC"' in verdict.reason


def test_no_query_result_fails() -> None:
    citation = CitationEvidence(source="s", excerpt="e")
    verdict = score(_COUNT, _response(citation, _query(["n"], None)))
    assert not verdict.passed
    assert "no query result" in verdict.reason


def test_a_matching_result_passes() -> None:
    verdict = score(_COUNT, _response(_query(["n"], [[3]]), _query(["COUNT(*)"], [[5]])))
    assert verdict.passed


def test_a_different_result_fails_and_says_what_came_back() -> None:
    verdict = score(_COUNT, _response(_query(["GenreId", "n"], [[1, 5], [2, 7]])))
    assert not verdict.passed
    assert "2 row(s)" in verdict.reason
    assert "GenreId" in verdict.reason


# --------------------------------------------------------------------------- #
# The report                                                                    #
# --------------------------------------------------------------------------- #


def _trial(passed: bool, run_id: str = "r") -> Trial:
    return Trial(run_id=run_id, passed=passed, reason="ok" if passed else "nope", elapsed_ms=1)


def _report(*cases: CaseResult) -> EvalReport:
    return EvalReport(
        domain="d", target="scratch", repeats=3, started_at="2026-10-05T12:00:00Z", cases=cases
    )


def test_render_compares_with_the_baseline() -> None:
    report = _report(
        CaseResult(id="always", question="q", trials=[_trial(True)] * 3),
        CaseResult(
            id="sometimes",
            question="q",
            trials=[_trial(True), _trial(False, run_id="run-9"), _trial(True)],
        ),
    )
    baseline = _report(
        CaseResult(id="always", question="q", trials=[_trial(True), _trial(False)]),
    )

    text = render(report, baseline)

    assert report.passes == 5
    assert report.rate == pytest.approx(5 / 6)
    assert "always" in text
    assert "sometimes" in text
    assert "3/3" in text
    assert "2/3" in text
    assert "67%" in text
    assert "baseline" in text
    assert "50%" in text
    # Header, column names, then one line per case: "sometimes" is new since the baseline.
    assert text.splitlines()[3].endswith("-")
    assert "5/6" in text.splitlines()[4]
    assert "run-9  nope" in text


def test_render_without_a_baseline_has_no_baseline_column() -> None:
    report = _report(CaseResult(id="always", question="q", trials=[_trial(True)]))

    text = render(report)

    assert "baseline" not in text
    assert "failures" not in text


# --------------------------------------------------------------------------- #
# Every domain's eval set                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("domain", domains_with_eval_sets())
def test_every_domain_eval_set_is_valid(domain: str) -> None:
    directory = evals_dir(domain)
    EvalSet.model_validate(yaml.safe_load((directory / EVAL_SET).read_text(encoding="utf-8")))

    # A baseline the runner can no longer read is a broken baseline.
    baseline = directory / BASELINE
    if baseline.is_file():
        EvalReport.model_validate_json(baseline.read_text(encoding="utf-8"))
