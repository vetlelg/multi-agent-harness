"""The hand-built engine: runs any domain's pipeline in a plain loop.

No framework. The loop below is the whole of the execution model, and it holds
the rules both engines share (CLAUDE.md, "Engines"):

- a step returns field updates, which overwrite and are re-validated;
- after a routed step, the route picks the next step and a RouteDecision is
  emitted;
- RunStart comes first and RunEnd last, even when a step raises;
- the run stops at END, or fails after ``max_steps``.
"""

from __future__ import annotations

from core.events import (
    Emitter,
    RouteDecision,
    RunContext,
    RunEnd,
    RunStart,
    emit,
    new_run_id,
    sequenced,
)
from core.models import AskRequest, AskResponse
from core.pipeline import END, Domain, PipelineError, apply_update

ORCHESTRATOR = "scratch"
SERVICE = "orchestrator_scratch"


def run(domain: Domain, request: AskRequest, *, emitter: Emitter = emit) -> AskResponse:
    run_id = request.run_id or new_run_id()
    # Scratch keeps no record of runs, so every call is a new run: segment 1, even
    # for a supplied run_id (CLAUDE.md, "Run identity and resume").
    ctx = RunContext(run_id=run_id, service=SERVICE, emit=sequenced(emitter, segment=1))
    pipeline = domain.pipeline
    state = pipeline.state(run_id=run_id, question=request.question)

    ctx.emit(
        RunStart(
            run_id=run_id,
            service=SERVICE,
            question=request.question,
            domain=domain.name,
            orchestrator=ORCHESTRATOR,
        )
    )

    try:
        step = pipeline.entry
        for _ in range(domain.settings.max_steps):
            state = apply_update(state, pipeline.steps[step](state, ctx))

            route = pipeline.routes.get(step)
            if route is None:
                step = pipeline.edges[step]
            else:
                step = route.decide(state)
                if step not in route.targets:
                    raise PipelineError(
                        f"route returned {step!r}, not one of {sorted(route.targets)}"
                    )
                ctx.emit(
                    RouteDecision(
                        run_id=run_id,
                        service=SERVICE,
                        attempt=state.attempts,
                        error_type=state.error_type,
                        branch=step,
                    )
                )

            if step == END:
                break
        else:
            raise PipelineError(f"run exceeded max_steps={domain.settings.max_steps}")

        if state.answer is None:
            raise PipelineError("pipeline reached END without setting an answer")
    except Exception as exc:
        ctx.emit(
            RunEnd(
                run_id=run_id,
                service=SERVICE,
                attempts=state.attempts,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        raise

    ctx.emit(
        RunEnd(
            run_id=run_id,
            service=SERVICE,
            attempts=state.attempts,
            ok=state.error is None,
            answer=state.answer,
            error=state.error,
        )
    )
    return AskResponse(
        run_id=run_id,
        domain=domain.name,
        orchestrator=ORCHESTRATOR,
        question=state.question,
        answer=state.answer,
        evidence=domain.evidence(state),
        attempts=state.attempts,
        error=state.error,
    )
