"""The eval runner: every domain's eval set, asked through ``/ask``, scored, reported.

Marked ``live``: it needs a running orchestrator and a real model, and its
outcome varies from run to run. Run with ``pytest -m live tests/test_evals_live.py``,
and ``--eval-repeats N`` to change how often each question is asked.

A target with nothing listening is skipped, so the runner measures whatever is
deployed. The pass rate is printed next to the baseline's, never asserted: with
a handful of questions it moves by many points between identical runs. The test
fails only when the run itself breaks.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import pytest
import yaml

from core.config import settings
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
    score,
)
from core.events import new_run_id
from core.models import AskRequest, AskResponse, QueryEvidence
from core.registry import load_domain_settings

pytestmark = pytest.mark.live

TARGETS = ("scratch", "lg")

# The CLI's budget: a run is several model calls, each with its own timeout.
_ASK_TIMEOUT_S = settings.llm_timeout_s * 4


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _trial(url: str, domain: str, case: EvalCase) -> Trial:
    # Chosen here and sent, like the CLI does, so even a failed trial can be
    # found in the log.
    run_id = new_run_id()
    body = AskRequest(question=case.question, run_id=run_id)
    start = time.perf_counter()
    try:
        response = httpx.post(f"{url}/ask", json=body.model_dump(), timeout=_ASK_TIMEOUT_S)
    except httpx.TimeoutException:
        return Trial(
            run_id=run_id,
            passed=False,
            reason=f"no response within {_ASK_TIMEOUT_S:.0f} s",
            elapsed_ms=_elapsed_ms(start),
        )
    elapsed_ms = _elapsed_ms(start)

    # The service broke on this question: a user would get nothing, so it counts.
    if response.status_code >= 500:
        return Trial(
            run_id=run_id,
            passed=False,
            reason=f"HTTP {response.status_code}",
            elapsed_ms=elapsed_ms,
        )
    response.raise_for_status()

    result = AskResponse.model_validate(response.json())
    if result.domain != domain:
        pytest.fail(f"{url} answers for domain {result.domain!r}, not {domain!r}")
    verdict = score(case, result)
    query = next((e.query for e in result.evidence if isinstance(e, QueryEvidence)), None)
    return Trial(
        run_id=run_id,
        passed=verdict.passed,
        reason=verdict.reason,
        attempts=result.attempts,
        query=query,
        answer=result.answer,
        elapsed_ms=elapsed_ms,
    )


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("domain", domains_with_eval_sets())
def test_eval(domain: str, target: str, eval_repeats: int, capsys) -> None:
    url = getattr(load_domain_settings(domain), f"{target}_url")
    try:
        httpx.get(f"{url}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"no {target} orchestrator at {url}")

    directory = evals_dir(domain)
    eval_set = EvalSet.model_validate(
        yaml.safe_load((directory / EVAL_SET).read_text(encoding="utf-8"))
    )
    baseline_path = directory / BASELINE
    baseline = (
        EvalReport.model_validate_json(baseline_path.read_text(encoding="utf-8"))
        if baseline_path.is_file()
        else None
    )

    started = datetime.now(UTC)
    with capsys.disabled():
        print()  # off pytest's progress line
    results = []
    for case in eval_set.cases:
        trials = []
        for n in range(1, eval_repeats + 1):
            trial = _trial(url, domain, case)
            trials.append(trial)
            with capsys.disabled():
                outcome = "pass" if trial.passed else "FAIL"
                print(f"  {case.id} [{n}/{eval_repeats}] {outcome} {trial.elapsed_ms / 1000:.0f}s")
        results.append(CaseResult(id=case.id, question=case.question, trials=trials))

    report = EvalReport(
        domain=domain,
        target=target,
        repeats=eval_repeats,
        started_at=started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        cases=results,
    )

    with capsys.disabled():
        print("\n" + render(report, baseline))
        if settings.runs_dir is not None:
            path = settings.runs_dir / "evals" / f"{domain}-{target}-{started:%Y%m%dT%H%M%SZ}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
            print(f"report: {path}")
