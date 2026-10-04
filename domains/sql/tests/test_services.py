"""The sql services start and answer, in-process. The executor needs ``make db``."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from domains.sql.config import settings


def test_agents_report_healthy() -> None:
    from domains.sql.agents.query_agent.app import app as query_app
    from domains.sql.agents.schema_agent.app import app as schema_app

    for app, name in ((schema_app, "schema_agent"), (query_app, "query_agent")):
        response = TestClient(app).get("/healthz")
        assert response.json() == {"status": "ok", "service": name}


needs_db = pytest.mark.skipif(not settings.db_path.exists(), reason="run `make db` first")


@pytest.fixture(scope="module")
def executor():
    from domains.sql.tools.executor.app import app

    return TestClient(app)


@needs_db
def test_executor_runs_a_select(executor, capsys) -> None:
    response = executor.post(
        "/execute", json={"run_id": "r1", "sql": "SELECT COUNT(*) FROM Artist"}
    )
    body = response.json()

    assert body["ok"] is True
    assert body["sql_executed"].endswith("LIMIT 50")
    assert body["rows"][0][0] > 0

    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["type"] == "tool_call"
    assert event["tool"] == "sql.execute"
    assert event["run_id"] == "r1"


@needs_db
def test_executor_guard_rejects_writes(executor, capsys) -> None:
    body = executor.post("/execute", json={"run_id": "r1", "sql": "DROP TABLE Artist"}).json()

    assert body["ok"] is False
    assert body["error_type"] == "guard_rejected"

    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["ok"] is False
    assert event["error_type"] == "guard_rejected"


@needs_db
def test_executor_classifies_missing_table(executor) -> None:
    body = executor.post("/execute", json={"run_id": "r1", "sql": "SELECT * FROM Nope"}).json()
    assert body["error_type"] == "missing_object"


#: About five seconds unbounded, so only the timeout ends it quickly -- and if the
#: timeout is ever lost, the query still finishes and the test fails, not hangs.
_LONG_QUERY = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 30000000) "
    "SELECT COUNT(*) FROM c"
)


@needs_db
def test_executor_times_out_long_queries(executor, monkeypatch) -> None:
    monkeypatch.setattr(settings, "query_timeout_s", 0.3)
    body = executor.post("/execute", json={"run_id": "r1", "sql": _LONG_QUERY}).json()

    assert body["ok"] is False
    assert body["error_type"] == "timeout"


@needs_db
def test_one_request_finishing_does_not_disarm_another() -> None:
    """Each query's timeout belongs to that query.

    With one shared connection the progress handler was shared too: a request
    finishing cleared it while another request's query was still to run, and that
    query ran unbounded. This replays the interleaving in a single thread.
    """
    from domains.sql.tools.executor.app import _query_connection

    with _query_connection(timeout_s=0.3) as slow:
        with _query_connection(timeout_s=0.3) as fast:
            fast.execute("SELECT 1").fetchall()
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            slow.execute(_LONG_QUERY).fetchall()
