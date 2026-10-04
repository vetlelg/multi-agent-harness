# Multi-agent harness

## Purpose

This repo is a **multi-agent harness**: reusable infrastructure for building, deploying,
and observing multi-agent LLM systems. Each use case is a **domain** — a self-contained
package under `domains/<name>/`. The SQL agent (`domains/sql/`, Chinook) is the first
domain, not the identity of the project. Planned next: a documentation agent and a
troubleshooting/ops agent.

The harness supplies everything that transfers between use cases: two competing
orchestration engines (hand-built and LangGraph), the provider-agnostic LLM wrapper with
structured output and tool use, the typed event log, the `/ask` contract, retry routing,
Docker/k8s infra, OTel + Grafana observability, and evaluation tooling.

A domain supplies only what is specific to it: agents, tool servers, prompts, models,
guard logic, the pipeline (steps + routing), evals, and its deploy overlay. **Adding a
domain means writing domain code, never editing the harness.**

## Vocabulary

- **Harness** — `core/`, `orchestrator_scratch/`, `orchestrator_lg/`, `cli.py`, `infra/base/`.
  Knows nothing about any domain.
- **Domain** — `domains/<name>/`: one use case, everything it needs, nothing shared with
  other domains.
- **Agent** — a service whose job is a model call (e.g. `schema_agent`). Stateless.
- **Tool server** — a deterministic service with data access or side effects and no LLM
  (e.g. the SQL `executor`). It enforces its guard in code.
- **Pipeline** — a domain's steps, static edges, and conditional routes, declared as data
  (`core.pipeline.Pipeline`). Both engines run the same pipeline.
- **Engine / orchestrator** — runs a pipeline. One process serves one domain, selected by
  the `DOMAIN` setting.
- **Evidence** — typed provenance attached to every answer: the SQL that ran, the passage
  cited, the commands executed.

## Hard rules

- **Dependency direction** (enforced by `tests/test_architecture.py`):
  - `core/` imports nothing of ours.
  - `domains/<name>/` imports only `core/` and itself. Domains never import each other.
  - Orchestrators import only `core/`. They load their domain at runtime by name via
    `core.registry` — never `import domains.<name>`. They never import each other.
- Never fix or tune something for one orchestrator only. Domain behaviour lives in the
  pipeline (steps and routes), which both engines execute — so they agree by construction.
- `orchestrator_scratch/`: no LangGraph, LangChain, CrewAI, or similar. The loop is
  written by hand.
- `orchestrator_lg/`: LangGraph lives here and nowhere else. Check the current LangGraph
  documentation at build time for APIs and package names — do not assume them from
  memory; pin whatever you install. All model calls go through `core.llm` — no LangChain
  model wrappers.
- Provider SDKs (`anthropic`, `ollama`, …) are imported only in `core/llm.py`.
- `os.getenv` / `os.environ` appear nowhere. Settings come from pydantic-settings:
  `core/config.py` for harness values, `domains/<name>/config.py` for domain values
  (env prefix `<NAME>_`, so domains cannot collide).
- Cross-cutting behaviour goes through the harness so every domain gets it for free:
  services are built with `core.service.create_app`, model calls go through
  `core.llm.call_model` or `core.agent_loop.run_tool_loop`, and inter-service calls go
  through `core.http`. That is where events (and later OTel spans) are emitted.
- Domains do not add event types. Tool activity is a `ToolCall` event with a namespaced
  tool name (`sql.execute`), so the log schema and dashboards are identical across domains.
- Guards live in tool servers, in code. Never trust the model.
- Every domain and both engines expose the identical `POST /ask` contract.
- NEVER implement code automatically. Always let me (the human) implement it unless I
  explicitly tell you to implement something for me.

## Harness modules (`core/`)

| Module | Responsibility |
|---|---|
| `config.py` | `Settings` (harness), `DomainSettings` base, `ModelSpec` (`provider:model`) |
| `models.py` | `Strict` base, `AgentRequest` (carries `run_id`), `/ask` contract, evidence kinds |
| `events.py` | Typed event log, `emit`, `RunContext`, `sequenced` emitter |
| `llm.py` | Provider adapters: structured output (`complete`), tool-use turns (`chat`); `call_model` emits `ModelCall` |
| `agent_loop.py` | Hand-built tool loop for agentic domains: `Tool`, `ToolFailed`, `run_tool_loop` |
| `pipeline.py` | `RunState` base, `Pipeline`, `Route`, `Domain`, `END`, `apply_update` |
| `registry.py` | `load_domain`, `load_domain_settings`, `available_domains` |
| `prompts.py` | `load_prompt` → `Prompt(id, text, sha)`; the id + sha land in every `ModelCall` |
| `http.py` | Instrumented service calls (`get` / `post`) that emit `HttpCall` |
| `service.py` | `create_app(service)`: FastAPI app with `/healthz`; the single hook for instrumentation |

## The domain contract

A domain package contains:

```
domains/<name>/
  __init__.py      # empty: importing an agent must not pull in the pipeline
  config.py        # <Name>Settings(DomainSettings), env_prefix "<NAME>_"; `settings`
  models.py        # its request/response contracts, model output schemas, its RunState subclass
  domain.py        # steps, routes, evidence(); exports DOMAIN = Domain(...)
  prompts/         # its prompt files
  agents/<agent>/  # LLM services (FastAPI via create_app)
  tools/<tool>/    # deterministic tool servers + guards
  tests/           # its unit tests (collected by the root pytest run)
  evals/           # questions + expected answers, promptfoo config
infra/overlays/<name>/  # its deployment
```

**Adding a domain — acceptance criterion:** outside `domains/<name>/` and
`infra/overlays/<name>/`, the only file that changes is `.env.example`. If anything else
had to change, the harness is missing an abstraction — fix the harness, not the domain.

## Two orchestration patterns

- **Workflow** — fixed steps with conditional routes (the SQL domain: schema → query →
  execute, routed retries).
- **Agentic** — a step calls `core.agent_loop.run_tool_loop`: the model chooses tool calls
  until it answers (the planned ops domain). Tool failures the model should see raise
  `ToolFailed` and go back to it as error results; anything else is a bug and fails the
  run. Checkpoint granularity is the step: if per-turn checkpoints are needed, split model
  and tool turns into separate steps with a route between them.

## Engines

Shared semantics (both engines must hold these; tests in `tests/test_scratch_engine.py`
pin them for scratch):

- A step is `(state, ctx) -> dict` of field updates, applied with
  `core.pipeline.apply_update`: updates overwrite fields and are re-validated; an unknown
  field name is an error. (LangGraph alone silently drops unknown keys — the LangGraph
  engine must wrap every node with `apply_update`.)
- After a step with a route, the route decides the next step and a `RouteDecision` event
  is emitted. A route returning a target it did not declare is an error.
- `RunStart` first, `RunEnd` last — `RunEnd` is emitted even when a step raises. The
  orchestrator numbers its own events (`seq`) and owns the ordered log of record.
- `max_steps` (domain settings) bounds the run; exceeding it is an error.
- The pipeline must end with `state.answer` set.

**`orchestrator_scratch/`** — `engine.py` is the hand-written loop over any `Pipeline`;
`app.py` wires it to FastAPI for the domain named by `DOMAIN`.

**`orchestrator_lg/`** (not built yet) — compiles any `Pipeline` into a LangGraph
`StateGraph`: steps → nodes, `edges` → static edges, `routes` → conditional edges with
`targets` as the path map, `END` is already LangGraph's `"__end__"`, the domain's
`RunState` subclass is the state schema, `max_steps` → recursion limit, `run_id` is the
`thread_id`. Persistent checkpointer: a SQLite file in dev; in-cluster that file lives on
a PersistentVolumeClaim so resume survives pod restarts. (Postgres later.)

## `/ask` contract

`POST /ask {question, run_id?}` →
`{run_id, domain, orchestrator, question, answer, evidence[], attempts, error}`.

Evidence kinds: `query` (language, query, columns, rows — SQL, PromQL, …), `citation`
(source, locator, excerpt), `tool` (tool, input, output, ok). `error` set with HTTP 200
means "could not answer"; a 5xx means the service is broken. Supplying `run_id` resumes a
LangGraph run (it is the `thread_id`).

## Behaviour requirements (every domain, both engines)

- The `run_id` is on every inter-service call (`AgentRequest`) and in every event.
- Each service emits one JSON event per model call, tool call, and HTTP call to stdout
  (visible via `kubectl logs`). `ModelCall` carries prompt id + sha, system prompt, user
  prompt, response, params, tokens, timing. In dev the orchestrator also appends the whole
  run to `runs/<run_id>.jsonl`. Raw and append-only.
- An answer is always shown with its evidence.
- If the data cannot answer the question, say so — never invent data.
- CLI: `python cli.py --domain sql --target scratch|lg "How many customers are from Brazil?"`
  (`--domain` defaults to `DOMAIN`; the URL comes from the domain's settings).

## Domain: `sql` (first instance)

Answers natural-language questions about the Chinook sample database (SQLite, read-only:
artists, albums, tracks, customers, invoices) by generating and executing SQL. `make db`
downloads it. The file is baked into the executor image (deliberate: small, read-only
sample data — no seed jobs, no dialect drift between dev and cluster).

- **schema_agent** (agent) — fetches the full schema from the executor's `/schema`, then
  the model selects the relevant tables and columns. Types and foreign keys are looked up
  from the real schema, never re-emitted by the model.
- **query_agent** (agent) — question + selected schema → one SQL statement. On retry also
  receives the previous SQL and the error.
- **executor** (tool server) — the only service with database access. No LLM.
  `GET /schema` (introspection) and `POST /execute` (guard → run → rows or classified
  error).
- **Pipeline** — `get_schema → generate_sql → execute → (route) → answer`. Route after
  `execute`: success → `answer`; `missing_object` ("no such table/column") with attempts
  left → `get_schema`; other retryable error with attempts left → `generate_sql`;
  exhausted or non-retryable → `answer` (failure explanation). `SQL_MAX_ATTEMPTS`
  executions per run (default 3).
- **Guard** — one read-only statement: `SELECT` or `WITH … SELECT`; reject
  INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/PRAGMA; SQLite authorizer as the real boundary;
  append `LIMIT 50` if missing; 5-second timeout.

## Planned domains

- **docs** — retrieve → answer with citations; on a miss, reformulate and retrieve again.
  Needs a retrieval tool server (embeddings + vector store; pgvector when Postgres lands).
- **ops** — agentic tool loop over read-only cluster tools (`kubectl get/describe/logs`,
  PromQL, LogQL) against the harness's own Grafana stack. Guard: verb + namespace
  allowlist. Mutations would need human approval — deferred.

## Stack

- Python 3.12, venv + pip, pinned requirements (`requirements.txt` for agents and
  orchestrators, `requirements-lg.txt` adds LangGraph, `requirements-tool.txt` is the
  minimal set for tool servers, `requirements-dev.txt` for tests)
- LLM: provider-agnostic `core/llm.py`. Anthropic and Ollama adapters implemented, for
  both structured output and tool use; OpenAI/Gemini are a class each when wanted. Model
  per role as `provider:model` in the domain's settings. Current Claude models reject
  sampling parameters, so the Anthropic adapter drops `temperature`; reproducibility comes
  from the run log, not from sampling.
- FastAPI per service via `core.service.create_app`; every service exposes `GET /healthz`
- httpx for inter-service calls (`core.http`)
- `langgraph` (in `orchestrator_lg/` only)
- Docker, k3d or minikube, kustomize: `infra/base/` (harness) + `infra/overlays/<domain>/`.
  One image per engine, containing `core/` and `domains/`; `DOMAIN` picks the domain at
  startup. Agent and tool-server images carry `core/` plus their own domain only.
- API keys: `.env` locally (gitignored); a Kubernetes Secret in-cluster — never in images
  or manifests
- `pytest`, `ruff`
- `structlog` for structured logging with context propagation (run_id bound once) —
  replaces the raw `print(json)` in `core/events.py`
- OpenTelemetry (`opentelemetry-instrumentation-fastapi`, `-httpx`), added once in
  `create_app` / `core.http`; model-call spans use the GenAI semantic conventions
  (`gen_ai.*`) so dashboards work for every domain
- Grafana stack in-cluster: Tempo (traces), Prometheus (metrics), Loki (logs), Grafana
- `promptfoo` per domain for offline prompt evaluation; prompt id + sha in the run log tie
  results to prompt versions
- Later: one Postgres for the LangGraph checkpointer, the event store, and pgvector

## Repo layout

```
cli.py                      # thin client: --domain, --target
core/                       # the harness library (see table above)
orchestrator_scratch/       # engine.py (hand-built loop) + app.py
orchestrator_lg/            # LangGraph compiler + app (milestone 8)
domains/
  sql/                      # first domain (see "The domain contract")
tests/                      # harness tests: engine, registry, agent loop, architecture rules
infra/                      # base/ + overlays/<domain>/, Makefile targets
docs/                       # step instructions and explanations
```

## Milestones

1. ✅ SQL agents + scratch orchestrator run locally; a single-table question works.
2. ✅ Harness restructure: `domains/sql/`, domain-agnostic `core/`, scratch engine runs
   any `Pipeline`, generic `/ask` envelope with evidence, tool-use support in `core.llm`
   + `core.agent_loop`, architecture rules enforced by tests.
3. Dockerfiles built; the sql domain runs with `docker compose up` (the orchestrator
   image started with `DOMAIN=sql`).
4. k3d/minikube up, sql domain deployed via `infra/base` + `infra/overlays/sql`; CLI hits
   the in-cluster orchestrator (port-forward).
5. Joins and aggregations work ("Top 5 artists by total invoice revenue").
6. Error-retry verified end-to-end: force a failing query, confirm from the logs which
   route handled it (schema re-selection vs. query regeneration) and that the run
   recovered.
7. Observability: OTel in `create_app` / `core.http` / `core.llm`, Grafana stack deployed
   in-cluster, structlog replacing raw JSON prints. A cross-service trace for a full run
   visible in Grafana.
8. `orchestrator_lg` compiles any `Pipeline`; the sql domain works end-to-end locally.
9. LangGraph orchestrator deployed alongside scratch. Resume verified: kill the pod
   mid-run, rerun with the same `run_id`, confirm it continues from the checkpoint.
10. Second domain: `docs`. Acceptance: the domain-contract criterion above holds, and both
    engines answer docs questions unchanged.
11. Eval sets per domain (`domains/<name>/evals/questions.yaml`, 20 questions for sql);
    `pytest` runs every domain's set against both in-cluster orchestrators via `/ask`.
12. `promptfoo` per domain: LLM-as-judge where needed (exact result-set match for sql),
    baseline snapshot, diff report on prompt changes.
13. Third domain: `ops` (agentic tool loop, read-only cluster tools).

## Deliberately deferred (do not build yet)

Event sourcing (`orchestrator_scratch/` will later be refactored into an event-sourced
`orchestrator_es/` — a third engine over the same `Pipeline`), observer agent (likely
becomes the ops domain), human-in-the-loop approvals, OpenAI/Gemini adapters, async
services, web UI.
