"""Command-line options for the live runs."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--eval-repeats",
        type=int,
        default=3,
        help="How many times the live eval runner asks each question (default: 3).",
    )


@pytest.fixture
def eval_repeats(request: pytest.FixtureRequest) -> int:
    return request.config.getoption("--eval-repeats")
