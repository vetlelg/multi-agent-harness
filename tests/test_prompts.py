from __future__ import annotations

from pathlib import Path

from core.prompts import load_prompt

_ANSWER = Path(__file__).resolve().parents[1] / "domains" / "sql" / "prompts" / "answer.txt"


def test_id_is_repo_relative() -> None:
    assert load_prompt(_ANSWER).id == "domains/sql/prompts/answer.txt"


def test_sha_tracks_content(tmp_path) -> None:
    path = tmp_path / "p.txt"
    path.write_text("Answer briefly. – ünïcode", encoding="utf-8")
    first = load_prompt(path)
    assert first.text == "Answer briefly. – ünïcode"
    assert len(first.sha) == 12
    assert load_prompt(path).sha == first.sha

    path.write_text("Answer at length.", encoding="utf-8")
    assert load_prompt(path).sha != first.sha


def test_id_outside_repo_falls_back_to_name(tmp_path) -> None:
    path = tmp_path / "p.txt"
    path.write_text("x", encoding="utf-8")
    assert load_prompt(path).id == "p.txt"
