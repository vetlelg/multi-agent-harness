from __future__ import annotations

from typing import Any

import pytest

from core.pipeline import END, Pipeline, PipelineError, Route, RunState


def _noop(state, ctx) -> dict[str, Any]:
    return {}


def _pipeline(**changes: Any) -> Pipeline:
    parts: dict[str, Any] = {
        "state": RunState,
        "entry": "a",
        "steps": {"a": _noop, "b": _noop},
        "edges": {"b": END},
        "routes": {"a": Route(decide=lambda s: "b", targets=frozenset({"a", "b"}))},
    }
    parts.update(changes)
    return Pipeline(**parts)


def test_valid_pipeline_builds() -> None:
    assert _pipeline().entry == "a"


def test_end_matches_langgraph() -> None:
    # The LangGraph engine relies on this to pass routes through untranslated.
    assert END == "__end__"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"entry": "zzz"}, "entry"),
        ({"edges": {}}, "neither"),
        ({"edges": {"a": "b", "b": END}}, "both"),
        ({"edges": {"b": "nowhere"}}, "unknown step"),
        ({"edges": {"b": END, "ghost": END}}, "unknown step 'ghost'"),
        (
            {"routes": {"a": Route(decide=lambda s: "b", targets=frozenset({"b", "nowhere"}))}},
            "unknown steps",
        ),
        ({"steps": {"a": _noop, "b": _noop, END: _noop}}, "reserved"),
    ],
    ids=[
        "bad-entry",
        "no-outgoing",
        "edge-and-route",
        "edge-target",
        "edge-source",
        "route-target",
        "reserved-name",
    ],
)
def test_malformed_pipeline_rejected(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(PipelineError, match=message):
        _pipeline(**changes)
