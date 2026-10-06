# Step 5 — What it's for

Companion to `step-5-instructions.md`. That file says what to do. This one says why.

---

## What Step 5 achieves

Until now, "it works" meant one question answered correctly once, on one run. That shows
the plumbing is connected, and nothing more. A model answers the same question
differently on the next run, and a question it has never seen differently again. Step 5
turns "it works" into a number: a fixed set of questions, each asked several times, scored
mechanically. The output is a pass rate per question and overall.

The number on its own matters less than when it is taken. Milestone 6 is where the system
gets changed to handle joins and aggregations: prompts, perhaps the pipeline, perhaps the
executor. Each of those changes needs a before and an after. This step records the
before, with nothing changed: no prompt edits, no fixes, every service behaving exactly as
step 4 left it. A baseline taken after the first fix is not a baseline.

The step adds no service and changes none. Everything new is either harness tooling that
any domain gets for free, or data that only the sql domain knows.

---

## Where each file lives, and why there

| File | Owner | Why it belongs there |
|---|---|---|
| `core/evals.py` | harness | Scores evidence kinds, and those are harness types. Matching rows works the same for SQL and PromQL. |
| `tests/test_evals.py` | harness | Tests the scorer, and checks every domain's eval set |
| `tests/conftest.py`, `tests/test_evals_live.py` | harness | The runner: finds every domain's eval set, asks through `/ask`, which is identical for all |
| `domains/sql/evals/questions.yaml` | domain | Only sql knows its questions and the rows that answer them |
| `domains/sql/evals/baseline.json` | domain | A measurement of this domain |
| `domains/sql/tests/test_evals.py` | domain | Runs the `reference` queries, which are SQL |

That is the acceptance criterion from CLAUDE.md applied to evaluation. A docs domain adds
`domains/docs/evals/questions.yaml`, and the runner finds it, the default `pytest` run
validates it, and `pytest -m live` scores it. No harness file changes. One caveat: today
the harness scores only `query` evidence. A domain whose answers rest on citations needs
a citation scorer, written once in `core/evals.py`, for every domain. That is the same rule
as for evidence kinds themselves: a genuinely new kind is a harness change.

---

## 5.1 Scoring

**Why score the evidence, not the answer.** The `/ask` contract returns an answer *with*
its evidence, and for sql the evidence is the table the query returned. Comparing tables
is exact, cheap and needs no model. Judging prose needs a judge: another model call, with
its own variance and its own prompt to get right. That is milestone 12's
"LLM-as-judge where needed". For sql it isn't needed. The answer step only turns rows into
a sentence. If the rows are wrong, the answer is wrong or invented. If the rows are right,
the answer is very likely right. The gap this leaves is an answer that garbles correct
rows. It is real, and it is milestone 12's to close, not this one's.

**Why the scorer is the harness's.** Evidence kinds are presentation types: "a tabular
query result looks the same whether the query was SQL or PromQL" (`core/models.py`). So
"does this table hold these rows?" is written once. The deferred ops domain's PromQL
results will be scored by the same function. A domain supplies data (questions, expected
rows), never scoring code.

**What counts as the same result.** Exact match on the raw table would score how the
model phrases SQL, not whether it got the data right. So some differences are ignored,
each because it carries no information:
- **Column names.** The model picks aliases: `COUNT(*)`, `customer_count`, `T1.Name`.
- **Column order.** `SELECT Name, COUNT(*)` and `SELECT COUNT(*), Name` are the same
  table.
- **Extra columns.** "Which country has the most customers?" answered with
  `Country, COUNT(*)` is a correct answer that shows its working. In every top-5 attempt
  in the trial runs, the model returned the revenue as a second column. Requiring exactly
  one column would score that habit, not the ranking. The cost: `SELECT *` passes a
  one-column question when the column is among them. That's acceptable, because the rows
  are still right.
- **Row order**, unless the question is a ranking. SQL without `ORDER BY` has no order. For
  "top 5 by revenue", the order is part of the answer: `ordered: true`.
- **Float noise.** `SUM` over `REAL` gives `105.92999999999999`. Models often add
  `ROUND(x, 2)`. The amounts are currency, so numbers are compared at two decimals.

Some differences are never ignored:
- **The number of rows.** "Which country?" answered with all 24 countries, ranked, is a
  different answer.
- **Values.** Strings match exactly.
- **A missing column.** Genre counts without the genre names don't answer "in each genre".
- **Duplicates.** Rows are compared as multisets, not sets.

This is close to what text-to-SQL benchmarks call *execution accuracy*. Spider's test-suite
evaluation also ignores column order, and ignores row order unless the reference query
sorts. It requires the same number of columns. Allowing extra columns is this harness's own
choice, for the reason above.

**Why the column search is cheap enough.** The scorer tries every way of choosing the
expected columns from the actual ones. That is `n!/(n-k)!` choices for `n` actual and `k`
expected columns, and each check is at most 50 rows, because the executor's guard caps
every result at `LIMIT 50`. Even `SELECT *` over a join is a few thousand cheap
comparisons.

**Why "unanswerable" means `error` set.** The `/ask` contract already has a field for
"could not answer": `error`, with HTTP 200. CLAUDE.md requires "if the data cannot answer
the question, say so — never invent data", and `error` is the machine-readable way of
saying so. A run that ends without `error` has claimed that its query answered the
question, whatever its prose says. If the prose says "no results found" next to a query
that computed something, that is exactly the confusion the contract separates. Scoring the
prose would need a judge again. Scoring `error` is mechanical, and it tells the eval what
the system *claimed*.

**Why rates are properties, not fields.** The report stores trials: what happened. A rate
stored next to them could be edited, or left stale by a later change to the report, and
then disagree with them. Computed on read, it can't.

**Why the scorer has its own test table.** The scorer is the ruler. If it is wrong, every
eval result is wrong, and no eval run can show it. Each row of the table in 5.2 is one
decision from the list above, pinned.

---

## 5.3 The runner

**Why a pytest test.** CLAUDE.md says `pytest -m live` runs the evals. Milestone 11 extends
that to both in-cluster orchestrators. Pytest already gives the runner what it needs:
- the `live` marker, which keeps evals out of the default run;
- parametrization over domain × target;
- skips with a reason;
- `-k` to pick one domain or one target.

**Why each question is asked several times.** Model output varies from run to run. The
Ollama adapter sends no temperature, so Ollama samples at its default. Current Claude
models accept no sampling parameters at all. The same question gets different SQL on
different runs, and the trial runs showed it (see the baseline below). One trial per
question measures luck. With three, each question gets a rate of 0, 33, 67 or 100%. That
is coarse, but it separates "always", "sometimes" and "never", and that is what the next
milestone needs to know. `--eval-repeats` trades time for resolution.

**Why the pass rate is reported, not asserted.** With 8 questions × 3 trials, the overall
rate has a 95% interval of roughly ±19 points around the baseline's 67%. Trials of the
same question are correlated, so the true uncertainty is wider still. The recheck right
after the baseline showed it. With one trial per case and nothing changed, `customer-age`
went from 1/3 to 1/1, and `tracks-per-genre` from 1/3 to 0/1. A threshold would be either
too low to catch anything or high enough to fail on noise. So the runner prints the rates
next to the baseline's, and a person reads the difference. A gate becomes worthwhile once
there are more questions (milestone 11's 20) and more trials. Until then, the test fails
only when the run itself breaks.

**Why a 5xx is a failed trial, but a refused connection stops the run.** An HTTP 500 means
the system broke on this question: for example, an 8B model ran past the token cap, or
returned JSON that didn't validate. The baseline had none. A user asking that question
gets nothing, so it counts against the pass rate. A refused connection means there is no
system to measure: the port-forward died, or nothing was deployed. Counting that as
failures would record 0% for a dead tunnel.

**Why the runner chooses the `run_id`.** For the same reason the CLI does: a run can be
found even if its request failed. Every failed trial in the table carries its
`run_id`. In the cluster, `kubectl logs` piped through `grep <run_id>` shows that run's
events from all four pods. Locally, `runs/<run_id>.jsonl` holds them.

**Why both targets, and why skip the one that isn't up.** Milestones 9 and 11 require both
engines to pass the same eval set. The runner is already parametrized over `lg`, so the
LangGraph engine is evaluated as soon as something listens on its URL, with no change to
the runner. Until then it is skipped with the reason printed: the runner measures what is
deployed.

**Why the response's domain is checked.** The URL comes from the domain's settings, but
whatever answers on that port answers. If another domain's orchestrator were forwarded
there, every case would fail with a plausible reason, and the report would show a
collapse. Checking `domain` turns that into one clear failure.

**Why the report is a file, and the baseline a copy of one.** The report holds every
trial: `run_id`, reason, the query, the answer. A failure can be read from it without
opening the logs. Recording a baseline is a decision, so it is a deliberate act: copy one
report, commit it. A runner that wrote the baseline itself would make the baseline
"whatever ran last".

**Why the report says nothing about models or prompts.** The runner is a client of `/ask`,
and the `/ask` response doesn't say which models or prompt versions produced it. The run
log does: every `ModelCall` carries provider, model, prompt id and prompt sha. Anything
the runner wrote about them would be read from the local working tree, which is not
necessarily what the cluster runs. The `run_id`s are the exact link. The commit message
carries the configuration in words.

**Why `--eval-repeats` is a pytest option, not a setting.** It configures one test run, not
a service. Settings are what services read from their environment. A command-line option
is visible in the command that produced the report.

---

## 5.4 The questions

Eight, because milestone 5 asks for about eight. That is enough to cover the shapes of
question a SQL agent meets, and small enough for one baseline run to take about twelve
minutes on CPU Ollama. Milestone 11 grows the set to 20.

| Case | What it tests | What the baseline showed |
|---|---|---|
| `brazil-customers` | The milestone-1 question: one table, filter, count | 3/3. It is the floor. |
| `longest-track` | Order and limit | 3/3 |
| `acdc-albums` | A join, filtered on the joined table | 3/3 |
| `tracks-per-genre` | Join plus `GROUP BY`, 25 rows | 1/3. Twice it grouped by `GenreId` and never joined `Genre`: right counts, no names. |
| `top-customer-country` | Aggregation and ranking, one row | 3/3 |
| `sales-2011` | Dates stored as text | 2/3. Once `SELECT Total` without `SUM` (see the baseline). Chinook has no invoice on 31 December, so the classic `BETWEEN … '2011-12-31'` bug (it misses `'2011-12-31 00:00:00'`) can't show up in this case. |
| `top-artists-revenue` | Milestone 6's question: four tables, aggregated, ranked | 0/3, three different ways (see the baseline) |
| `customer-age` | Unanswerable: `Customer` has no birth date | 1/3. Twice, after two failed attempts, the run computed employee ages instead (see the baseline). |

**Why each case carries its `reference`, and a test runs it.** An eval set's expected
values are the one thing in an eval that usually goes untested. A hand-copied 1279 instead
of 1297 makes a case impossible to pass, and the system gets the blame. So each answerable
case records the query its rows came from, and `domains/sql/tests/test_evals.py` runs it
on the real database and requires the rows and the column count to match exactly. The
expected rows were generated from the database, not typed, and the test keeps them that
way. The harness never runs `reference`. It is in the domain's query language, and only
the domain knows how to execute that.

**Why the strings are quoted.** PyYAML implements YAML 1.1, where `yes`, `no`, `on`, `off`
and `null` are not strings when unquoted (the "Norway problem"). None of today's values
trips it, but the next question might. The reference test would catch it. Quoting means
it never happens.

**Why `customer-age` in particular.** A question the data cannot answer is only a useful
test if answering it is tempting. Chinook has birth dates, but only for employees. A
schema-selection step that grabs `BirthDate` from the wrong table, or a query agent that
assumes a column exists, will produce *a* number, or a NULL that looks like a result. The
right behaviour is to end with `error` set.

---

## The baseline

Recorded on 2026-10-05 at 01:36 UTC. It ran against the k3d deployment, rebuilt from this
branch (on top of commit `d43a956`) with `make k8s-up`, through the scratch engine. All
three roles used `ollama:llama3.1:8b` on CPU. The run log's `ModelCall` events show the
prompt versions, the same shas as the working tree: `schema_select.txt` `49c8c9179228`,
`generate_sql.txt` `97b361940b3b`, `answer.txt` `3ebca9373695`. Three trials per case,
12 minutes 23 seconds in all.

| Case | Passed | Rate |
|---|---|---|
| `brazil-customers` | 3/3 | 100% |
| `longest-track` | 3/3 | 100% |
| `acdc-albums` | 3/3 | 100% |
| `tracks-per-genre` | 1/3 | 33% |
| `top-customer-country` | 3/3 | 100% |
| `sales-2011` | 2/3 | 67% |
| `top-artists-revenue` | 0/3 | 0% |
| `customer-age` | 1/3 | 33% |
| **overall** | **16/24** | **67%** |

One table and a single join are solid. Grouping, ranking across several tables, and
knowing when to stop are not. Every failure was read from the report's `query` and
`answer`. Three trials are worse than a wrong number, because the run reported something
the data never said. Two gave customer ages computed from employees, and one gave a total
that no query computed.

**Retries can turn an honest failure into an invention.** This is the most important
finding. `customer-age` is the only case that retried at all, and all three of its trials
used all three attempts. Their logs (`kubectl logs`, grepped by `run_id`) show this:
- **Through `get_schema`.** The query agent wrote `SELECT AVG(age) FROM Customer`, and the
  executor said `no such column: age`. The route did what it was built for:
  `missing_object` → `get_schema`. The same happened on attempt 2. On attempt 3 the query
  was `… strftime('%Y', BirthDate)) FROM Employee`. It succeeded, and the answer read "The
  average age of our customers is 61.5 years.", with `error` unset.
- **Through `generate_sql`.** Two syntax errors (`near ","`, then `near "FROM"`), each sent
  back to `generate_sql`. The third query swapped `FROM Customer` for `FROM Employee` and
  succeeded. The answer hedged, but `error` was unset.
- **Declined.** `AVG(Age)`, `AVG(age)` and `AVG(age)` again, all `no such column`, then
  `missing_object` → `answer` with attempts exhausted. This is the correct outcome, and
  the one trial that passed.

Both routes exist to recover from the model's own mistakes. When the data simply isn't
there, every retry is another chance to find *something* that runs. On an unanswerable
question, a query that runs is an invented answer.

**The answer step invents what the rows don't say.** In one `sales-2011` trial, the query
was `SELECT Total FROM Invoice WHERE …` with no `SUM`. 2011 has 83 invoices, and the
guard's `LIMIT 50` cut them to 50. The answer step reported "The total sales in 2011 were
$18.86." The `truncated` flag is in the state and in the evidence, but `answer()` never
passes it to the model. So the model sees a list of 50 amounts, is asked for a total, and
makes one up.

**Joins across four tables fail three different ways.** All three `top-artists-revenue`
trials failed, each differently:
- An unqualified `ArtistId` in a four-table join gave `ambiguous column name: ArtistId`.
  `domains/sql/errors.py` has no prefix for that message, so it is classified `other`,
  which is not retryable. The run went straight to the answer step after one attempt,
  though a regenerated query would very likely have qualified the column. This is the
  first time the message has been observed, which is what that file asks for before a
  prefix is added.
- `JOIN Artist a ON 1=1 /* error: no join condition */`: the model annotated its own cross
  join and ran it anyway.
- `Artist.ArtistId = Invoice.CustomerId`: two unrelated ids. The answer named five artists
  with confidence.

**Grouping by the key instead of the name.** Twice, `tracks-per-genre` came back as
`GenreId, COUNT(…)`, answered as "Genre 1: 1297 tracks".

**SQLite's double-quoted strings** (found in a probe before the baseline, not in its
trials). One early `customer-age` probe ran
`SELECT AVG(STRFTIME('%Y', "BirthDate") - …) FROM Customer`. `Customer` has no
`BirthDate`, but SQLite treats a double-quoted identifier that resolves to nothing as a
*string literal*. It is a legacy compatibility feature, called DQS. So the query
"succeeded": `strftime` of the string `'BirthDate'` is NULL, and the result was `[[None]]`.
The answer said "No results found.", with no error. The executor never saw a missing
column, so the route never got a chance. Python 3.12+ can switch DQS off per connection:
`connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)`. With it off, in the
executor image (Python 3.12.15, SQLite 3.46.1), the same query fails with
`no such column: "BirthDate" - should this be a string literal in single-quotes?`, and
`classify_sql_error` already maps that to `missing_object`. The guard lives in the tool
server, so that is where this belongs.

None of these is fixed here, on purpose. Each is a change for milestone 6 to make and then
measure against this table:
- DQS off in the executor;
- `ambiguous column name` classified as a retryable error, probably `syntax`;
- the truncation flag passed to the answer step;
- some way for a run to conclude that the data cannot answer, other than SQL failing three
  times;
- prompts for qualified columns in joins, and for grouping by name.

---

## What this sets up

| Later | What it builds on |
|---|---|
| Milestone 6: joins and aggregations | Change prompts or the executor, run the evals, read the new rates next to the baseline's. The findings above are the to-do list. |
| Milestone 7: retry routes | `attempts` per trial shows which runs retried. Their `run_id`s lead to the `route_decision` events. In the baseline only `customer-age` retried: two trials through `missing_object → get_schema`, one through `syntax → generate_sql`. Each route "recovered" once into an invented answer. |
| Milestone 8: observability | Every trial's `run_id` becomes a trace id to look up in Grafana |
| Milestone 9: LangGraph | The `lg` target is already in the parametrization: the same eval set, the same scorer |
| Milestone 11: 20 questions, both engines | More cases in `questions.yaml`, nothing else. Enough trials, perhaps, for a gate. |
| Milestone 12: promptfoo | The same cases, and possibly the same scorer, called from a promptfoo Python assertion. The baseline is the snapshot to diff against. |

---

## What was checked

The candidate questions were tried against the running step-4 deployment before anything
was written. Their expected rows were generated from `data/chinook.db`, not typed. Then
the step was implemented in the repo and run as the instructions say.

- **Probes.** Each candidate was asked once, by a throwaway client, through the
  port-forward: 13–66 seconds per question on CPU Ollama. Five answered correctly. The
  other three are where the findings came from: genre ids, the `CustomerId` join, and the
  DQS `NULL`.
- **Offline.** `pytest`: 182 passed. That is step 4's 142, plus 32 in
  `tests/test_evals.py` and 8 in `domains/sql/tests/test_evals.py`. Every reference query
  reproduced its rows. `ruff check .` and `ruff format --check .` were clean.
- **The option loads.** Both `pytest -m live tests/test_evals_live.py --eval-repeats 1`
  and `pytest -m live --eval-repeats 1` (no path, via `testpaths`) accepted
  `--eval-repeats`. The latter collected 4 live tests: two smoke tests, and `test_eval`
  for scratch and lg.
- **`make k8s-up`** from this branch: 21 seconds. The query-agent image contains
  `core/evals.py`, and `/app/domains/sql/` has no `evals/` and no `tests/`.
- **A one-trial run:** 7 of 8 in 3 minutes 19 seconds. `lg` was skipped with
  `no lg orchestrator at http://localhost:8200`. The report was written under `runs/evals/`.
  Its blank lines between progress lines led to the one change made after that run (print
  the newline once).
- **The baseline run:** 16 of 24 in 12 minutes 23 seconds, `1 passed, 1 skipped`. The
  report was copied to `domains/sql/evals/baseline.json`.
- **The recheck:** one trial per case, 4 minutes 13 seconds. The header named the baseline,
  and the baseline column was filled for all 8 cases. 6 of 8 passed.
- **From `run_id` to log.** The invented-age trial's `run_id`, grepped from
  `kubectl logs`, gave its 15 orchestrator events plus the agents' and the executor's.
  Among them were both `route_decision`s (`missing_object → get_schema`) and the prompt
  shas quoted above. The other two `customer-age` trials were traced the same way, which
  gave each one's SQL per attempt and its routes.
- **DQS** was confirmed twice: locally (Python 3.14.2, SQLite 3.50.4), and in the executor
  image (3.12.15, 3.46.1). It is on by default, and `setconfig` switches it off. The
  resulting error classifies as `missing_object`.

Not checked:
- Anthropic models: no key is configured. The runner is a client of `/ask` and does not
  care which provider answers.
- The `lg` target, which doesn't exist yet. Its parametrization only showed that it skips
  cleanly.
- The compose stack as a target. It serves the same URL as the port-forward, and nothing
  in the runner tells the two apart.
