"""The CLI picks the run_id, says what it is before asking, and sends it."""

from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest

import cli
from tests.fixtures.toy.config import ToySettings


def _ask(monkeypatch, *flags: str) -> tuple[dict[str, Any], int]:
    sent: dict[str, Any] = {}

    def fake_post(url: str, json: dict[str, Any], timeout: float) -> httpx.Response:
        sent.update(json, url=url)
        return httpx.Response(
            200,
            json={
                "run_id": json["run_id"],
                "domain": "toy",
                "orchestrator": "scratch",
                "question": json["question"],
                "answer": "done",
                "evidence": [],
                "attempts": 1,
                "error": None,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(cli, "load_domain_settings", lambda name: ToySettings())
    monkeypatch.setattr(cli.httpx, "post", fake_post)
    monkeypatch.setattr(sys, "argv", ["cli.py", "--domain", "toy", "--target", "scratch", *flags])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    return sent, exit_.value.code


def test_a_new_run_id_is_generated_printed_and_sent(monkeypatch, capsys) -> None:
    sent, code = _ask(monkeypatch, "go?")

    assert code == 0
    assert sent["url"] == "http://toy-scratch/ask"
    assert sent["run_id"]
    assert f"run_id: {sent['run_id']}" in capsys.readouterr().err


def test_a_supplied_run_id_is_sent_unchanged(monkeypatch, capsys) -> None:
    sent, _ = _ask(monkeypatch, "--run-id", "run-42", "go?")

    assert sent["run_id"] == "run-42"
    assert "run_id: run-42" in capsys.readouterr().err
