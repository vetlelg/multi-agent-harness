# Step 1 — What it's for

Companion to `step-1-instructions.md`. That file says what to do. This one says why.

---

## What Step 1 achieves

A single-table question goes in at the CLI and a correct answer comes out, with every
intermediate step — schema selection, SQL generation, execution, answer — visible in the
run log. Four processes talk over HTTP on localhost, mirroring the container topology they
will have in Step 2.

This is the first time the vocabulary from Step 0 carries traffic. Every model shape,
error classification, and event type you defined gets exercised for real. If any of them
are wrong, you find out now, while the system is four Python processes and a print
statement, not four containers and `kubectl`.

---

## 1.1 Executor: schema introspection

**Why the executor serves the schema rather than each agent reading the database.**
The executor is the only service with database access. If agents could read the schema
directly, they could also read data — the access boundary would be a convention rather
than a fact. A single entry point makes the boundary enforceable.

**Why PRAGMA for introspection.** SQLite's `PRAGMA table_info`, `PRAGMA foreign_key_list`,
and `sqlite_master` are the authoritative source for schema metadata. ORM introspection
layers wrap these same calls and hide details you care about (nullable, primary key,
foreign key targets). Going direct means fewer dependencies and complete information.

**Why a separate connection.** Step 0 discovered that the SQLite authorizer callback — the
real security boundary — blocks PRAGMA. Introspection needs PRAGMA. So you use two
connections: one without the authorizer for introspection (read-only mode is sufficient
here since you control the queries), and one with the authorizer for executing
user-generated SQL.

---

## 1.2 Executor: the guard

**Why defense in depth.** The guard has three layers, each catching what the previous one
might miss:

1. **Pre-execution text check.** Rejects multi-statement input, verifies the statement is
   `SELECT` or `WITH … SELECT`, and scans for forbidden keywords. This catches the obvious
   cases and gives clear error messages.

2. **SQLite authorizer callback.** A runtime hook that SQLite calls before executing any
   operation. It rejects ATTACH, PRAGMA, and write operations at the engine level,
   regardless of how cleverly the SQL is constructed. This is the layer that actually
   holds — it cannot be bypassed by SQL tricks because SQLite itself enforces it.

3. **LIMIT injection.** Appends `LIMIT 50` if the query doesn't already have one. This
   prevents a valid SELECT from returning a million rows.

**Why the authorizer is the real boundary, not the text check.** SQL parsing is hard.
A regex or keyword scan catches `DROP TABLE` but misses `DROP/**/TABLE`, string escaping
tricks, and constructs not yet invented. The authorizer operates on the parsed query plan,
after SQLite has resolved all of that. The text check exists for user-facing error
messages, not for security.

**Why ATTACH is specifically dangerous.** Step 0 discovered this: a `mode=ro` connection
can still run `ATTACH DATABASE`, which opens a new writable database file. The authorizer
is what blocks this — it intercepts the ATTACH operation at the engine level.

**Why timeout uses `set_progress_handler`.** SQLite calls a progress handler every N
virtual machine operations during query execution. Returning non-zero from it interrupts
the query, which raises an `OperationalError` with "interrupted". This frees the
connection immediately rather than holding it until an HTTP timeout fires externally.

**Why the executor classifies errors.** Both orchestrators need the same error
classification to route retries identically. Classifying at the source means the
orchestrators receive `error_type` as a field on the response and neither one implements
classification logic. One shared function (`classify_sql_error` from Step 0), called in
one place.

---

## 1.3 Executor: query execution

**Why `POST /execute` returns both the result and the error classification.** The
orchestrator needs three things after an execution: success or failure, the data if it
succeeded, and the error type if it failed. Packing all three into `ExecuteResponse`
(designed in Step 0) means one HTTP round trip per execution, and the orchestrator never
touches `classify_sql_error` itself.

**Why `sql_executed` is returned alongside the original SQL.** The guard may have appended
`LIMIT 50`. Returning the actually-executed SQL is what lets the answer prompt say "this
query was run" and shows the user what really happened, not what the model asked for.

---

## 1.4 Schema agent

**Why the model returns names only.** The output schema (`SelectedSchemaOut`) carries
table and column names but no types, no nullability, no foreign keys. Those are looked up
from the real introspected schema after the model responds. This means the model cannot
hallucinate types — it can only select from what actually exists. A model that returns
`INTEGER` for a `TEXT` column, or invents a foreign key that doesn't exist, would send the
query agent down the wrong path.

**Why `schema_text` is rendered here and passed onward.** The query agent receives a text
representation of the selected schema, not the structured objects. Rendering it once, in
the schema agent, means:
- Both orchestrators send the same text to the query agent.
- The text in the run log is exactly what the model saw.
- The rendering logic lives next to the schema logic, not scattered across orchestrators.

**Why the schema agent takes `previous_sql` and `error` on retry.** When a "no such
table/column" error triggers schema re-selection, the model needs to know what went wrong.
Seeing the failed SQL and the error lets it make a different selection — typically a wider
one that includes the missing table or a corrected column name.

---

## 1.5 Query agent

**Why it's a separate service rather than a function in the orchestrator.** In Step 2 it
becomes a separate container. Starting with HTTP boundaries means local dev and cluster
behave identically — you never discover at deployment time that a function call was
relying on shared memory or that a Pydantic model you passed by reference isn't
serialisable.

**Why the prompt carries the attempt number.** The model can see whether this is a first
try or a retry, and adjust its approach. A first attempt should be straightforward; a
retry after a syntax error might try a simpler construction.

---

## 1.6 Orchestrator scratch

**Why the pipeline is schema → query → execute, not schema+query → execute.** The schema
agent selects relevant tables; the query agent writes SQL against that selection; the
executor runs it. Splitting schema selection from SQL generation means each model call has
a focused job and a flat output schema. It also means retries can target the right stage:
a schema miss goes back to selection, a syntax error goes back to generation.

**Why HTTP calls to the agents, not function imports.** Same reason as the agents: the
production topology is four containers. HTTP from day one means the retry loop, the error
handling, and the logging are all exercised against the same boundaries they'll face
in-cluster. It also means the LangGraph orchestrator in Step 7 calls the same agents at
the same addresses — no second implementation path to keep in sync.

**Why the answer is a separate LLM call.** The model that wrote the SQL shouldn't also
interpret the results. Different tasks, different prompts, potentially different models
(configurable via `answer_model` in config). The answer prompt can focus entirely on
translating rows into natural language without also carrying the SQL generation context.

**Why retry routing has two branches, not one.** A "no such table/column" error
(`MISSING_OBJECT`) means the schema was wrong — the model selected tables that don't
exist, or missed a table it needed. Fix: re-select the schema. A syntax error (`SYNTAX`)
means the SQL was malformed but the schema was probably fine. Fix: regenerate just the
SQL, with the error as context. Sending everything back to schema selection on every error
wastes a model call and can change the schema when it was already correct.

**Why `run_id` flows through everything.** Every HTTP call carries it, every event includes
it, every log line prints it. When something goes wrong in a four-service system, the
`run_id` is how you find the relevant log lines across all four processes. This is also the
anchor for distributed tracing added in a later step.

**Why the orchestrator owns event sequencing.** Agents emit events with the `run_id` for
correlation but leave `seq` as `None`. The orchestrator numbers its events sequentially,
starting at 1 per run. This makes the orchestrator's event stream the ordered log of
record — the one you could, in the future, replay to reconstruct a run.

---

## 1.7 CLI

**Why it's a thin client.** The CLI does one thing: POST the question to the orchestrator's
`/ask` endpoint and print the response. No retry logic, no result formatting beyond what
the orchestrator returns, no direct model calls. This means the same CLI works against
both orchestrators, against a local process, and against an in-cluster service via
port-forward — all without change.

---

## 1.8 Running locally

**Why four separate processes.** Each service runs in its own terminal, on its own port,
with its own stdout. This mirrors the container topology and lets you see each service's
log output independently. It also makes it obvious when a service crashes — its terminal
shows the traceback, rather than a multi-service process silently losing one component.

**Why not Docker Compose yet.** Step 2 adds containers. Running bare processes first means
you can iterate on the code with instant feedback — no image builds, no layer caching, no
"did my change make it into the container?" ambiguity. You also get Python tracebacks
directly, not behind container log formatting.

---

## What to verify

The end-to-end check is a single-table question: "How many customers are from Brazil?"
This hits every stage of the pipeline — schema selection, SQL generation, execution,
answer — without requiring joins or aggregation, which are Step 4's concern.

The run log should show: `run_start`, HTTP calls to executor and agents, model calls for
schema selection, SQL generation, and answer, and `run_end`. If any of these are missing,
the event logging from Step 0 isn't wired correctly. The run file in `runs/` should be
parseable by `parse_event` on every line — that's what `test_events.py` proved in
isolation, and now you confirm it holds under real traffic.
