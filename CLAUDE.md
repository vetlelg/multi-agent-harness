# SQL Agent — monorepo, two orchestrators

## What to build

A multi-agent system that answers natural-language questions about a real database by
generating and executing SQL — implemented twice in one repo. Two LLM agents and a
deterministic executor are shared; only the orchestration layer differs:

- `orchestrator_scratch/` — hand-built loop, no agent framework.
- `orchestrator_lg/` — LangGraph graph with checkpointing.

Four services per running variant, each in its own container, deployed on Kubernetes.

## Hard rules

- The two orchestrators NEVER import from each other. Both import only from `core/`
  and `agents/`. Everything outside the orchestrators is shared by construction —
  prompts, agents, guard, tests. Never fix or tune something for one orchestrator only.
- `orchestrator_scratch/`: no LangGraph, LangChain, CrewAI, or similar. The loop,
  retries, and coordination are written by hand.
- `orchestrator_lg/`: LangGraph lives here and nowhere else. Check the current
  LangGraph documentation at build time for APIs and package names — do not assume
  them from memory; pin whatever you install. All model calls (including the answer
  node) go through the shared `core/` Anthropic wrapper — no LangChain model wrappers.
- Both orchestrators expose the identical `POST /ask` contract, so the same CLI and
  the same test set run against either.
- NEVER implement code automatically. Always let me (the human) implement it unless I explicitly tell you to implement something for me.

## Database

Chinook sample database, SQLite, read-only — a music store with artists, albums,
tracks, customers, and invoices. `make db` downloads it. The file is baked into the
executor image (deliberate: small, read-only sample data — no seed jobs, and no SQL
dialect drift between dev and cluster).

## Stack

- Python 3.12, venv + pip, pinned `requirements.txt`
- `anthropic` SDK via the shared `core/` wrapper — temperature 0; model name and
  params set in one place (`core/config`)
- `langgraph` (used in `orchestrator_lg/` only)
- FastAPI per service; every service exposes `GET /healthz` for k8s probes
- SQLite (read-only, inside the executor image)
- Docker, k3d or minikube, plain k8s manifests (kustomize acceptable)
- API key: `.env` locally (gitignored); a Kubernetes Secret in-cluster — never in
  images or manifests
- `pytest`, `ruff`

## Shared services (`agents/`)

- **schema_agent** — fetches the full schema from the executor's `/schema` endpoint,
  then uses the LLM to select and return only the tables and columns relevant to the
  question (with types and foreign keys).
- **query_agent** — receives the question plus the selected schema, returns a single
  SQL statement. On retry, also receives the previous SQL and the error message.
- **executor** — the only service with database access. No LLM. Two endpoints:
  `GET /schema` (introspection: tables, columns, types, foreign keys) and
  `POST /execute` (runs SQL through the guard, returns rows or the error).

## Orchestrator: scratch (`orchestrator_scratch/`)

Receives the question, generates a `run_id` (UUID), and drives the pipeline over
HTTP: schema_agent → query_agent → executor. Retry routing on SQL error, max 3
retries per run: "no such table/column" errors → back to schema_agent (which sees
the failed SQL and the error); any other SQL error → back to query_agent. Finally,
one LLM call (prompt: `core/prompts/answer.txt`) turns rows — or exhausted retries —
into a plain-language answer.

## Orchestrator: LangGraph (`orchestrator_lg/`)

State: run_id, question, schema, current SQL, execution result or error, attempt
count, final answer.

Nodes: `get_schema` (calls schema_agent) → `generate_sql` (calls query_agent) →
`execute` (calls executor) → `answer` (one LLM call, prompt `core/prompts/answer.txt`).

Conditional edge after `execute`:
- "no such table/column" error and attempts < 3 → `get_schema`;
- any other SQL error and attempts < 3 → `generate_sql`;
- attempts exhausted → `answer` (failure explanation);
- success → `answer`.

Checkpointing: a persistent checkpointer with a `thread_id` per run — a SQLite file
in dev; in-cluster that file lives on a PersistentVolumeClaim so resume survives pod
restarts. (Swap to a Postgres checkpointer later for the production-grade version.)

## Guard (enforced in executor, in code — never trust the model)

One single read-only statement — `SELECT`, or `WITH … SELECT`; reject
INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/PRAGMA; append `LIMIT 50` if missing;
5-second query timeout.

## Behavior requirements (both orchestrators)

- The `run_id` is passed on every inter-service call and included in every log line.
- Each service emits one JSON line per model call and per tool/HTTP call to stdout
  (visible via `kubectl logs`): run_id, service, full prompt, response, SQL, rows,
  timing, model + params. In dev the orchestrator also appends the whole run to
  `runs/<run_id>.jsonl`. Raw and append-only.
- Always print the executed SQL together with the answer.
- If the schema cannot answer the question, say so — never invent data.
- CLI: `python cli.py --target scratch|lg "How many customers are from Brazil?"`
  (resolves the target orchestrator's URL and calls its `/ask` endpoint).

## Repo layout

```
cli.py                  # thin client; --target picks the orchestrator
docker-compose.yml
core/                   # shared models, LLM wrapper, config, prompts/ — copied into every image
agents/
  schema_agent/         # FastAPI app, Dockerfile
  query_agent/          # FastAPI app, Dockerfile
  executor/             # /schema + /execute + guard, FastAPI app, Dockerfile
orchestrator_scratch/   # hand-built loop + answer call, FastAPI app, Dockerfile
orchestrator_lg/        # LangGraph graph + state + answer node, FastAPI app, Dockerfile
tests/                  # unit tests (guard, schema parsing), integration tests, questions.yaml
infra/                  # k8s manifests, kustomization, Makefile targets
```

## Milestones

1. Shared agents + scratch orchestrator run locally (no containers); a single-table
   question works end-to-end.
2. Dockerfiles built; the scratch path runs with `docker compose up`.
3. k3d/minikube up, scratch path deployed via manifests, CLI hits the in-cluster
   orchestrator (port-forward).
4. Joins and aggregations work ("Top 5 artists by total invoice revenue").
5. Error-retry verified end-to-end: force a failing query, confirm from the logs
   which path handled it (schema re-selection vs. query regeneration) and that the
   run recovered.
6. `orchestrator_lg` runs locally against the same shared services; single-table
   question works end-to-end.
7. LangGraph orchestrator deployed alongside the scratch one. Resume verified: kill
   the pod mid-run, rerun with the same `thread_id`, confirm it continues from the
   checkpoint (works because the checkpointer is on the PVC).
8. 20-question test set with expected answers in `tests/questions.yaml`; `pytest`
   runs it against BOTH in-cluster orchestrators via the shared `/ask` contract.

## Deliberately deferred (do not build yet)

Event sourcing (`orchestrator_scratch/` will later be refactored into an
event-sourced `orchestrator_es/`), observer agent, evaluation/experiments harness,
web UI.