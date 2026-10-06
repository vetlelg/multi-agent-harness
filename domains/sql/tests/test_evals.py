"""The sql eval set itself: every expected result is what its reference query returns.

An eval with a wrong expected result scores the system against a typo. So each
case's ``reference`` is run on the real database, and must give its ``rows``:
the same columns, the same values. Needs ``make db``.
"""

from __future__ import annotations

import sqlite3

import pytest
import yaml

from core.evals import EVAL_SET, EvalCase, EvalSet, domains_with_eval_sets, evals_dir, rows_match
from domains.sql.config import settings

CASES = EvalSet.model_validate(
    yaml.safe_load((evals_dir("sql") / EVAL_SET).read_text(encoding="utf-8"))
).cases

needs_db = pytest.mark.skipif(not settings.db_path.exists(), reason="run `make db` first")


@needs_db
@pytest.mark.parametrize("case", [c for c in CASES if c.rows is not None], ids=lambda c: c.id)
def test_reference_returns_expected_rows(case: EvalCase) -> None:
    assert case.reference, "an answerable case needs the query its rows came from"

    connection = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        cursor = connection.execute(case.reference)
        rows = [list(row) for row in cursor.fetchall()]
        width = len(cursor.description)
    finally:
        connection.close()

    assert width == len(case.rows[0])
    assert rows_match(case.rows, rows, ordered=case.ordered)


def test_the_runner_finds_this_eval_set() -> None:
    assert "sql" in domains_with_eval_sets()
