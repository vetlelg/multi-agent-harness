# Step 6 — Instructions

Exact actions only. For the reasoning behind any of it, see `step-6-explained.md`.

**Convention:** file sections give you names, signatures, fields and required behaviour.
They are specifications, not source. Exact strings are given only where the exact string
is the point: prompts (their text is what the model sees, and their sha lands in the run
log), SQLite's error messages, file names, and commands.

**Goal (milestone 6):** joins and aggregations work. "Who are the top 5 artists by total
invoice revenue?" is answered from a correct four-table join. The eval pass rate holds at
or above the step-5 baseline (16 of 24, 67%), measured the same way.

**What changes:** only the sql domain, plus `CLAUDE.md`. No harness file, image, manifest
or eval case changes. Every change answers a failure that the step-5 baseline recorded,
or that a probe of the running system showed (see `step-6-explained.md`):

| Change | Where | Answers |
|---|---|---|
| Double-quoted strings off; the row cap enforced in code | executor | A missing column read as a string; a `LIMIT` in a subquery lifting the cap |
| Three more error messages classified | `errors.py` | `ambiguous column name` ended a run after one attempt |
| The model names tables; code adds the join path, every column, and the join conditions | schema agent | Tables selected without their key or name columns; joins on unrelated ids |
| The query agent may decline | query agent | No way to say "the data can't answer" except failing three times |
| Two new routes, and the answer step told about truncation | pipeline | Retries that "recovered" into invented answers; totals made up from a cut list |
| Rules for joins, grouping and aggregation | prompts | Joins on unrelated ids, grouping by id, double counting |

**State of play:** 6.0–6.9 are done. Claude implemented them on request and ran every
check below on 2026-10-06. Measured twice, with one change in between (`Joins`, and "an
error is not a reason to decline"). The second report is recorded in
`domains/sql/evals/baseline.json`: 21 of 24 trials, 88%. Nothing is committed yet. The
commit is yours.

**Checked against:** the four sql services as local processes (uvicorn) on this branch,
not the cluster: Docker Desktop wasn't running. Ollama `llama3.1:8b` on CPU for all three
roles, as in the overlay. Python 3.14.2 with SQLite 3.50.4 locally; the images are 3.12.15
with SQLite 3.46.1. See "What was checked" at the end of `step-6-explained.md`.

---

## 6.0 Before you start — DONE

- Step 5 is merged into `main`. Branch `build/step-6` from it.
- `data/chinook.db` exists (`make db`). The new selection and executor tests read it.
- Ollama is running and has `llama3.1:8b`: `curl http://localhost:11434/api/tags`.
- To measure (6.8) you need a running sql stack. Either the cluster (`make k8s-up`, then
  `make k8s-forward` in a second terminal), or the four services as local processes:

  ```bash
  uvicorn domains.sql.tools.executor.app:app --port 8000
  uvicorn domains.sql.agents.schema_agent.app:app --port 8010
  uvicorn domains.sql.agents.query_agent.app:app --port 8020
  uvicorn orchestrator_scratch.app:app --port 8100
  ```

  Both serve the scratch orchestrator on `localhost:8100`, which is where the eval runner
  looks. Local processes read `.env`, so set the three `SQL_*_MODEL` values there to the
  overlay's `ollama:llama3.1:8b` to measure what the baseline measured.

Nothing to install. `Connection.setconfig` (6.1) is new in Python 3.12, which the images
and CLAUDE.md already require.

---

## 6.1 Executor: `domains/sql/tools/executor/app.py` — DONE

### Double-quoted strings off

In `_query_connection`, before the connection is yielded:

```
conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
```

Only the query connection. `_introspect` runs our own SQL and is unchanged.

Add a clause to the docstring: double-quoted names are identifiers only, never strings.

### The row cap, in code

In `execute`, read the rows with `cursor.fetchmany(settings.row_limit)` instead of
`fetchall()`. `truncated` stays `row_count == settings.row_limit`.

The guard is unchanged. It still appends `LIMIT <row_limit>` to SQL that has no `LIMIT`,
which lets SQLite stop early and shows the cap in the evidence. But a `LIMIT` anywhere,
even `LIMIT 1000` or one inside a subquery, stops it appending. From now on the cap is
whatever `fetchmany` reads.

---

## 6.2 Errors: `domains/sql/errors.py` — DONE

### A new error type

`ErrorType.UNANSWERABLE = "unanswerable"`. SQLite never produces it. A step sets it when it
finds that the data cannot answer the question (6.5). It is not retryable: leave
`_RETRYABLE` as it is.

### Three more messages

Each was produced by running a failing query on `data/chinook.db`. The first is also the
one the baseline recorded.

| Message, as SQLite gives it | Match | Type |
|---|---|---|
| `ambiguous column name: ArtistId` | prefix `ambiguous column name:` | `SYNTAX` |
| `misuse of aggregate function SUM()` | prefix `misuse of aggregate` | `SYNTAX` |
| `no such column: "AC/DC" - should this be a string literal in single-quotes?` | ends with `should this be a string literal in single-quotes?` | `SYNTAX` |

- The two prefixes go in `_PREFIXES`.
- The third is a module constant, `_DQS_HINT`. `classify_sql_error` checks it before the
  prefixes, because the message also starts with `no such column:`. SQLite adds the hint
  only for a double-quoted name that resolves to nothing. `no such column: T.Foo` and
  `no such column: age` remain `MISSING_OBJECT`.

---

## 6.3 Schema agent: tables from the model, columns and joins from the schema — DONE

### Model output: `domains/sql/models.py`

- `SelectedSchemaOut` becomes one field, `tables: list[str]`: table names only. Update its
  docstring. Columns, types, keys and joins all come from the real schema.
- Delete `SelectedTableOut`.

### New module: `domains/sql/agents/schema_agent/selection.py`

Pure functions: no I/O, no model, no settings. Imports `collections`, `domains.sql.models`
and nothing else of ours.

```
def select_tables(schema: list[TableInfo], names: list[str]) -> list[TableInfo]
```

1. **Match.** Each name is matched to a table of `schema`, ignoring case (SQLite's names
   are case-insensitive). Names that match nothing are dropped. A table named twice counts
   once.
2. **Connect.** Treat the foreign keys as an undirected graph. Two different tables are
   neighbours when either has a foreign key to the other; a self-reference
   (`Employee.ReportsTo`) is not an edge. Take the matched tables in schema order. Each one
   not yet in the result is joined to it by the shortest path from any table already in
   it (breadth-first, neighbours in name order). Every table on that path is added. A
   table that no path reaches is added alone, unconnected.
3. **Return** the matched and added tables in schema order, each exactly as in `schema`:
   every column, every foreign key.

An empty `names`, or names that all match nothing, give `[]`.

```
def render_schema(tables: list[TableInfo]) -> str
```

`_render_schema`, moved here from `app.py`, with two changes:
- `[]` renders as `""`.
- After the tables, one more block lists the join conditions between the tables shown.
  It is `Joins:` followed by one line per foreign key whose `references_table` is among
  `tables`, in table order, indented two spaces:
  `<table>.<column> = <references_table>.<references_column>`. When there are none, the
  block is the single line `Joins: none`.

`REFERENCES` stays on every foreign-key column, also when the referenced table isn't
shown: it says what the column holds. `Joins` says which joins can be made. For `Invoice`
and `Artist`, the block reads:

```
Joins:
  Album.ArtistId = Artist.ArtistId
  InvoiceLine.TrackId = Track.TrackId
  InvoiceLine.InvoiceId = Invoice.InvoiceId
  Track.AlbumId = Album.AlbumId
```

### `domains/sql/agents/schema_agent/app.py`

- After the model call:
  `tables = select_tables(full_schema.tables, result.parsed.tables)`.
- Return `SelectSchemaResponse(tables=tables, schema_text=render_schema(tables))`.
- Render the full schema for the prompt with `render_schema` too.
- Delete the old column-filtering loop. An empty `tables` is a valid response: the
  pipeline reads it as "the data cannot answer" (6.5).

---

## 6.4 Query agent: it may decline — DONE

### `domains/sql/models.py`

`SqlOut`, the model's output, in this field order:

| Field | Type | Meaning |
|---|---|---|
| `answerable` | `bool` | False when the schema holds nothing that answers the question |
| `missing` | `str` | What the question needs that the schema lacks. `""` when answerable. |
| `sql` | `str` | The query. `""` when not answerable. |

The order is deliberate: the model decides before it writes SQL. Keep it flat: no
optionals, per the comment above the model schemas.

`GenerateSqlResponse`, the agent's response:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `sql` | `str \| None` | `None` | The query, or `None` when the agent declined |
| `missing` | `str \| None` | `None` | Why it declined |

An `after` model validator raises `ValueError` unless exactly one of the two is set.

### `domains/sql/agents/query_agent/app.py`

From the parsed `SqlOut`:
- `answerable` false: `GenerateSqlResponse(missing=...)`, with the model's `missing`
  stripped, or `"no reason given"` when that is empty. The `sql` field is ignored.
- Otherwise: `GenerateSqlResponse(sql=out.sql)`, even when it is empty. The executor's
  guard rejects an empty query, as it does today.

---

## 6.5 Pipeline: `domains/sql/domain.py` — DONE

### Two exits for "the data cannot answer"

A module constant, `CANNOT_ANSWER = "the data cannot answer this: "`, prefixes the error
that both exits set.

- **`get_schema`**: when `response.tables` is empty, also return
  `error = CANNOT_ANSWER + "no table holds what the question asks for"` and
  `error_type = ErrorType.UNANSWERABLE`.
- **`generate_sql`**: when `response.sql is None`, return only
  `error = CANNOT_ANSWER + response.missing` and `error_type = ErrorType.UNANSWERABLE`.
  Leave `sql` as it was: the evidence keeps showing the last query actually attempted, if
  there was one.

`attempts` counts executions, so a decline does not change it.

### Routes

```
def _unless_unanswerable(next_step: str) -> Route[SqlState]
```

Its `decide` returns `"answer"` when `state.error_type == ErrorType.UNANSWERABLE`, and
`next_step` otherwise. Its targets are `{next_step, "answer"}`.

The pipeline becomes:

| Step | Then |
|---|---|
| `get_schema` | route `_unless_unanswerable("generate_sql")` |
| `generate_sql` | route `_unless_unanswerable("execute")` |
| `execute` | route `route_after_execute` (unchanged) |
| `answer` | edge to `END` |

A stale `error_type` from a failed execution is never `unanswerable`, so on a retry
these routes go on as before.

Update the module docstring's diagram:

```
get_schema -> generate_sql -> execute -(route)-> answer -> END
     ^             ^             |  |
     |             |  syntax     |  |
     |             +-------------+  |
     +------------------------------+  missing table/column

get_schema and generate_sql route to answer instead when they find
that the data cannot answer the question.
```

### The answer step

`answer` builds its prompt from one of three cases, in this order:

1. **Rows** (`state.rows is not None`): as today. When `state.truncated`, add a section
   `## Note` that reads
   `The result was cut at <len(rows)> rows; the full result may have more.`
2. **Cannot answer** (`state.error_type == ErrorType.UNANSWERABLE`): `## Question`, then
   `## Cannot answer` with `state.error`.
3. **Failure**: as today.

---

## 6.6 Prompts: `domains/sql/prompts/` — DONE

The exact text of each file. End each with a single newline.

### `schema_select.txt`

```
You are a database schema selector. Given the full database schema and a natural-language question, name the tables needed to answer the question.

Rules:
- Name every table that holds a value the answer needs: the values to filter, group, count or add up, and the names or titles to show.
- Use the table names exactly as they appear in the schema.
- You need not name tables that only link others together. The joins between the tables you name are added for you.
- If no table stores the information the question asks for, return an empty list. A similar column in a table about something else does not count: it answers a different question.

If previous SQL and an error are provided, the previous attempt failed. Use the error to correct the selection. If the error says a column does not exist, check whether any table holds that information under another name. If none does, return an empty list.
```

### `generate_sql.txt`

```
You are a SQL query generator for SQLite. Given a natural-language question and the relevant database schema, write one SQL query that answers the question, or say that the schema cannot answer it.

Rules for the query:
- One SELECT statement (or WITH ... SELECT). Read-only: no INSERT, UPDATE, DELETE, DROP, ALTER, ATTACH, or PRAGMA.
- Use only the tables and columns in the schema.
- Join tables only with the conditions listed under Joins, and give every join its ON condition. If no join is listed, use one table.
- When the query uses more than one table, qualify every column with its table name or alias.
- When the question asks about each X, or the top X, select the name or title of X, not only its id.
- Compute totals, counts, averages and rankings in the query. Only the first rows of a result are returned, so never leave the adding up to the reader.
- After a join, a value from the table on the "one" side repeats on every matching row of the "many" side. Add up amounts from the most detailed table in the join: line items, not the total of the invoice or order they belong to.
- Quote string values with single quotes.

If the schema has no column that holds what the question asks for, set answerable to false, say in missing what the schema lacks, and leave sql empty. Do not substitute data about something else that only looks similar. Otherwise set answerable to true, leave missing empty, and write the query.

If previous SQL and an error are provided, fix the query based on the error message. An error in the previous query is not a reason to set answerable to false: only a schema without the information is.
```

### `answer.txt`

```
You are an answer generator. Given a natural-language question, the SQL query that was executed, and the resulting rows, produce a clear, concise answer.

Rules:
- Use only the data in the returned rows. Do not invent or assume data.
- If the rows are empty, say that no results were found.
- If the result was cut short, say that only the first rows are shown. Never compute a total, count or average from a result that was cut short.
- If the data cannot answer the question, say so and say what is missing. Do not offer a number.
- If the query failed, explain that plainly.
- Include specific numbers and names from the data when relevant.
```

---

## 6.7 Tests — DONE

All offline, in the default run. "needs db" means the `needs_db` skip that
`test_services.py` already uses.

### `domains/sql/tests/test_errors.py`

Add to the classification table:

| Message | Type |
|---|---|
| `ambiguous column name: ArtistId` | `SYNTAX` |
| `misuse of aggregate function SUM()` | `SYNTAX` |
| `no such column: "AC/DC" - should this be a string literal in single-quotes?` | `SYNTAX` |
| `no such column: T.Foo` | `MISSING_OBJECT` |

Add `(ErrorType.UNANSWERABLE, False)` to the retryable table.

### `domains/sql/tests/test_selection.py` (new)

Against a small schema built by hand, with no database:
`A(id)`, `B(id, a_id → A)`, `C(id, b_id → B)`, `D(id)` with no keys, and
`E(id, parent_id → E)`.

- Names match ignoring case; unknown names are dropped; a repeated name counts once.
- `[]` and `["nope"]` give `[]`.
- `["A", "C"]` adds `B`, returns `A, B, C` in schema order, and keeps the foreign keys
  `B → A` and `C → B`.
- `["B"]` alone returns `B`, still with its foreign key to `A`.
- `["D", "A"]`: nothing reaches `D`; both are returned, nothing is added.
- `["E"]` returns `E` with its self-reference kept and nothing added.
- Every table comes back with all its columns.
- `render_schema` of `["B"]` labels `a_id` with `REFERENCES A(id)` and ends with
  `Joins: none`. Of `["A", "C"]`, it ends with the two joins `B.a_id = A.id` and
  `C.b_id = B.id`. Of `[]`, it is `""`.

Against Chinook (needs db), with the executor's introspected schema:

- `["invoice", "ARTIST"]` gives exactly `Album, Artist, Invoice, InvoiceLine, Track`.
- `["Genre", "Track"]` gives exactly `Genre, Track`, and `Genre` has its `Name` column.

### `domains/sql/tests/test_services.py`

Executor (needs db):

- `WHERE Name = "AC/DC"` on `Artist` fails with `error_type` `syntax`, and the error
  mentions single quotes.
- Parametrized: `SELECT * FROM Track LIMIT 1000`, and
  `SELECT * FROM (SELECT * FROM Track LIMIT 5) UNION ALL SELECT * FROM Track`. Each returns
  `row_limit` rows, with `truncated` true.

Schema agent (needs db): with `http.get` returning the executor's schema and
`llm.call_model` returning `SelectedSchemaOut(tables=["Invoice", "Artist"])`, both
monkeypatched, `POST /select-schema` returns the five tables above, and `schema_text`
contains `REFERENCES Artist(ArtistId)` and `Album.ArtistId = Artist.ArtistId`.

Query agent: with `llm.call_model` monkeypatched,
- `SqlOut(answerable=False, missing="no birth date", sql="")` gives `sql` `null` and
  `missing` `"no birth date"`;
- `SqlOut(answerable=False, missing="", sql="")` gives `missing` `"no reason given"`;
- `SqlOut(answerable=True, missing="", sql="SELECT 1")` gives `sql` `"SELECT 1"`.

### `domains/sql/tests/test_models.py`

- Add `GenerateSqlResponse(missing="no birth date")` to the round-trip cases.
- `GenerateSqlResponse()` and `GenerateSqlResponse(sql="SELECT 1", missing="x")` each
  raise `ValidationError`.

### `domains/sql/tests/test_domain.py`

The stub's `/select-schema` returns one table by default, so the run goes on. Let a test
script a different schema response, and a declining `/generate-sql` response. Capture the
`user` prompt of every `model:answer` call.

Every routed step now emits a `RouteDecision`, so the routes a run takes are its path.
Update the existing expectations:

| Test | Routes |
|---|---|
| success first time | `generate_sql, execute, answer` |
| missing object | `generate_sql, execute, get_schema, generate_sql, execute, answer` |
| syntax error | `generate_sql, execute, generate_sql, execute, answer` |
| attempts exhausted | `generate_sql, execute`, then `generate_sql, execute` once per extra attempt, then `answer` |
| non-retryable | `generate_sql, execute, answer` |

New scenarios:

| Scenario | Routes | Then |
|---|---|---|
| The schema agent selects nothing | `answer` | No `/generate-sql` call. `attempts` 0, `error` starts with `CANNOT_ANSWER`, `evidence` `[]`. |
| The query agent declines | `generate_sql, answer` | No `/execute` call. `error` is `CANNOT_ANSWER` + the agent's `missing`. |
| A syntax error, then a decline | `generate_sql, execute, generate_sql, answer` | `attempts` 1. The evidence still shows the failed SQL. |
| A truncated result | `generate_sql, execute, answer` | The answer prompt contains `cut at` |
| The decline's answer prompt | | It contains `## Cannot answer` and the agent's `missing` |

Route table: add rows for both `_unless_unanswerable` routes, taken from the pipeline
(`sql.PIPELINE.routes["get_schema"].decide`): `unanswerable` gives `answer`; `None`,
`syntax` and `missing_object` give the next step.

```bash
pytest domains/sql -v
ruff check . && ruff format --check .
```

---

## 6.8 Run it, compare, and record the new baseline — DONE

```bash
pytest                                  # everything offline
make k8s-up && make k8s-forward         # or the four local processes from 6.0
pytest -m live tests/test_evals_live.py
```

The table shows the step-5 baseline's rate next to each case. Read every failure in the
`failures:` section. Its `run_id` leads to the run's events: `runs/<run_id>.jsonl` for
local processes, or `kubectl logs` grepped as in step 5, 5.6.

Done means:
- overall at or above the baseline's 67%;
- `top-artists-revenue` passes at least once;
- no case below its baseline rate by more than one trial in three, unless its failures are
  read and explained.

The recorded run gave this table, at 39 seconds per trial on average, 16 minutes in all.
Yours will differ, because model output varies:

```
sql on scratch: 3 trial(s) per case; baseline from 2026-10-05 on scratch
  case                   passed   rate  baseline
  brazil-customers          3/3   100%      100%
  longest-track             3/3   100%      100%
  acdc-albums               2/3    67%      100%
  tracks-per-genre          3/3   100%       33%
  top-customer-country      3/3   100%      100%
  sales-2011                3/3   100%       67%
  top-artists-revenue       1/3    33%        0%
  customer-age              3/3   100%       33%
  overall                 21/24    88%       67%
```

`acdc-albums` is one trial below its baseline, within the rule above. Its failure is
read in `step-6-explained.md`, as are both `top-artists-revenue` failures and a
`customer-age` pass that is not a real decline.

Then record the report as the new baseline. Milestone 7 measures against this step, not
step 5. The step-5 baseline stays in the history and in `step-5-explained.md`.

```bash
cp runs/evals/sql-scratch-<stamp>.json domains/sql/evals/baseline.json
```

The file in the repo is the report of 2026-10-06T17:43:00Z. Put the date, the commit, the
models and where it ran in the commit message: local processes, on top of `482ddac`,
`ollama:llama3.1:8b` for all three roles.

Check that the runner reads it back: `pytest tests/test_evals.py` validates every
domain's baseline.

---

## 6.9 CLAUDE.md — DONE

In "Domain: `sql`":
- **schema_agent**: the model names the tables. Code adds the tables on the shortest
  foreign-key paths between them, takes every column, type and foreign key from the real
  schema, and lists the join conditions between them. An empty selection means the data
  cannot answer.
- **query_agent**: may decline with what the schema lacks, instead of writing SQL.
- **Pipeline**: after `get_schema` and after `generate_sql`, a route to `answer` when the
  step found the data cannot answer (`error_type` `unanswerable`). The route after
  `execute` is unchanged.
- **Guard**: double-quoted strings off (SQLite DQS), so a misspelled quoted column is an
  error, not a string. The executor returns at most 50 rows, whatever `LIMIT` the SQL has.

In "Milestones": mark 6 ✅.

---

## Done when

```bash
pytest                                       # green
ruff check . && ruff format --check .        # clean
pytest -m live tests/test_evals_live.py      # overall >= 67%; top-artists-revenue > 0/3
```

- `domains/sql/evals/baseline.json` is the step-6 report.
- Outside `domains/sql/`, the step touched only `CLAUDE.md` and `docs/`.
- `questions.yaml` is unchanged. The comparison is only fair on the same questions.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AttributeError: 'sqlite3.Connection' object has no attribute 'setconfig'` | Python older than 3.12 | Use the venv's Python, or the image's 3.12 |
| A query with a double-quoted string fails where it used to work | DQS is off (6.1). Before, `"AC/DC"` was silently a string. | Expected. It is classified `syntax` and regenerated, and the query prompt asks for single quotes. |
| Every case ends `declined: the data cannot answer this: …` | The query agent declines answerable questions | Read the `missing` it gave in the run log. A model that declines everything needs its prompt, not a lower bar. |
| `ValidationError` on `GenerateSqlResponse` in the orchestrator | An agent image built before 6.4 answering a newer orchestrator | `make k8s-up` rebuilds and restarts every image |
| The runner shows no baseline column | `baseline.json` failed to load, or was deleted | Restore it from git. It is read before the run starts. |
| Rates differ from the recorded ones | Model output varies (step 5) | Compare over more trials: `--eval-repeats 5` |
