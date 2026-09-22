# Step 1 — Instructions

Exact actions only. For the reasoning behind any of it, see `step-1-explained.md`.

**Convention:** file sections give you names, signatures, fields and required behaviour.
They are specifications, not source. You write the bodies.

---

## 1.1 Executor: app skeleton and schema introspection — DONE

Create `agents/executor/__init__.py` (empty) and `agents/executor/app.py`.

FastAPI app. Three endpoints by the end of this step; start with two:

**`GET /healthz`** — returns `{"status": "ok"}`.

**`GET /schema`** — returns `SchemaResponse` (from `core.models`).

Introspection uses a **read-only** connection to `settings.db_path`:
- URI format: `file:{db_path}?mode=ro`
- No authorizer on this connection — introspection needs PRAGMA.
- Open once at startup, not per request.

Query `sqlite_master` for table names where `type = 'table'`. Skip tables whose name
starts with `sqlite_`.

For each table:
- `PRAGMA table_info({table_name})` for columns.
- `PRAGMA foreign_key_list({table_name})` for foreign keys.

Column mapping from `PRAGMA table_info`:

| Result index | PRAGMA name | Model field | Conversion |
|---|---|---|---|
| 1 | `name` | `name` | as-is |
| 2 | `type` | `type` | as-is (may be empty for untyped) |
| 3 | `notnull` | `nullable` | inverted: `0` → `True` |
| 5 | `pk` | `pk` | `> 0` → `True` |

Foreign key mapping from `PRAGMA foreign_key_list`:

| Result index | PRAGMA name | Model field |
|---|---|---|
| 3 | `from` | `column` |
| 2 | `table` | `references_table` |
| 4 | `to` | `references_column` |

Verify:

```bash
uvicorn agents.executor.app:app --port 8000
# in another terminal:
curl http://localhost:8000/healthz
curl http://localhost:8000/schema | python -m json.tool | head -40
```

Confirm tables like Album, Artist, Customer, Track, Invoice, InvoiceLine appear with
columns and foreign keys.

---

## 1.2 Executor: guard — DONE

Create `agents/executor/guard.py`.

Exports:

```
def validate_and_rewrite(sql: str, *, row_limit: int = settings.row_limit) -> str
def make_authorizer() -> Callable
```

### `validate_and_rewrite`

Raises `GuardRejection` (from `core.errors`) on refusal. Returns the (possibly
rewritten) SQL on success. Never executes SQL — text only.

Steps, in order:

1. Strip leading/trailing whitespace. Strip a single trailing semicolon if present,
   then strip trailing whitespace again.
2. Reject if empty.
3. Reject if `;` remains anywhere (multi-statement).
4. First keyword (case-insensitive) must be `SELECT` or `WITH`. Reject otherwise.
5. Scan for forbidden keywords as whole words (case-insensitive, `\b` boundaries):
   `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `ATTACH`, `PRAGMA`.
6. If `LIMIT` does not appear as a whole word (case-insensitive), append
   ` LIMIT {row_limit}`.
7. Return the SQL.

### `make_authorizer`

Returns a callback for `connection.set_authorizer()`. This is the real security
boundary — the text check above is for clear error messages.

Allowlist — return `sqlite3.SQLITE_OK`:

| Constant | Value | Purpose |
|---|---|---|
| `SQLITE_SELECT` | 21 | Entering a SELECT |
| `SQLITE_READ` | 20 | Reading a column value |
| `SQLITE_FUNCTION` | 31 | Calling a function |
| `SQLITE_RECURSIVE` | 33 | WITH RECURSIVE |

Everything else: return `sqlite3.SQLITE_DENY`.

---

## 1.3 Executor: `POST /execute` — DONE

Add to `agents/executor/app.py`.

**`POST /execute`** — receives `ExecuteRequest`, returns `ExecuteResponse`.

Execution connection (separate from the introspection connection):
- Read-only (`mode=ro` URI).
- Authorizer set via `make_authorizer()`.
- Opened once at startup.

Per-request flow:

1. Call `validate_and_rewrite(request.sql)`. On `GuardRejection` return
   `ExecuteResponse(ok=False, error_type=ErrorType.GUARD_REJECTED, error=str(e),
   elapsed_ms=0, sql_executed=None)`.
2. Set a progress handler for timeout:
   `connection.set_progress_handler(handler, 1000)`.
   The handler checks elapsed time against `settings.query_timeout_s` and returns
   non-zero to interrupt.
3. Execute with `cursor.execute(rewritten_sql)`.
4. On success: read `cursor.description` for column names, `cursor.fetchall()` for
   rows. Return `ExecuteResponse(ok=True, sql_executed=rewritten_sql,
   columns=[col[0] for col in description], rows=[list(r) for r in raw_rows],
   row_count=len(rows), truncated=(len(rows) == row_limit), elapsed_ms=...)`.
5. On `sqlite3.DatabaseError` (catches both `OperationalError` and
   `ProgrammingError`): classify with `classify_sql_error(e)`, return
   `ExecuteResponse(ok=False, error_type=..., error=str(e), elapsed_ms=...,
   sql_executed=rewritten_sql)`.
6. Always clear the progress handler after: `connection.set_progress_handler(None, 0)`.

Emit a `SqlExecute` event for every execution (success and failure). Service name:
`"executor"`. Leave `seq` as `None` — the executor is not the orchestrator.

Verify:

```bash
# Executor running on :8000

# Success
curl -s -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d "{\"run_id\":\"test\",\"sql\":\"SELECT COUNT(*) FROM Customer WHERE Country = 'Brazil'\"}" \
  | python -m json.tool
# expect: ok=true, rows present

# Guard rejection
curl -s -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d "{\"run_id\":\"test\",\"sql\":\"DROP TABLE Customer\"}" \
  | python -m json.tool
# expect: ok=false, error_type=guard_rejected

# LIMIT injection
curl -s -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d "{\"run_id\":\"test\",\"sql\":\"SELECT * FROM Track\"}" \
  | python -m json.tool
# expect: ok=true, row_count<=50, truncated=true
```

---

## 1.4 Guard tests — DONE

Create `tests/test_guard.py`.

Test cases for `validate_and_rewrite`:

| Input | Expected |
|---|---|
| `SELECT 1` | passes, `LIMIT 50` appended |
| `  SELECT 1 ;  ` | passes (stripped) |
| `WITH cte AS (SELECT 1) SELECT * FROM cte` | passes |
| `select * from Track` | passes (case-insensitive), `LIMIT 50` appended |
| `SELECT * FROM Track LIMIT 10` | passes, LIMIT unchanged |
| `SELECT 1; SELECT 2` | `GuardRejection` |
| `INSERT INTO T VALUES (1)` | `GuardRejection` |
| `UPDATE T SET x=1` | `GuardRejection` |
| `DELETE FROM T` | `GuardRejection` |
| `DROP TABLE T` | `GuardRejection` |
| `ALTER TABLE T ADD x` | `GuardRejection` |
| `ATTACH DATABASE ':memory:' AS m` | `GuardRejection` |
| `PRAGMA table_info(T)` | `GuardRejection` |
| `""` (empty / whitespace) | `GuardRejection` |

Verify:

```bash
pytest tests/test_guard.py -v
```

---

## 1.5 Schema agent — DONE

Create `agents/schema_agent/__init__.py` (empty) and `agents/schema_agent/app.py`.

FastAPI app.

**`GET /healthz`** — returns `{"status": "ok"}`.

**`POST /select-schema`** — receives `SelectSchemaRequest`, returns
`SelectSchemaResponse`.

Flow:

1. Fetch full schema: `GET {settings.executor_url}/schema` via `httpx`. Parse as
   `SchemaResponse`.
2. Render the full schema as text for the LLM. The text must include: table names,
   column names with types, primary key markers, nullable/not-null markers, and
   foreign key relationships. Use a format the model can read as DDL-like structure.
3. Compose the user prompt. Must include:
   - The full schema text from step 2.
   - The question.
   - If `previous_sql` is set (retry): the failed SQL and the error.
4. Call the LLM:
   - `system`: contents of `core/prompts/schema_select.txt` (read once at startup).
   - `user`: the composed prompt.
   - `output_model`: `SelectedSchemaOut`.
   - Client: `get_client(settings.schema_model)`.
5. Look up full details from the fetched schema:
   - For each table in `SelectedSchemaOut.tables`, find the matching `TableInfo`
     from the full schema.
   - Filter its columns to only those the model selected.
   - Keep only foreign keys whose `column` is in the selected columns.
   - Silently skip any table or column name the model returned that doesn't exist
     in the real schema (hallucination).
6. Render `schema_text` from the filtered tables (same rendering function as step 2,
   applied to the subset).
7. Return `SelectSchemaResponse(tables=filtered_tables, schema_text=schema_text)`.

Emit events (all with `service="schema_agent"`, `seq=None`):
- `HttpCall` for the executor request.
- `ModelCall` for the LLM call (`role="schema"`).

HTTP client: `httpx.Client` with `timeout=settings.http_timeout_s`. Create once, reuse.

Verify:

```bash
# Executor on :8000
uvicorn agents.schema_agent.app:app --port 8010
curl -s -X POST http://localhost:8010/select-schema \
  -H "Content-Type: application/json" \
  -d "{\"run_id\":\"test\",\"question\":\"How many customers are from Brazil?\"}" \
  | python -m json.tool
# expect: Customer table selected, schema_text present
```

---

## 1.6 Query agent — DONE

Create `agents/query_agent/__init__.py` (empty) and `agents/query_agent/app.py`.

FastAPI app.

**`GET /healthz`** — returns `{"status": "ok"}`.

**`POST /generate-sql`** — receives `GenerateSqlRequest`, returns
`GenerateSqlResponse`.

Flow:

1. Compose the user prompt. Must include:
   - The `schema_text`.
   - The question.
   - The attempt number.
   - If `previous_sql` is set (retry): the failed SQL and the error.
2. Call the LLM:
   - `system`: contents of `core/prompts/generate_sql.txt` (read once at startup).
   - `user`: the composed prompt.
   - `output_model`: `SqlOut`.
   - Client: `get_client(settings.sql_model)`.
3. Return `GenerateSqlResponse(sql=result.parsed.sql)`.

Emit: `ModelCall` event (`service="query_agent"`, `role="sql"`, `seq=None`).

Verify:

```bash
# Executor on :8000, schema agent on :8010
uvicorn agents.query_agent.app:app --port 8020

# First get schema_text from the schema agent, then use it:
curl -s -X POST http://localhost:8020/generate-sql \
  -H "Content-Type: application/json" \
  -d "{\"run_id\":\"test\",\"question\":\"How many customers are from Brazil?\",\"schema_text\":\"<paste from 1.5>\",\"attempt\":1}" \
  | python -m json.tool
# expect: a SELECT statement
```

---

## 1.7 Orchestrator scratch — DONE

`orchestrator_scratch/__init__.py` already exists. Create
`orchestrator_scratch/app.py`.

FastAPI app.

**`GET /healthz`** — returns `{"status": "ok"}`.

**`POST /ask`** — receives `AskRequest`, returns `AskResponse`.

Flow:

1. Generate `run_id` via `new_run_id()` if not provided.
2. Initialise `RunState(run_id=run_id, question=request.question)`.
3. Initialise a per-request sequence counter (starts at 0, increments before each
   `emit`).
4. Emit `RunStart(orchestrator="scratch")`.
5. **Pipeline loop** — while `state.attempts < settings.max_attempts`:

   a. **Schema** — if `state.schema_text is None` or
      `state.error_type is ErrorType.MISSING_OBJECT`:
      - POST `SelectSchemaRequest` to `{settings.schema_agent_url}/select-schema`.
      - Parse response as `SelectSchemaResponse`.
      - Set `state.schema_text = response.schema_text`.
      - Emit `HttpCall`.

   b. **SQL** — POST `GenerateSqlRequest` to
      `{settings.query_agent_url}/generate-sql`.
      - Set `state.sql = response.sql`.
      - Emit `HttpCall`.

   c. **Execute** — POST `ExecuteRequest` to `{settings.executor_url}/execute`.
      - Emit `HttpCall`.

   d. Increment `state.attempts`.

   e. If `response.ok`:
      - Set `state.columns`, `state.rows` from the response.
      - Clear `state.error`, `state.error_type`.
      - Emit `RouteDecision(attempt=state.attempts, error_type=None, branch="answer")`.
      - Break.

   f. If not `response.ok`:
      - Set `state.error = response.error`, `state.error_type = response.error_type`.
      - If `is_retryable(state.error_type)` and `state.attempts < settings.max_attempts`:
        - `branch`: `"get_schema"` for `MISSING_OBJECT`, `"generate_sql"` for `SYNTAX`.
        - Emit `RouteDecision(attempt=state.attempts, error_type=state.error_type, branch=branch)`.
        - Continue the loop.
      - Else (not retryable or attempts exhausted):
        - Emit `RouteDecision(attempt=state.attempts, error_type=state.error_type, branch="answer")`.
        - Break.

6. **Answer** — one direct LLM call (not via an agent service):
   - `system`: contents of `core/prompts/answer.txt` (read once at startup).
   - `user`: the question, the executed SQL, and either the result rows or the
     error information if the run failed.
   - `output_model`: `AnswerOut`.
   - Client: `get_client(settings.answer_model)`.
   - Set `state.answer = result.parsed.answer`.
   - Emit `ModelCall(role="answer")`.
7. Emit `RunEnd(attempts=state.attempts, ok=(state.error is None), answer=state.answer, error=state.error)`.
8. Return `AskResponse(run_id=state.run_id, orchestrator="scratch",
   question=state.question, sql=state.sql, columns=state.columns,
   rows=state.rows, answer=state.answer, attempts=state.attempts,
   error=state.error)`.

HTTP client: `httpx.Client` with `timeout=settings.http_timeout_s`. Create once, reuse.

Rules:
- Every emitted event carries `run_id`, `service="orchestrator_scratch"`, and the
  next `seq` value.
- `GenerateSqlRequest.attempt` is `state.attempts + 1` (1-indexed, before incrementing).
- Read all three prompt files once at startup, not per request.
- The answer prompt must handle both the success case (rows present) and the failure
  case (no rows, error present).

Verify:

```bash
# All three services running (:8000, :8010, :8020)
uvicorn orchestrator_scratch.app:app --port 8100

curl -s -X POST http://localhost:8100/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"How many customers are from Brazil?\"}" \
  | python -m json.tool
# expect: answer with the count, sql present, attempts=1, error=null
```

---

## 1.8 CLI — DONE

Create `cli.py`.

```
python cli.py --target scratch|lg "How many customers are from Brazil?"
```

- `--target`: required, choices `scratch` or `lg`.
- Positional argument: the question.
- Resolve URL: `scratch` → `settings.orchestrator_scratch_url`,
  `lg` → `settings.orchestrator_lg_url`.
- POST `AskRequest(question=question)` to `{url}/ask` via `httpx`.
- Parse response as `AskResponse`.
- Print: the executed SQL, then a blank line, then the answer.
- Exit 0 on success (`response.error is None`), exit 1 otherwise.

Use `argparse` and `httpx`.

---

## 1.9 Run locally and verify

Start all four services in separate terminals:

```bash
# Terminal 1
uvicorn agents.executor.app:app --port 8000

# Terminal 2
uvicorn agents.schema_agent.app:app --port 8010

# Terminal 3
uvicorn agents.query_agent.app:app --port 8020

# Terminal 4
uvicorn orchestrator_scratch.app:app --port 8100
```

Then:

```bash
python cli.py --target scratch "How many customers are from Brazil?"
```

Expected: a correct count, the SQL printed above the answer.

Verify the run file (requires `RUNS_DIR=runs` in `.env`):

```bash
python -c "
from pathlib import Path
from core.events import parse_event
for f in sorted(Path('runs').glob('*.jsonl')):
    for line in f.read_text().splitlines():
        e = parse_event(line)
        print(f'{e.type:20s} seq={e.seq}')
"
```

Expect: `run_start`, `http_call` (×3 minimum), `route_decision`, `model_call`
(answer), `run_end` — all with sequential `seq` values starting at 1. The agents'
own `model_call` events appear in their terminal stdout, not in the orchestrator's
run file.

---

## Done when

```bash
pytest tests/test_guard.py -v              # guard tests green
pytest -v                                  # all tests still green (step 0 + guard)
ruff check .                               # clean
```

- All four services start without error.
- `python cli.py --target scratch "How many customers are from Brazil?"` prints a
  correct answer and the SQL that produced it.
- `runs/<run_id>.jsonl` exists (when `RUNS_DIR=runs`), every line parses with
  `parse_event`, and events carry sequential `seq` values.
