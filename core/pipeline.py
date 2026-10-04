"""What a domain hands the engines: its run state, its steps, and how they connect.

A pipeline is data, not a code path. The scratch orchestrator interprets it in
a hand-written loop; the LangGraph orchestrator compiles it into a StateGraph.
Because both execute the same steps and the same routes, they cannot drift apart
on what a domain does -- only on how execution is managed (checkpointing,
resume, streaming), which is the comparison worth making.

The shape is LangGraph's on purpose, so compiling is a mapping, not a translation:

    steps   -> nodes            edges  -> static edges
    routes  -> conditional edges, with ``targets`` as the path map
    END     -> LangGraph's END, which is the same string

Steps return field updates rather than mutating state. That is what LangGraph
nodes do, and it keeps a step's effect visible in one return statement.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.config import DomainSettings
from core.events import RunContext
from core.models import Evidence, Strict

#: Where a run stops. Identical to LangGraph's END, so routes pass through untranslated.
END = "__end__"


class PipelineError(RuntimeError):
    """A pipeline is malformed, or a run broke one of the engine's rules."""


class RunState(Strict):
    """Fields every run has, whatever the domain.

    A domain subclasses this and adds its own fields. The subclass is the
    scratch loop's state and, unchanged, the LangGraph state schema.
    """

    run_id: str
    question: str
    #: Executions performed so far. Routes compare it with ``max_attempts``.
    attempts: int = 0
    #: The last failure, and the domain's classification of it.
    error: str | None = None
    error_type: str | None = None
    #: Set by the step that answers. A run that ends without one is an error.
    answer: str | None = None


#: A step: read the state, do one thing, return the fields that changed.
type Step[S: RunState] = Callable[[S, RunContext], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class Route[S: RunState]:
    """A conditional edge: ``decide`` picks the next step from ``targets``."""

    decide: Callable[[S], str]
    targets: frozenset[str]


@dataclass(frozen=True)
class Pipeline[S: RunState]:
    state: type[S]
    entry: str
    steps: Mapping[str, Step[S]]
    #: Unconditional successors.
    edges: Mapping[str, str] = field(default_factory=dict)
    #: Conditional successors. A step has an edge or a route, never both.
    routes: Mapping[str, Route[S]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names = set(self.steps)
        if END in names:
            raise PipelineError(f"{END!r} is reserved and cannot name a step")
        if self.entry not in names:
            raise PipelineError(f"entry {self.entry!r} is not a step; steps: {sorted(names)}")

        for name in names:
            has_edge, has_route = name in self.edges, name in self.routes
            if has_edge == has_route:
                raise PipelineError(
                    f"step {name!r} needs exactly one of an edge or a route; "
                    f"it has {'both' if has_edge else 'neither'}"
                )

        for source in set(self.edges) | set(self.routes):
            if source not in names:
                raise PipelineError(f"edge or route from unknown step {source!r}")

        valid = names | {END}
        for source, target in self.edges.items():
            if target not in valid:
                raise PipelineError(f"edge {source!r} -> unknown step {target!r}")
        for source, route in self.routes.items():
            unknown = route.targets - valid
            if unknown:
                raise PipelineError(f"route from {source!r} -> unknown steps {sorted(unknown)}")


def apply_update[S: RunState](state: S, update: Mapping[str, Any]) -> S:
    """A step's update applied: fields overwrite, the result is re-validated.

    A field the state does not have is an error. Both engines apply updates
    through here -- LangGraph on its own silently drops an unknown key, so a
    typo in a step would be lost on one engine and fatal on the other.
    """
    unknown = set(update) - set(type(state).model_fields)
    if unknown:
        raise PipelineError(f"step updated unknown state fields {sorted(unknown)}")
    return type(state).model_validate(state.model_dump() | dict(update))


@dataclass(frozen=True)
class Domain[S: RunState]:
    """One use case, as the engines see it. Each domain exports one as ``DOMAIN``."""

    name: str
    settings: DomainSettings
    pipeline: Pipeline[S]
    #: What the answer rests on, built from the final state for ``AskResponse``.
    evidence: Callable[[S], list[Evidence]]
