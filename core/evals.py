"""Evaluating a domain: its eval set, how a response is scored, and the report.

An eval set is a domain's questions, each with the result its answer must rest
on. Scoring reads the evidence ``/ask`` returns -- never the answer's wording --
so it is mechanical, identical for every domain and both engines, and needs no
model of its own.

Evals score a pass rate, never one assertion per question: model output varies
from run to run, so each question is asked several times and the rate is the
measurement.

No YAML and no network here. The live runner (``tests/test_evals_live.py``)
reads the files, calls ``/ask`` and writes the report.
"""

from __future__ import annotations

import importlib
import itertools
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from core.models import AskResponse, QueryEvidence, Row, Strict
from core.registry import DOMAINS_PACKAGE, available_domains

#: In every domain's ``evals/`` directory: the cases, and the recorded report
#: new runs are compared with.
EVAL_SET = "questions.yaml"
BASELINE = "baseline.json"

#: Numbers are compared at this many decimal places, so float noise and a
#: model's ``ROUND(x, 2)`` don't decide a case.
DECIMALS = 2


def evals_dir(domain: str) -> Path:
    """``domains/<name>/evals/``: where a domain keeps its eval set and baseline."""
    package = importlib.import_module(f"{DOMAINS_PACKAGE}.{domain}")
    return Path(package.__path__[0]) / "evals"


def domains_with_eval_sets() -> list[str]:
    return [name for name in available_domains() if (evals_dir(name) / EVAL_SET).is_file()]


# --------------------------------------------------------------------------- #
# The eval set                                                                  #
# --------------------------------------------------------------------------- #


class EvalCase(Strict):
    """One question, and what its answer must rest on."""

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    question: str
    #: How ``rows`` was derived, in the domain's query language. The harness
    #: never runs it; a domain test can, so expected rows can't drift from the data.
    reference: str | None = None
    #: The result the answer must rest on, matched against ``query`` evidence.
    rows: list[Row] | None = None
    #: Row order is part of the answer (a ranking).
    ordered: bool = False
    #: The data cannot answer: the run must end with ``error`` set.
    unanswerable: bool = False

    @model_validator(mode="after")
    def _one_expectation(self) -> EvalCase:
        if self.unanswerable == (self.rows is not None):
            raise ValueError(f"case {self.id!r}: give either rows or unanswerable: true")
        if self.ordered and self.rows is None:
            raise ValueError(f"case {self.id!r}: ordered applies only to rows")
        widths = {len(row) for row in self.rows or []}
        if len(widths) > 1 or 0 in widths:
            raise ValueError(f"case {self.id!r}: rows must all have the same, non-zero length")
        return self


class EvalSet(Strict):
    """The body of ``questions.yaml``."""

    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> EvalSet:
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"duplicate case id {case.id!r}")
            seen.add(case.id)
        return self


# --------------------------------------------------------------------------- #
# Scoring                                                                       #
# --------------------------------------------------------------------------- #


def _value(value: Any) -> Any:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return round(float(value), DECIMALS)
    return value


def _canonical(rows: list[Row], ordered: bool) -> list[tuple[Any, ...]] | Counter:
    normalised = [tuple(_value(v) for v in row) for row in rows]
    return normalised if ordered else Counter(normalised)


def rows_match(expected: list[Row], actual: list[Row], *, ordered: bool) -> bool:
    """Whether ``actual`` holds ``expected``.

    Column names, column order and extra columns in ``actual`` don't matter:
    ``SELECT Country, COUNT(*)`` answers "which country" as well as
    ``SELECT Country``. Rows do: the same number, the same values, in the same
    order when ``ordered`` and as a multiset otherwise.
    """
    if len(actual) != len(expected):
        return False
    if not expected:
        return True
    want = _canonical(expected, ordered)
    for columns in itertools.permutations(range(len(actual[0])), len(expected[0])):
        projected = [[row[i] for i in columns] for row in actual]
        if _canonical(projected, ordered) == want:
            return True
    return False


class Verdict(Strict):
    passed: bool
    reason: str


def score(case: EvalCase, response: AskResponse) -> Verdict:
    """Score one ``/ask`` response against its case. The answer's wording is never read."""
    if case.unanswerable:
        if response.error is not None:
            return Verdict(passed=True, reason=f"declined: {response.error}")
        return Verdict(passed=False, reason="answered, but the data cannot answer this")
    if response.error is not None:
        return Verdict(passed=False, reason=f"error: {response.error}")

    results = [e for e in response.evidence if isinstance(e, QueryEvidence) and e.rows is not None]
    if not results:
        return Verdict(passed=False, reason="no query result in the evidence")
    if any(rows_match(case.rows, e.rows, ordered=case.ordered) for e in results):
        return Verdict(passed=True, reason="result matches")
    last = results[-1]
    return Verdict(
        passed=False,
        reason=(
            f"result differs: got {len(last.rows)} row(s) of {last.columns}, "
            f"expected {len(case.rows)} row(s) of {len(case.rows[0]) if case.rows else 0} "
            "column(s)"
        ),
    )


# --------------------------------------------------------------------------- #
# The report                                                                    #
# --------------------------------------------------------------------------- #
# Rates are properties, not fields: the file stores what happened, and a rate
# computed on read can't disagree with the trials it came from.


class Trial(Strict):
    """One asking of one case."""

    #: Finds the run in the log.
    run_id: str
    passed: bool
    #: The verdict's reason, or why there was no response.
    reason: str
    #: None when there was no response.
    attempts: int | None = None
    #: What the answer rested on, so a failure can be read without the log.
    query: str | None = None
    answer: str | None = None
    elapsed_ms: int


class CaseResult(Strict):
    id: str
    question: str
    trials: list[Trial] = Field(min_length=1)

    @property
    def passes(self) -> int:
        return sum(trial.passed for trial in self.trials)

    @property
    def rate(self) -> float:
        return self.passes / len(self.trials)


class EvalReport(Strict):
    domain: str
    #: Which orchestrator answered: scratch or lg.
    target: str
    repeats: int = Field(ge=1)
    #: UTC, ISO 8601 to the second.
    started_at: str
    cases: list[CaseResult]

    @property
    def passes(self) -> int:
        return sum(case.passes for case in self.cases)

    @property
    def total(self) -> int:
        return sum(len(case.trials) for case in self.cases)

    @property
    def rate(self) -> float:
        return self.passes / self.total


def render(report: EvalReport, baseline: EvalReport | None = None) -> str:
    """The report as a table for the terminal, with the baseline's rates beside it."""
    before = {case.id: case.rate for case in baseline.cases} if baseline else {}
    width = max(len("overall"), *(len(case.id) for case in report.cases))

    def line(name: str, passes: int, total: int, rate: float, base: float | None) -> str:
        text = f"  {name:<{width}}  {f'{passes}/{total}':>7}  {rate:>5.0%}"
        if baseline is not None:
            text += f"  {'-' if base is None else format(base, '.0%'):>8}"
        return text

    header = f"{report.domain} on {report.target}: {report.repeats} trial(s) per case"
    if baseline is not None:
        header += f"; baseline from {baseline.started_at[:10]} on {baseline.target}"
    columns = f"  {'case':<{width}}  {'passed':>7}  {'rate':>5}"
    if baseline is not None:
        columns += f"  {'baseline':>8}"

    lines = [header, columns]
    for case in report.cases:
        lines.append(line(case.id, case.passes, len(case.trials), case.rate, before.get(case.id)))
    lines.append(
        line(
            "overall",
            report.passes,
            report.total,
            report.rate,
            baseline.rate if baseline else None,
        )
    )

    failures = [
        f"  {case.id:<{width}}  {trial.run_id}  {trial.reason}"
        for case in report.cases
        for trial in case.trials
        if not trial.passed
    ]
    if failures:
        lines += ["failures:", *failures]
    return "\n".join(lines)
