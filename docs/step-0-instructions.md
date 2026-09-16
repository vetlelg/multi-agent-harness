# Step 0 — Instructions

Exact actions only. For the reasoning behind any of it, see `step-0-explained.md`.

**Convention:** file sections give you names, signatures, fields and required behaviour.
They are specifications, not source. You write the bodies.

**State of play:** 0.1–0.5 are done. Start at 0.6.

---

## 0.1 Branch and virtual environment — DONE

```bash
git checkout -b build/step-0
python -m venv .venv
source .venv/Scripts/activate        # Git Bash
# .venv\Scripts\Activate.ps1         # PowerShell
```

---

## 0.2 Requirements files — DONE, needs additions in 0.6

Current state is correct. Additions are listed in 0.6.

---

## 0.3 Install make — DONE

```powershell
choco install make          # elevated PowerShell
```

---

## 0.4 Ignore files — DONE, `.env.example` rewritten in 0.6

---

## 0.5 Makefile `db` target — DONE

Verify it still passes:

```bash
make db
make db     # second run should re-download and re-verify without error
python -c "import sqlite3;print(sqlite3.connect('file:data/chinook.db?mode=ro',uri=True).execute('select count(*) from Customer').fetchone())"
# expect: (59,)
```

---

## 0.6 Provider-agnostic LLM wrapper

### 0.6.1 Add dependencies

Append to `requirements.txt`:

```
ollama==0.6.2
pydantic-settings==2.15.0
```

Append to `requirements-executor.txt`:

```
pydantic-settings==2.15.0
```

Do **not** add `openai` or `google-genai` yet. Their pins, for when you do:

```
openai==3.6.0
google-genai==2.20.0
```

Then:

```bash
pip install -r requirements-dev.txt
```

### 0.6.2 Rewrite `.env.example`

```
# --- provider credentials (leave blank if unused) ---
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
OLLAMA_HOST=http://localhost:11434

# --- model per role, as provider:model ---
SCHEMA_MODEL=anthropic:claude-opus-5
SQL_MODEL=anthropic:claude-opus-5
ANSWER_MODEL=anthropic:claude-opus-5

# --- service addresses ---
EXECUTOR_URL=http://localhost:8000
SCHEMA_AGENT_URL=http://localhost:8010
QUERY_AGENT_URL=http://localhost:8020
ORCHESTRATOR_SCRATCH_URL=http://localhost:8100
ORCHESTRATOR_LG_URL=http://localhost:8200

# --- behaviour ---
RUNS_DIR=runs
```

Copy it to `.env` and fill in one real key.

### 0.6.3 Verify the installed SDK surfaces before writing adapters

```bash
python -c "import anthropic; print([m for m in dir(anthropic.Anthropic().messages) if not m.startswith('_')])"
python -c "import ollama, inspect; print(inspect.signature(ollama.chat))"
```

Confirm `parse` appears in the first list.

### 0.6.4 `core/llm.py`

Exports:

```
class StopReason(str, Enum)         # FINISHED, TRUNCATED, REFUSED, FAILED
class LLMError(RuntimeError)        # base
class LLMTruncated(LLMError)
class LLMRefused(LLMError)
class LLMUnparsable(LLMError)
@dataclass(frozen=True) LLMRequest
@dataclass(frozen=True) LLMResult
class LLMClient(ABC)                # complete(request: LLMRequest) -> LLMResult
class AnthropicAdapter(LLMClient)
class OllamaAdapter(LLMClient)
def get_client(spec: str) -> LLMClient
```

`LLMRequest` and `LLMResult` are frozen dataclasses rather than Pydantic models:
they never cross a process boundary, and `LLMResult.parsed` holds a model instance
that Pydantic would otherwise try to re-validate. `LLMClient` is an ABC rather than
a Protocol so adapters share construction and the stop-reason check.

`LLMRequest` fields:

| Field | Type | Notes |
|---|---|---|
| `system` | `str` | system prompt |
| `user` | `str` | user prompt |
| `output_model` | `type[BaseModel]` | the class you want back |
| `max_tokens` | `int` | from config |
| `extras` | `dict[str, Any]` | passed to the provider untouched, default empty |

`LLMResult` fields:

| Field | Type | Notes |
|---|---|---|
| `parsed` | `BaseModel` | validated instance of `request.output_model` |
| `raw_text` | `str` | the model's text output |
| `provider` | `str` | `anthropic`, `ollama`, … |
| `model` | `str` | model id that actually served it |
| `input_tokens` | `int \| None` | |
| `output_tokens` | `int \| None` | |
| `stop_reason` | `StopReason` | normalised |
| `elapsed_ms` | `int` | measured by the adapter |

`get_client(spec)`:

- Splits `spec` on the **first** colon only. Ollama model names contain colons.
- Returns the adapter for that provider, constructed with that model id.
- Raises a clear error naming the unknown provider.

Adapter mapping table:

| | Anthropic | Ollama |
|---|---|---|
| call | `client.messages.parse(...)` | `ollama.chat(...)` |
| system prompt | top-level `system=` | `{"role": "system"}` message |
| schema param | `output_format=<model class>` | `format=<model>.model_json_schema()` |
| parsed result | `response.parsed_output` | **none** — validate `response.message.content` yourself |
| input tokens | `response.usage.input_tokens` | `prompt_eval_count` |
| output tokens | `response.usage.output_tokens` | `eval_count` |
| stop signal | `response.stop_reason` | `done_reason` |
| host/key | `ANTHROPIC_API_KEY` from env | `OLLAMA_HOST` |

Stop reason mapping:

| Provider value | `StopReason` |
|---|---|
| `end_turn`, `stop` | `FINISHED` |
| `max_tokens`, `length` | `TRUNCATED` |
| `refusal` | `REFUSED` |
| anything else | `FAILED` |

Rules:

- Never send `temperature` to Anthropic. Current models reject it with HTTP 400.
- `TRUNCATED` must raise, not return. A truncated response is not an answer.
- Check `stop_reason` before reading content.
- Anthropic responses may contain a `thinking` block before the `text` block. Iterate
  content blocks and select by type; never index `content[0]`.
- Construct exactly one client object per adapter instance, not one per call.
- `core/llm.py` is the only file in the repo permitted to import a provider SDK.

Verify — write `tests/test_llm_smoke.py`, parametrised over the providers you configured:

```bash
pytest tests/test_llm_smoke.py -v
```

It must assert: a validated instance is returned, `stop_reason == FINISHED`, and
`input_tokens` is not None.

---

## 0.7 `core/config.py`

Exports:

```
class Settings(BaseSettings)
settings: Settings          # module-level singleton, built once at import
```

Fields:

| Field | Default |
|---|---|
| `anthropic_api_key` | `None` — `SecretStr` |
| `openai_api_key` | `None` — `SecretStr` |
| `google_api_key` | `None` — `SecretStr` |
| `llm_timeout_s` | `120.0` — model calls need a longer budget than service calls |
| `schema_model` | `anthropic:claude-opus-5` |
| `sql_model` | `anthropic:claude-opus-5` |
| `answer_model` | `anthropic:claude-opus-5` |
| `max_tokens` | `16000` |
| `max_attempts` | `3` |
| `row_limit` | `50` |
| `query_timeout_s` | `5` |
| `http_timeout_s` | `30` |
| `db_path` | `data/chinook.db` |
| `executor_url` | `http://localhost:8000` |
| `schema_agent_url` | `http://localhost:8010` |
| `query_agent_url` | `http://localhost:8020` |
| `orchestrator_scratch_url` | `http://localhost:8100` |
| `orchestrator_lg_url` | `http://localhost:8200` |
| `runs_dir` | `None` |
| `ollama_host` | `http://localhost:11434` |

Rules:

- Reads `.env` via `pydantic-settings`.
- Built once at import. No `os.getenv` anywhere else in the repo.
- Validates the three `*_model` fields contain a colon and a known provider prefix.
- No logic, no I/O, no clients. This module imports nothing else from the repo.

Verify — `tests/test_config.py`:

- `settings.max_attempts == 3`
- constructing `Settings` with `sql_model="nonsense"` raises a validation error

---

## 0.8 `core/models.py`

All models set `model_config = ConfigDict(extra="forbid")`.

**Public contract**

| Model | Fields |
|---|---|
| `AskRequest` | `question: str`, `run_id: str \| None = None` |
| `AskResponse` | `run_id: str`, `orchestrator: str`, `question: str`, `sql: str \| None`, `columns: list[str] \| None`, `rows: list[list] \| None`, `answer: str`, `attempts: int`, `error: str \| None` |

**Introspection**

| Model | Fields |
|---|---|
| `ColumnInfo` | `name: str`, `type: str`, `nullable: bool`, `pk: bool` |
| `ForeignKeyInfo` | `column: str`, `references_table: str`, `references_column: str` |
| `TableInfo` | `name: str`, `columns: list[ColumnInfo]`, `foreign_keys: list[ForeignKeyInfo]` |
| `SchemaResponse` | `tables: list[TableInfo]` |

**Internal calls**

| Model | Fields |
|---|---|
| `SelectSchemaRequest` | `run_id: str`, `question: str`, `previous_sql: str \| None`, `error: str \| None` |
| `SelectSchemaResponse` | `tables: list[TableInfo]`, `schema_text: str` |
| `GenerateSqlRequest` | `run_id: str`, `question: str`, `schema_text: str`, `previous_sql: str \| None`, `error: str \| None`, `attempt: int` |
| `GenerateSqlResponse` | `sql: str` |
| `ExecuteRequest` | `run_id: str`, `sql: str` |
| `ExecuteResponse` | `ok: bool`, `sql_executed: str \| None`, `columns: list[str] \| None`, `rows: list[list] \| None`, `row_count: int \| None`, `truncated: bool = False`, `elapsed_ms: int`, `error_type: ErrorType \| None`, `error: str \| None` |

**Model output schemas** — handed to `LLMRequest.output_model`. Keep flat.

| Model | Fields |
|---|---|
| `SqlOut` | `sql: str` |
| `SelectedTableOut` | `name: str`, `columns: list[str]` |
| `SelectedSchemaOut` | `tables: list[SelectedTableOut]` |
| `AnswerOut` | `answer: str` |

`SelectedSchemaOut` carries **names only**. Types, nullability and foreign keys are
looked up from the real introspected schema by the schema agent rather than re-emitted
by the model, so they cannot be hallucinated. It also keeps the output schema flat,
which is what every provider handles the same way.

**Shared run state** — used by both orchestrators, and by LangGraph in Step 6.

| Model | Fields |
|---|---|
| `RunState` | `run_id: str`, `question: str`, `schema_text: str \| None`, `sql: str \| None`, `columns: list[str] \| None`, `rows: list[list] \| None`, `error: str \| None`, `error_type: ErrorType \| None`, `attempts: int = 0`, `answer: str \| None` |

Rules:

- No optionals or unions inside the three output schemas.
- This module imports only `core.errors` (for `ErrorType`) and nothing else from the repo.

Verify — `tests/test_models.py`: round-trip each model to JSON and back; feed one
model a misspelled field name and expect a validation error.

---

## 0.9 `core/errors.py`

Exports:

```
class ErrorType(str, Enum)      # MISSING_OBJECT, SYNTAX, GUARD_REJECTED, TIMEOUT, OTHER
class GuardRejection(Exception) # raised by the executor's guard in Step 1
def classify_sql_error(error: Exception | str) -> ErrorType
def is_retryable(error_type: ErrorType) -> bool
```

Classification — match on message **prefix**, case-sensitively:

| Message starts with | Returns |
|---|---|
| `no such table:` | `MISSING_OBJECT` |
| `no such column:` | `MISSING_OBJECT` |
| `no such function:` | `SYNTAX` |
| `near "` | `SYNTAX` |
| `wrong number of arguments to function` | `SYNTAX` |
| `interrupted` | `TIMEOUT` |
| `attempt to write a readonly database` | `OTHER` |
| `You can only execute one statement at a time.` | `OTHER` |
| anything else | `OTHER` |

`is_retryable`: `MISSING_OBJECT` and `SYNTAX` are retryable. `GUARD_REJECTED`,
`TIMEOUT` and `OTHER` are not.

Rules:

- When passed an exception, catch `sqlite3.DatabaseError`, not `OperationalError`.
  The multi-statement case raises `ProgrammingError`, a sibling class.
- `GUARD_REJECTED` is raised by your own guard in Step 1. It never comes from SQLite.

Verify — `tests/test_errors.py`, parametrised over all nine strings above.

---

## 0.10 `core/events.py`

Exports:

```
class Event(BaseModel)              # base
class RunStart(Event) ...           # one class per event type
AnyEvent                            # discriminated union of all six
def emit(event: Event) -> None
def parse_event(line: str) -> AnyEvent
def new_run_id() -> str
```

Base fields on every event:

| Field | Type |
|---|---|
| `v` | `int` — schema version, `1` |
| `ts` | `str` — UTC ISO-8601 ending in `Z` |
| `run_id` | `str` |
| `service` | `str` |
| `type` | `Literal[...]` — the discriminator |
| `seq` | `int \| None` — set by orchestrators only |

Event types and their extra fields:

| Type | Extra fields |
|---|---|
| `run_start` | `question`, `orchestrator` |
| `http_call` | `target`, `method`, `path`, `status`, `elapsed_ms` |
| `model_call` | `role`, `provider`, `model`, `prompt`, `response`, `input_tokens`, `output_tokens`, `stop_reason`, `elapsed_ms` |
| `sql_execute` | `sql`, `ok`, `row_count`, `error_type`, `error`, `elapsed_ms` |
| `route_decision` | `attempt`, `error_type`, `branch` |
| `run_end` | `attempts`, `ok`, `answer`, `error` |

`emit(event)` does exactly three things:

1. Validate the event.
2. Print one line of JSON to stdout.
3. If `settings.runs_dir` is set, append the same line to `<runs_dir>/<run_id>.jsonl`.

Rules:

- Append only. Never rewrite or delete a run file.
- Only orchestrators set `seq`, starting at 1 and incrementing per run. Agents leave it `None`.
- No credential may appear in any event field.
- `new_run_id()` returns a UUID4 string.

Verify — `tests/test_events.py`: emit one of every type to a temp `runs_dir`, read the
file back, and validate each line into the correct event class.

---

## 0.11 Prompts, lint, tests

### Prompt files

`core/prompts/schema_select.txt` — inputs are the full schema and the question.
Must instruct: return only relevant tables and columns with types and foreign keys;
return an empty list if nothing is relevant rather than guessing.

`core/prompts/generate_sql.txt` — inputs are the question and the selected schema.
Must instruct: one SQLite `SELECT` (or `WITH … SELECT`), read-only, single statement.
On retry the prompt also carries the previous SQL and the error it produced.

`core/prompts/answer.txt` — inputs are the question, the SQL and the rows.
Must instruct: use only the returned rows; if rows are empty or the run failed, say so
plainly and invent nothing.

### Ruff

Create `pyproject.toml` with a `[tool.ruff]` section: `line-length = 100`,
`target-version = "py312"`.

```bash
ruff check .
ruff format --check .
```

### Optional but recommended

Add `.gitattributes` containing `* text=auto eol=lf`. Your files are currently CRLF,
which is harmless now but bites when the Makefile is used from a Linux container in Step 2.

---

## Done when

```bash
make db                                          # verifies, exits 0
pytest                                           # 5 test files green
ruff check .                                     # clean
git status                                       # no .env, no data/, no .venv/
```

- `pytest tests/test_llm_smoke.py` passes against both adapters
- `docs/`, `core/config.py`, `core/models.py`, `core/errors.py`, `core/events.py`,
  `core/llm.py` and `core/prompts/*.txt` all exist
