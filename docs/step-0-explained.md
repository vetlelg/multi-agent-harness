# Step 0 — What it's for

Companion to `step-0-instructions.md`. That file says what to do. This one says why.

---

## What Step 0 achieves

Nothing runs at the end of Step 0, and that is the point.

You are doing two things. First, getting a **verified** copy of the database onto the
machine. Second, writing down the shared vocabulary — the message shapes, the constants,
the error names, the log format — that all five programs will speak.

The reason to do this before any service code exists: you are building the coordinator
twice, by hand and with LangGraph, and the two versions have to be interchangeable.
If you invent the message shapes while writing the first coordinator, the second one
will quietly disagree with it. You will then spend a day working out why version A
answers a question and version B does not.

Decide once, up front, in one place. Roughly half a day.

---

## 0.1 Branch and virtual environment

**Why a branch per step.** Each step should be revertable on its own. If Step 3 goes
badly you want to return to a working Step 2 without unpicking commits.

**Why a virtual environment.** Five containers will each install a different subset of
these packages. If your laptop has everything installed globally, an import that works
locally will fail inside a container and you won't find out until Step 2.

---

## 0.2 Four requirements files, not one

This is the first place where a rule in `CLAUDE.md` becomes a fact about the built
artefact rather than a promise you have to remember.

| File | Enforces |
|---|---|
| `requirements-executor.txt` | The executor has no AI in it. There is no model SDK in its image, so it cannot make a model call even by accident. |
| `requirements-lg.txt` | LangGraph exists in exactly one service. The hand-built coordinator cannot import it, because it isn't installed there. |

The second one matters more than it looks. `langgraph` depends on `langchain-core`, so
"no LangChain in the hand-built version" can never mean "never installed anywhere" — it
will always be present in the LangGraph image. Splitting the requirements is what makes
the rule true where it counts, and a test in Step 6 makes it true in the source.

---

## 0.3 make

`CLAUDE.md` commits to `make db` and to Makefile targets in `infra/`. Rather than rewrite
the spec to match a machine that lacked `make`, installing it keeps the document honest.

Two Windows hazards are worth knowing even though you're past this step. Recipe lines
need real tab characters, and most editors silently insert spaces. And make picks its own
shell — on Windows it uses `cmd.exe` unless it finds `sh.exe` on the PATH, which is why
you run it from Git Bash.

---

## 0.4 Ignore files

**Why the database is ignored rather than committed.** It gets baked into a container
image and every test result depends on its contents. Committing a binary blob puts it in
git history permanently. Downloading it from a pinned URL with a checksum means there is
exactly one authoritative copy, and any deviation from it is caught immediately.

**Why `.env` is ignored and `.env.example` is not.** The example file documents which
variables exist without ever holding a secret. New contributors copy it. The real file
never leaves the machine.

---

## 0.5 The `db` target

**Why verify the checksum.** A download that fails halfway produces a file that looks
present and parses as a database, just with fewer rows. That surfaces three steps later
as mysteriously wrong answers, and you will blame the model.

**Why download to a temporary name first.** If you write straight to `data/chinook.db`
and the transfer dies, you have replaced a good file with a corrupt one. Downloading,
verifying, then moving means a failed run leaves the previous good copy untouched.

Your Makefile checks both the SHA-256 and the byte count. The size check is redundant
given the hash, but it fails faster and with a clearer message on a truncated download.

---

## 0.6 The provider seam

### Why this exists at all

You asked to be able to run against Claude, GPT, Gemini or a local model. Without a seam
that means provider-specific code scattered through both agents and both coordinators,
and switching provider becomes a refactor.

There is a second reason that matters more for this particular repo. Your event log has
to stay provider-neutral. If Gemini's token counts land under different field names than
Claude's, the observability agent you're planning breaks the first time you switch. The
adapter isn't only about calling the model — it's about producing one normalised record
regardless of who answered.

### Why the seam is genuinely necessary

All four SDKs do the same thing: take a Pydantic class, return a validated instance.
They disagree on every single name involved.

| Provider | What you pass | Where the result lands |
|---|---|---|
| Anthropic | `output_format=` | `response.parsed_output` |
| OpenAI | `text_format=` | `response.output_parsed` |
| Gemini | `response_schema=` | `response.parsed` |
| Ollama | `format=` | nothing — you validate the text yourself |

Anthropic and OpenAI use the same two words in opposite orders, in both the parameter
and the result. That table is the entire argument for an adapter layer.

### The five differences the adapters hide

1. **System prompt placement.** A top-level argument, a message in a list, or a config
   field, depending on provider.
2. **Structured output**, per the table above. Ollama constrains the generation to your
   schema but hands back a plain string, so your adapter does the validation the other
   three do for you.
3. **Token count field names.** Every provider chose different words for the same two
   numbers.
4. **Stop reason vocabulary.** Normalising these to four of your own values is what lets
   one piece of code decide "this response is unusable" regardless of provider.
5. **Which parameters are legal.** Current Claude models reject `temperature` outright.
   The adapter drops unsupported parameters rather than passing everything through.

### Why truncation must raise rather than return

A response cut off at the token limit is still a valid HTTP 200 with text in it. If your
wrapper returns it, the SQL agent executes half a statement and you get a syntax error
that looks like a model mistake. Treating `TRUNCATED` as a failure means the real cause
appears in the log.

### Why two adapters now, not four

The provider layer is not what this repo is studying. Anthropic plus Ollama covers the
interesting axis — one cloud, one local — and adding the other two later is an afternoon
because the seam already exists. Building all four on day one is work you cannot yet
justify.

`litellm` and `instructor` both offer this abstraction off the shelf. Skipping them is a
deliberate choice: you still need your own normalisation layer for the event log either
way, and writing two thin adapters teaches you the differences that matter. If the
provider layer ever starts eating time, reach for one of them.

### Why a local model is worth having

Two concrete payoffs. The 40-case test suite in Step 8 costs real money against a hosted
model and nothing against Ollama, so you can iterate freely and spend only when you want
real numbers. And small local models are noticeably worse at SQL, which means your retry
loop finally has something to do — Step 5 becomes easier to demonstrate, not harder.

### Why one model setting per role

Three settings rather than one global provider costs nothing now and buys you the
question this harness exists to answer: does a cheap local model do schema selection just
as well as an expensive hosted one? That's an experiment you can run in Step 8 by
changing one line of configuration.

---

## 0.7 `core/config.py`

**Why one file.** `CLAUDE.md` requires model name and parameters to be set in one place.
Extending that to every constant means the retry budget, the row cap and the query
timeout are also single-sourced.

**Why `MAX_ATTEMPTS` in particular.** The original spec contradicted itself — the
hand-built section said "max 3 retries" while the LangGraph section said "attempts < 3",
which differ by one execution. Both coordinators reading one constant makes the
contradiction impossible to reintroduce.

**Why settings are built once at import.** The alternative is calling `os.getenv` where
you need it. The failure mode is a test that sets an environment variable and leaks into
the next one, producing failures that depend on the order tests happen to run in. Those
take hours to diagnose because the failing test is not the broken one.

**Why config imports nothing.** It sits at the bottom of the dependency graph. Everything
imports it. If it imports upward you get circular imports the first time you add a type.

---

## 0.8 `core/models.py`

**Why every inter-service message is a model.** These shapes are the contract that makes
the two coordinators interchangeable. Written as models, a mismatch is a validation error
at the boundary. Written as dictionaries, a mismatch is a `None` that travels two hops
before causing a confusing failure somewhere unrelated.

**Why unknown fields are rejected.** If a service sends `sql_statement` where the contract
says `sql`, the default behaviour is to ignore the unknown key and leave the expected one
empty. You want that to be loud.

**Why `RunState` lives here.** Both coordinators use the same state class, and LangGraph
accepts a Pydantic model directly as its graph state. That turns "the two versions behave
identically" from something you assert into something the type system holds. It is also
the answer to your Pydantic question — Pydantic is a validation library and LangGraph is
an orchestration framework. They are different layers, and LangGraph depends on Pydantic.
The thing you were thinking of is PydanticAI, which is a different package and a genuine
LangGraph competitor.

**Why the model output schemas must stay flat.** Optionals, unions and nested structures
are exactly where the four providers' schema support diverges. Your two real output
shapes are a string and a list of tables, so this costs you nothing — but it will if you
let those classes grow.

---

## 0.9 `core/errors.py`

**Why this is the most important file in Step 0.** It decides where a retry goes. If each
coordinator had its own copy of that logic they could route differently, and no test in
the suite would notice — both would still return correct answers, just via different
paths. One shared function means routing cannot diverge because there is only one place
to change it.

**Why the error strings are real.** Every message in the table was produced by running a
failing query against your actual `chinook.db`. Classifiers written from memory match
messages that don't exist.

Three things that would otherwise have bitten you:

- **The multi-statement case raises a different exception class.** `ProgrammingError`, not
  `OperationalError`. Catching only the latter lets it escape as an unhandled crash.
- **`no such column` does not include the table name** unless the query itself qualified
  it. Any parser expecting a `Table.Column` shape fails on the common case.
- **`no such function` is a hallucination, not a schema problem.** The model invented a
  SQLite function. The schema was fine, so it goes back to SQL regeneration rather than
  schema re-selection.

**Why `GUARD_REJECTED` and `TIMEOUT` are not retryable.** If your own guard refused the
SQL, regenerating rarely helps and you want to notice the guard firing rather than paper
over it. A timeout means the query was too expensive; the same query will time out again.

---

## 0.10 `core/events.py`

**Why events rather than log lines.** The hand-built coordinator eventually becomes the
event-sourced one. If the log is free-form text now, that is a rewrite later. If each
record is a typed, numbered, validated event now, it is a refactor.

To be explicit about the boundary: this is a decision about the **shape of the log**, not
the deferred feature. No event store, no projections, no replay code, no
`orchestrator_es/`. Your deferral holds.

**Why the coordinators number events and the agents don't.** A single run crosses four
processes, so they cannot share a counter. Rather than invent a distributed one, split the
two purposes: the coordinator's stream is the ordered log of record, the thing you will
one day replay. The agents' output carries the same run id so you can correlate it, but
makes no claim to be replayable. That keeps the event-sourcing story honest.

**Why `route_decision` exists.** It records the error that came in and the branch that went
out. It is what turns Step 5 from "the retry seems to work" into a line you can read out
of a file. Without it, the most interesting behaviour in the system is invisible.

**Why append-only.** An event log you can edit is not evidence. It also means the
observability agent can tail the file rather than re-read it.

**Why credentials must never appear.** You are logging full prompts by design, which is
right — it is what makes runs reproducible. It also means anything that reaches a prompt
is on disk permanently.

---

## 0.11 Prompts and tests

**Why the prompts stay short.** You will tune them from real failures in Step 4, using
evidence from the run logs. Elaborate prompts written today are guesses, and guesses are
harder to improve than a plain baseline because you cannot tell which part was helping.

**Why these three tests specifically.** They are the ones that verify the shared vocabulary
actually works: errors classify correctly against real strings, messages survive a
round-trip, and the event log can be read back into typed objects. That last one proves
the premise the observability agent depends on.

---

## Two findings that shaped decisions here

**`temperature: 0` is impossible on current Claude models.** Sampling parameters were
removed and sending one returns HTTP 400. `CLAUDE.md` still specifies temperature 0, and
that line needs updating. The determinism knob on Claude is now an effort level. On
OpenAI, Gemini and Ollama temperature still works — which is one more reason the
multi-provider seam is worth having.

**Assistant prefill no longer works.** The standard text-to-SQL trick — prefill the reply
with `SELECT` so the model can only continue SQL — is rejected on current models.
Structured outputs replace it, and are better: no fence stripping, no preamble to trim.

**Opening the database read-only is not the security boundary.** This one is for Step 1,
but it was discovered here. A `mode=ro` connection can still run `ATTACH DATABASE`, and a
test successfully created a new file and wrote a row to it from a supposedly read-only
connection. Read-only protects the *main* database from writes; it does not stop the
connection opening a second one it can write to freely. The layer that actually holds is
SQLite's authorizer callback, which returns "not authorized" for `ATTACH`. Note that a
strict authorizer also blocks `PRAGMA`, which schema introspection needs — so
introspection will use a separate connection without one.
