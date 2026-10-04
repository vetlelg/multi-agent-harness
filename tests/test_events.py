from __future__ import annotations

import tempfile
from pathlib import Path

from core.events import (
    Event,
    HttpCall,
    ModelCall,
    RouteDecision,
    RunEnd,
    RunStart,
    ToolCall,
    emit,
    new_run_id,
    parse_event,
    sequenced,
)


def test_emit_and_parse_all_event_types(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runs_dir = Path(tmp)
        monkeypatch.setattr("core.events.settings.runs_dir", runs_dir)

        rid = new_run_id()
        events = [
            RunStart(
                run_id=rid,
                service="test",
                question="Q?",
                domain="sql",
                orchestrator="scratch",
                seq=1,
            ),
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
                role="query",
                provider="anthropic",
                model="claude-opus-5",
                prompt_id="domains/sql/prompts/generate_sql.txt",
                prompt_sha="0123456789ab",
                system="You write SQL.",
                prompt="generate sql",
                response="SELECT 1",
                params={"max_tokens": 16000},
                input_tokens=10,
                output_tokens=5,
                stop_reason="finished",
                elapsed_ms=100,
                seq=3,
            ),
            ToolCall(
                run_id=rid,
                service="test",
                tool="sql.execute",
                input={"sql": "SELECT 1"},
                ok=True,
                output={"columns": ["1"], "rows": [[1]], "row_count": 1},
                elapsed_ms=3,
                seq=4,
            ),
            RouteDecision(
                run_id=rid,
                service="test",
                attempt=1,
                error_type="syntax",
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
            assert parsed == original


def test_sequenced_numbers_from_one_without_mutating() -> None:
    seen: list[Event] = []
    emit_seq = sequenced(seen.append)
    originals = [
        RunEnd(run_id="r", service="s", attempts=0, ok=True),
        RunEnd(run_id="r", service="s", attempts=1, ok=True),
    ]
    for event in originals:
        emit_seq(event)

    assert [e.seq for e in seen] == [1, 2]
    assert all(e.seq is None for e in originals)


def test_each_sequence_is_independent() -> None:
    first: list[Event] = []
    second: list[Event] = []
    sequenced(first.append)(RunEnd(run_id="a", service="s", attempts=0, ok=True))
    sequenced(second.append)(RunEnd(run_id="b", service="s", attempts=0, ok=True))
    assert first[0].seq == second[0].seq == 1
