from __future__ import annotations

import tempfile
from pathlib import Path

from core.errors import ErrorType
from core.events import (
    HttpCall,
    ModelCall,
    RouteDecision,
    RunEnd,
    RunStart,
    SqlExecute,
    emit,
    new_run_id,
    parse_event,
)


def test_emit_and_parse_all_event_types(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runs_dir = Path(tmp)
        monkeypatch.setattr("core.events.settings.runs_dir", runs_dir)

        rid = new_run_id()
        events = [
            RunStart(run_id=rid, service="test", question="Q?", orchestrator="scratch", seq=1),
            HttpCall(
                run_id=rid,
                service="test",
                target="executor",
                method="POST",
                path="/execute",
                status=200,
                elapsed_ms=42,
                seq=2,
            ),
            ModelCall(
                run_id=rid,
                service="test",
                role="sql",
                provider="anthropic",
                model="claude-opus-5",
                prompt="generate sql",
                response="SELECT 1",
                input_tokens=10,
                output_tokens=5,
                stop_reason="finished",
                elapsed_ms=100,
                seq=3,
            ),
            SqlExecute(
                run_id=rid,
                service="test",
                sql="SELECT 1",
                ok=True,
                row_count=1,
                elapsed_ms=3,
                seq=4,
            ),
            RouteDecision(
                run_id=rid,
                service="test",
                attempt=1,
                error_type=ErrorType.SYNTAX,
                branch="generate_sql",
                seq=5,
            ),
            RunEnd(
                run_id=rid,
                service="test",
                attempts=1,
                ok=True,
                answer="One.",
                seq=6,
            ),
        ]

        for event in events:
            emit(event)

        run_file = runs_dir / f"{rid}.jsonl"
        assert run_file.exists()

        lines = run_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == len(events)

        for line, original in zip(lines, events):
            parsed = parse_event(line)
            assert type(parsed) is type(original)
            assert parsed.run_id == rid
            assert parsed.type == original.type
