# Harness restructure — What it's for

Companion to the revised `CLAUDE.md`. Unlike Steps 0 and 1, this change was implemented
for you; this file explains what moved, why, and how to run things now.

---

## What the restructure achieves

Before, the repo was a SQL agent with harness ambitions: SQL lived in every "shared" layer
(`AskResponse.sql`, `SqlExecute`, `classify_sql_error`, `db_path` in core config), and the
scratch orchestrator *was* the SQL pipeline. Adding a docs or ops agent would have meant
editing core and writing two more orchestrators.

Now `core/` knows nothing about SQL, the orchestrator runs any domain's pipeline, and the
SQL agent is one package, `domains/sql/`. Adding a domain means adding a package. The
acceptance test for that claim is written into CLAUDE.md: outside `domains/<name>/` and
its infra overlay, only `.env.example` may change.

Behaviour is unchanged: the same question gets the same SQL, the same retries and the
same answer, verified end-to-end against Ollama after the move.

---

## Where things went

| Before | After |
|---|---|
| `agents/executor/app.py`, `guard.py` | `domains/sql/tools/executor/` |
| `agents/schema_agent/app.py` | `domains/sql/agents/schema_agent/app.py` |
| `agents/query_agent/app.py` | `domains/sql/agents/query_agent/app.py` |
| `core/errors.py` | `domains/sql/errors.py` |
| `core/prompts/*.txt` | `domains/sql/prompts/*.txt` |
| SQL half of `core/models.py` | `domains/sql/models.py` (+ `SqlState`) |
| SQL half of `core/config.py` | `domains/sql/config.py` (`SqlSettings`, prefix `SQL_`) |
| loop in `orchestrator_scratch/app.py` | `orchestrator_scratch/engine.py` (generic) + `domains/sql/domain.py` (steps, route) |
| `tests/test_guard.py`, `test_errors.py` | `domains/sql/tests/` |
| `requirements-executor.txt` | `requirements-tool.txt` |

New harness modules: `core/pipeline.py`, `core/registry.py`, `core/agent_loop.py`,
`core/http.py`, `core/prompts.py`, `core/service.py`.

The Step 0 and Step 1 docs describe the old layout; read their paths through this table.

---

## Running it now

```bash
uvicorn domains.sql.tools.executor.app:app --port 8000
uvicorn domains.sql.agents.schema_agent.app:app --port 8010
uvicorn domains.sql.agents.query_agent.app:app --port 8020
uvicorn orchestrator_scratch.app:app --port 8100        # needs DOMAIN=sql

python cli.py --domain sql --target scratch "How many customers are from Brazil?"
pytest                                                   # harness + every domain's tests
```

`.env` variables for the domain gained a `SQL_` prefix (`SCHEMA_MODEL` → `SQL_SCHEMA_MODEL`,
`SQL_MODEL` → `SQL_QUERY_MODEL`, `EXECUTOR_URL` → `SQL_EXECUTOR_URL`,
`ORCHESTRATOR_SCRATCH_URL` → `SQL_SCRATCH_URL`, …), plus `DOMAIN=sql`. Your local `.env`
was migrated in place; `.env.example` shows the full set.

---

## Why a pipeline is data

**The problem it solves.** Two orchestrators that each hand-code the same graph agree
only as long as someone keeps them in sync — and every new domain doubles that work. A
`Pipeline` (steps, static edges, conditional routes) is declared once by the domain, and
each engine *executes* it. They can still differ in how they execute (checkpointing,
resume), which is the comparison worth making, but not in what a domain does.

**Why its shape is LangGraph's.** Steps return field updates, routes return the next step
name, and `END` is the literal `"__end__"`. Compiling a pipeline into a `StateGraph` is
then a mapping: steps → nodes, edges → edges, routes → conditional edges. This was checked
with a throwaway script against the installed LangGraph: the toy domain took identical
routes on both engines, with a SQLite checkpointer keyed by `run_id`.

**Why `apply_update` lives in core.** That same check found a divergence: LangGraph
silently drops an update key the state doesn't have, while the scratch engine raised.
A typo in a step would be lost on one engine and fatal on the other. Both engines now
apply updates through `core.pipeline.apply_update`, so they fail the same way.

**Why the route is a pure function of state.** `route_after_execute` reads `error`,
`error_type` and `attempts` and returns a step name — no I/O. That makes the retry table
unit-testable (`domains/sql/tests/test_domain.py::test_route_table`), and it is exactly
what a LangGraph conditional edge wants.

---

## Why domains are loaded by name

`core.registry.load_domain("sql")` imports `domains.sql.domain` at runtime. The
orchestrator never writes `import domains.sql`. If it did, every new domain would be an
edit to the orchestrator. `tests/test_architecture.py` enforces it, along with the other
dependency rules: core imports nothing of ours, domains never import each other, provider
SDKs appear only in `core/llm.py`, LangGraph only in `orchestrator_lg/`, and `os.getenv`
nowhere.

Settings are found the same way, but separately: `load_domain_settings` imports only
`config.py`, so the CLI can find a URL without loading a pipeline.

---

## Why the `/ask` response carries evidence

`AskResponse` no longer has `sql`, `columns` and `rows`. Those were SQL's provenance;
a docs agent's provenance is a citation, and an ops agent's is the commands it ran. The
response now carries `evidence: list[...]` with three presentation kinds — `query`,
`citation` and `tool` — and the CLI prints whatever is there. "Always print the executed
SQL with the answer" became "always show the evidence with the answer", which holds for
every domain.

---

## Why domains don't add event types

`SqlExecute` became `ToolCall(tool="sql.execute", …)`. If each domain added its own event
types, every log consumer — `parse_event`, Grafana dashboards, the future event store —
would need to learn about every domain. One generic tool event with a namespaced name
keeps the log schema fixed. `error_type` became a plain string for the same reason: each
domain classifies failures in its own vocabulary.

`ModelCall` gained `prompt_id`, `prompt_sha`, `system` and `params`. The old event logged
only the user prompt, short of the "full prompt, model + params" requirement. The id and
sha also tie every run to the exact prompt text, which is what a promptfoo regression
needs to be attributable.

---

## Why helpers instead of boilerplate

Each agent used to time its own model call and hand-build a 13-field `ModelCall`; the
orchestrator did the same for every HTTP call. A new domain would copy that, and drift.
Now `llm.call_model(ctx, …)` and `http.post(ctx, …)` emit the events themselves, and
`create_app` builds every service with `/healthz`. These three are also where OTel goes in
milestone 7: added once, inherited by every domain.

`RunContext` is what they share: which run, which service, where events go.
Orchestrators give it a `sequenced` emitter (numbered events); agents use plain `emit`.

---

## Why a tool loop, and why `ToolFailed`

The SQL pipeline is a workflow: the code decides what happens next. A troubleshooting
agent isn't — the model decides whether to read logs or query metrics. `core.agent_loop`
is the hand-built loop for that: model turn, run the tools it asked for, feed results
back, repeat until it answers or `max_turns` runs out. A domain calls it from one step,
so both engines run it unchanged.

The transcript is provider-neutral (`UserTurn`, `AssistantTurn`, `ToolResults`), and
`core.llm` translates it for Anthropic and Ollama. An Anthropic turn keeps its native
content and replays it verbatim, because Claude's thinking blocks must come back
unchanged.

The failure policy is explicit: `ToolFailed` (bad arguments, guard refusal, nothing found)
goes back to the model as an error result it can recover from. Any other exception is a
bug and fails the run — the harness doesn't hide bugs by narrating them to the model.

---

## Why a toy domain in the tests

`tests/fixtures/toy/` is a two-step domain with a retry route. The engine and registry
tests run against it rather than SQL, which proves the engine has no SQL in it: if it
did, the toy domain couldn't run. It is also the smallest example of the domain contract.

---

## What was verified

- `ruff check` and `ruff format --check` are clean; the offline suite passes (136 tests).
- The architecture tests catch real violations: a planted domain import in core, an
  `os.getenv`, and a tool server reaching `httpx` each failed the suite.
- Live against Ollama `llama3.1:8b`: structured output and a full tool loop (the model
  called the tool and answered from its result).
- End-to-end on the new layout: "How many customers are from Brazil?" →
  `SELECT COUNT(*) FROM Customer WHERE Country = 'Brazil' LIMIT 50` → "5 customers are
  from Brazil." The run file parsed line by line, with orchestrator events numbered 1–7.

Not verified: the Anthropic adapter's tool-use path against the real API (no key
configured — it is covered by offline tests against stand-in responses), and the retry
routes against a live model (covered by `test_domain.py` with stubbed services; live
verification is milestone 6).
