# Step 6 — What it's for

Companion to `step-6-instructions.md`. That file says what to do. This one says why.

---

## What Step 6 achieves

Step 5 recorded the "before": 16 of 24 trials, 67%, with `top-artists-revenue` at 0 of 3.
It also recorded a list of the ways the system failed. Step 6 is the first change measured
against that record. The rule for every change: fix each failure at the layer where it
happens. Use code where code can do the job exactly, and prompts only for what only the
model can do, which is writing the SQL.

Two things make this step different from simply "improve the prompts":

- **A probe found the first cause before anything changed.** The schema agent was asked
  directly what it selects (see 6.3). The baseline's two join failures and its
  grouping-by-id failures started there, not in the query agent. No query prompt can fix
  a join whose key columns the model was never shown. The first measurement then found
  the second cause: with every key in front of it, the model still composed joins wrongly.
  So the joins are now written out for it (see "Two measurements").
- **Every change stays in the sql domain.** The harness, the images, the manifests and the
  eval set are untouched. Making a domain better without editing the harness is the
  acceptance criterion from CLAUDE.md, met in practice.

---

## Where each change lives, and why there

| Change | Owner | Why there |
|---|---|---|
| DQS off, row cap | executor (tool server) | Guards live in tool servers, in code. Both are about what the database does with the SQL, whoever wrote it. |
| Error classification | `domains/sql/errors.py` | The single authority on what a SQL error means. The route reads its answer. |
| Join paths, columns, join conditions | schema agent, in code (`selection.py`) | The foreign-key graph is data, and a shortest path is an algorithm, not a judgement |
| Declining | query agent, as a field of its output | The query agent is the one that sees the question next to the exact schema it must answer from |
| Unanswerable routes | the pipeline (`domain.py`) | Only routes choose the next step, and both engines run the same routes |
| Telling the answer step about truncation | the pipeline's `answer` step | The flag was in the state all along. The step never passed it on. |
| Rules for joins, grouping, aggregation | prompts | What only the model does: writing SQL |

Nothing here is a harness change. `ErrorType.UNANSWERABLE` is the sql domain's word. The
harness carries it as the plain string `error_type`, as it carries `syntax`. "Could not
answer" reaches the client through the field the `/ask` contract already has for it:
`error`, with HTTP 200.

---

## 6.1 The executor

**Why double-quoted strings go off.** SQLite has a legacy compatibility feature, DQS. A
double-quoted name that matches no column becomes a string literal. In a step-5 probe,
`SELECT AVG(STRFTIME('%Y', "BirthDate") - …) FROM Customer` "succeeded". `Customer` has no
`BirthDate`, so `"BirthDate"` became the string `'BirthDate'`, its year was `NULL`, and the
run answered "No results found." with no error. The route never saw a missing column. The
SQLite documentation calls DQS a misfeature and recommends turning it off. Python 3.12+
can do that per connection with `setconfig`. With it off, the same query fails with
`no such column: "BirthDate" - should this be a string literal in single-quotes?`.

**What it costs.** A query that writes a string in double quotes, `WHERE Name = "AC/DC"`,
used to work by accident and now fails. That is a common habit in models trained on MySQL
code. The trade is a loud error that names its own fix, against a silent wrong answer. A
silent wrong answer is the one thing CLAUDE.md rules out ("never invent data"). 6.2
classifies the new error so that the trade costs one regeneration, and the query prompt
asks for single quotes so that it rarely comes up.

**Why the row cap moves into code.** The guard appends `LIMIT 50` only when the SQL has no
`LIMIT` anywhere, and it decides that with a regex over the text. So `LIMIT 1000` returns
1000 rows. So does a `LIMIT` that sits only in a subquery:
`SELECT * FROM (SELECT * FROM Track LIMIT 5) UNION ALL SELECT * FROM Track` returned all
3,508 tracks. That breaks three things that rely on the cap:
- the answer step's prompt size;
- the scorer's cost argument (step 5: "at most 50 rows");
- `truncated`, which only compared the row count with 50.

A text check is not a boundary. For writes, the boundary is the SQLite authorizer, and the
regex is only the first line. For rows, the boundary is now how the executor reads them:
`fetchmany(row_limit)`. The appended `LIMIT` stays, because it lets SQLite stop early on a
plain scan, and the evidence shows the limit that applied.

**Why `truncated` still means "may have more".** At exactly 50 rows, the executor can't
tell "cut" from "complete" without reading a 51st row, and the guard's `LIMIT 50` means
there is never a 51st to read. The answer prompt's wording follows that: "may have more".

---

## 6.2 Errors

**`ambiguous column name`** ended the first baseline `top-artists-revenue` trial after one
attempt. The query was a correct four-table join with one unqualified `ArtistId`. The
message wasn't in `errors.py`, so it was classified `other`, which is not retryable, and
the run went straight to the answer step. It is a mistake in the SQL, and the message says
exactly what to fix, so it is `syntax`, which goes back to `generate_sql`.

**`misuse of aggregate`** is the other classic aggregation mistake: `SUM(…)` in a `WHERE`
instead of a `HAVING`. It is equally a mistake in the SQL. It was produced on purpose
against Chinook (6.2's table), which is what `errors.py` asks for before a message is
added. The baseline never produced it, but this step is about aggregations, and an
aggregation mistake that ends the run on its first attempt would hide what the change
does.

**Why the DQS message is `syntax`, though it starts with `no such column:`.** SQLite adds
"should this be a string literal in single-quotes?" only when a double-quoted name matches
nothing. Two things can be wrong. Either the model meant a string, and the quotes are the
mistake. Or it meant a column that doesn't exist, and the query agent used a column it
wasn't shown. Both are fixed by regenerating the query: with the hint, or by declining
(6.4). Sending it to the schema agent instead would be worse than a wasted model call. The
new schema prompt says "if the error says a column does not exist … return an empty list",
so `"AC/DC"` could end a perfectly answerable question as unanswerable. The hint is
matched at the end of the message, so a question that quotes it can't trigger it. That is
the same reason the other messages are matched at the start.

---

## 6.3 Schema selection

### What the probe showed

Before writing any of this, the running step-5 schema agent was asked three questions,
three times each, and its selections were printed. The query agent sees only these tables
and columns:

| Question | What was selected (table: columns) |
|---|---|
| top 5 artists by revenue | `Invoice: CustomerId, InvoiceDate, Total; Track: —; InvoiceLine: TrackId; Album: ArtistId; Artist: ArtistId` |
| | All 11 tables, nine of them with no columns |
| | The five path tables with their keys, except `Invoice.InvoiceId`. The best of the three. |
| tracks per genre | `Genre: GenreId, Name; Track: TrackId, GenreId` (correct) |
| | `Genre: —; Track: TrackId, GenreId` |
| | `Genre: GenreId; Track: TrackId, Name, AlbumId, MediaTypeId, GenreId` |
| average customer age | `Customer: —; Invoice: CustomerId; Employee: ReportsTo` |
| | `Customer: CustomerId; Employee: EmployeeId, BirthDate` |
| | `Customer: —` |

Selecting tables went well enough: the right tables are nearly always there. Selecting
columns did not. Tables came back with no columns, without the keys their joins need, and
without the name the question asks for.

That explains the baseline better than the step-5 list did:
- **"Grouping by the key instead of the name"** (`tracks-per-genre`, 2 of 3) happens when
  `Genre.Name` is not in the schema the query agent sees. It *can't* group by name.
- **"Joins on unrelated ids"** (`top-artists-revenue`: `ON 1=1`, and
  `Artist.ArtistId = Invoice.CustomerId`) happens when the key columns on the path aren't
  there. Asked for a join it has no columns for, the model invents one.

Those were on step 5's list as prompt problems. They are schema problems first.

### What changes, and why

**The model names tables; code does the rest.** The model's output becomes a list of table
names. Every column of each table comes from the real schema, with its type, its keys and
its foreign keys. That was already the rule for types and foreign keys ("never re-emitted
by the model"). Columns now follow it too.

**What that costs.** The query agent's prompt grows. A selected table brings all its
columns, at most 15 in Chinook (`Employee`). The worst case, a model that names all 11
tables, is the full schema: 64 columns, about 110 lines and 2,800 characters. For a schema
of hundreds of wide tables, column pruning would matter again. It would then be done in
code, by relevance, not by an 8B model listing names. Precision of selection buys nothing
when recall fails.

**Join paths come from the foreign-key graph.** The model names what the question is about
(artists, invoices). It doesn't have to name the tables that link them. The code finds the
shortest foreign-key path between the named tables and adds every table on it. For
`Invoice` and `Artist`, that adds `InvoiceLine`, `Track` and `Album`: exactly the join the
reference query makes. Whenever the schema connects the named tables, the query agent
gets every table on the way between them, and every join it needs.

The method is greedy: each table joins the ones before it by its shortest path. The best
possible connection (a minimum Steiner tree) is NP-hard in general, and irrelevant with 11
tables. On Chinook, the greedy paths are the joins a person would write. Ties are broken
by name, and tables are taken in schema order, so the same names always give the same
schema text. A run is reproducible from its log only if that holds.

**The joins are written out.** After the tables, the schema text lists every join
condition between them, exactly as it goes in an `ON`: `Track.AlbumId = Album.AlbumId`.
The query prompt says to join only with listed conditions, and to use one table when none
is listed. This came out of the first measurement (see "Two measurements" below). With
the whole path and every key in front of it, the model still wrote
`InvoiceLine.TrackId = Album.AlbumId` twice. That skips `Track` and joins two unrelated
ids, in plain sight of the two `REFERENCES` clauses that say how. Composing a join from
two clauses in two tables is one step more than an 8B model reliably takes. Copying a
listed line is not.

**`REFERENCES` labels every key, joinable or not.** The first version showed only foreign
keys whose table was selected, so that every `REFERENCES` was a join the query agent
could make. The first measurement showed the cost. On "Which country has the most
customers?", only `Customer` was selected, and `SupportRepId` lost its label. The model
self-joined `Customer` on `SupportRepId = CustomerId` and answered "Canada". Now the
label says what a key column holds (`SupportRepId … REFERENCES Employee(EmployeeId)`),
and `Joins` alone says what can be joined. For a one-table schema, `Joins: none` says it
outright.

**Names are matched ignoring case.** SQLite's names are case-insensitive, so `invoice` is a
correct answer from the model. Dropping it, as the old exact match did, lost a table the
model chose correctly.

**An empty selection means "the data cannot answer".** The schema prompt has long said:
"If the question cannot be answered by any table in the schema, return an empty
list." Until now, the pipeline then sent an empty schema to the query agent anyway, which
wrote SQL against nothing. 6.5 finally honours what the prompt asked for.

---

## 6.4 The query agent may decline

**Why an explicit exit.** Step 5's most important finding was that retries can turn an
honest failure into an invention. On `customer-age`, the only way the run could conclude
"the data can't answer" was to fail three times. Every retry was another chance to find
something that runs, and twice it found employee birth dates. A system that may only stop
by failing is pushed, attempt by attempt, toward an answer. The fix is to let the model
say "this can't be answered" at the step that knows. The query agent sees the question
next to the exact schema it must answer from. If `Customer` has no birth date, that is
where it shows.

**Why not a separate "answerability" agent.** It would be one more model call on every
question: about 10–30 seconds on CPU Ollama. It would also be a second model making the
same judgement from the same inputs. The query agent already has them.

**Why `answerable` comes first.** A model writes the fields of its output in order. With
`sql` first, the model has committed to a query before it considers whether one should
exist. With `answerable` and `missing` first, it decides, says why, and only then writes
SQL, or doesn't.

**Why `missing`.** A decline is an answer with no rows, so its reason is its evidence. It
becomes the run's `error`. The answer step shows it, and the scorer prints it next to a
pass (`declined: …`). "No birth date in Customer" can be checked. A bare "no" can't.

**Why two shapes for one answer.** `SqlOut` is what the model fills in. It is kept flat,
with empty strings instead of optionals, because optionals are where providers' schema
support diverges (the comment in `models.py`). `GenerateSqlResponse` is our own contract
between services. There, `None` is the honest "no SQL", and a validator makes "both" and
"neither" impossible. The agent translates between the two once.

**The risk is false declines.** A model allowed to decline may decline answerable
questions. The eval measures both directions. `customer-age` passes only with `error`
set, and every other case fails if it is declined. A change that "fixes" the unanswerable
case by declining more often will show up as losses elsewhere.

---

## 6.5 The pipeline

**Why routes, not a step that jumps.** In this harness, steps return field updates and
only routes choose what runs next. Each choice is a `RouteDecision` event, "the event that
makes retry routing provable rather than believed" (`core/events.py`). Stopping early
because the data can't answer is a routing decision of exactly that kind. In the log, it
should look like one. The LangGraph engine (milestone 9) will compile both routes into
conditional edges, with no extra work.

**Why every routed step now leaves a `RouteDecision`.** `get_schema` and `generate_sql`
now have routes, so a successful run logs three decisions instead of one:
`generate_sql, execute, answer`. The decisions are the run's path, which is what
milestone 7 needs to read from the logs.

**Why `error` and `error_type`.** No new harness concept is needed. `RunState` has both
fields. `error` set at the end already means "could not answer" to the `/ask` contract, to
the scorer, and to `RunEnd.ok`. The domain adds only a word to its own vocabulary,
`unanswerable`, which no SQL error produces and no route retries.

**Why a stale `error_type` can't misfire.** After a failed execution, `error_type` stays
`syntax` or `missing_object` while `get_schema` and `generate_sql` run again. The new
routes check for `unanswerable` alone, which only the two exits set. So a retry goes on
exactly as before, unless the step that just ran declined.

**Why the SQL is kept on a decline.** Evidence is what was tried. If the query agent
declines on its second attempt, the first attempt's SQL and its error are the evidence for
that decision. Clearing `sql` would make the run look as if it never tried.

**Why `attempts` doesn't count a decline.** `attempts` counts executions, and
`max_attempts` is a budget of executions. A decline executes nothing.

**Why the answer step is told about truncation.** In the baseline's failed `sales-2011`
trial, the query listed invoice totals without `SUM`. The cap cut them to 50, and the
answer step reported "The total sales in 2011 were $18.86": a number that no query
computed. `truncated` was in the state and in the evidence, but `answer()` never passed it
to the model. Now it does, and the answer prompt forbids totals from a cut list.

The eval can't see this improvement. It scores evidence, and the evidence was already
wrong in that trial. What changes is the wording next to a wrong result: "only the first
rows are shown" instead of an invented total. Milestone 12's judge is what would score
that.

---

## 6.6 Prompts

**What the rules are.** Each one names a general SQL mistake, observed in the baseline or
the probe:

| Rule | Observed as |
|---|---|
| Join only with the conditions listed under `Joins`; none listed means one table | `JOIN Artist a ON 1=1`; `Artist.ArtistId = Invoice.CustomerId`; `InvoiceLine.TrackId = Album.AlbumId`; a `Customer` self-join |
| Qualify every column when there are several tables | `ambiguous column name: ArtistId` |
| Select the name or title of X, not only its id | "Genre 1: 1297 tracks" |
| Compute totals in the query, because only the first rows come back | `SELECT Total` without `SUM`, cut to 50 rows |
| After a join, add up the most detailed table's amounts | Revenue summed from `Invoice.Total` over a join to its lines, so each total repeats once per line |
| Single quotes for strings | The cost of DQS off (6.1) |
| Decline rather than substitute something that looks similar | Employee birth dates for customer ages |
| An error is not a reason to decline | Two top-5 trials in the first measurement declined right after a failed query, with reasons that were false ("missing a table for artist name") |

**The risk of tuning to the test.** Every rule was prompted by a failure in the eval set.
Rules written that way can become answers to the eval set, and then a better number means
less. Three things keep that in check:
1. **Code before prompts.** The biggest change, schema selection, is code. It doesn't know
   which questions are in the eval set.
2. **General wording.** No rule names a Chinook table. The one example, "line items, not
   the total of the invoice or order", is the textbook case of join fan-out, which any
   sales schema has.
3. **A held-out probe.** Nine questions that are not in the eval set were asked before and
   after the change: two unanswerable, seven answerable, with joins, rankings and
   aggregations. They are not committed. They are a check on this step, not a new eval
   set. See "What was checked".

The lasting fix is milestone 11's larger set. With 20 questions there is room to keep some
back.

---

## 6.8 Measuring

**Same questions, same models, same runner.** `questions.yaml` is untouched. Comparing
pass rates over different questions measures the questions. The models are the
overlay's `ollama:llama3.1:8b` for all three roles, as in the baseline.

**How to read the comparison.** Step 5 showed that three trials per case gives a rate of
0, 33, 67 or 100%, and that identical runs move by a trial or two. So:
- a case one trial below its baseline rate is within noise;
- two below is worth reading the failures for;
- the overall rate over 24 trials is the number milestone 6 is held to.

**Why record a new baseline.** The baseline is "the system as it is now". Milestone 7
changes nothing about answering, but it reads runs. Milestone 9 must reach the same rates
on the LangGraph engine. Both should be compared with this system, not with step 5's.
Keeping the old file would make every later step look as good as step 6 did against step
5. The old report stays in git history, and its table stays in `step-5-explained.md`.

---

## Two measurements

The step was measured twice, with one change in between. Both runs used the same runner,
three trials per case, and `ollama:llama3.1:8b` on CPU for all three roles. The four
services ran as local processes on this branch.

| Case | Step 5 (baseline) | First measurement | Second measurement (recorded) |
|---|---|---|---|
| `brazil-customers` | 3/3 | 3/3 | 3/3 |
| `longest-track` | 3/3 | 3/3 | 3/3 |
| `acdc-albums` | 3/3 | 2/3 | 2/3 |
| `tracks-per-genre` | 1/3 | 2/3 | 3/3 |
| `top-customer-country` | 3/3 | 2/3 | 3/3 |
| `sales-2011` | 2/3 | 3/3 | 3/3 |
| `top-artists-revenue` | 0/3 | 0/3 | 1/3 |
| `customer-age` | 1/3 | 2/3 | 3/3 (2 real declines, see below) |
| **overall** | **16/24, 67%** | **17/24, 71%** | **21/24, 88%** |

### The first measurement, and what it changed

The first run (2026-10-06 17:24 UTC) had everything in 6.1–6.6 except two things: the
`Joins` block, and the prompt line that an error is no reason to decline. It gave 71%,
with the milestone's own question still at 0 of 3. The logs showed why:
- **The schema was right and the joins were still wrong.** All three top-5 trials had
  the whole path, every key, and every `REFERENCES` clause. Twice the query joined
  `InvoiceLine.TrackId = Album.AlbumId`, skipping `Track`. Once it stopped at `Album` and
  ranked album titles.
- **The decline became an escape hatch.** Two of those trials declined right after a
  failed query, with reasons that were false: "the schema is missing a table for artist
  name". A model that has just been told it was wrong, and is offered a way out, takes it.
- **A key without its label was guessed at.** On `top-customer-country`, `SupportRepId`
  had lost its `REFERENCES Employee` label (only foreign keys inside the selection were
  shown then). The model self-joined `Customer` on `SupportRepId = CustomerId`.
- **What worked already:** `customer-age` declined twice, once before running any SQL at
  all. `sales-2011` was 3/3. `ambiguous column name` went back to `generate_sql`, as 6.2
  intends.

The change that followed is what 6.3 and 6.6 now describe: written-out joins, a
`REFERENCES` label on every key, and "an error in the previous query is not a reason to
set answerable to false". All three are structural or general. None names a table, and
the eval set stayed the same.

### The second measurement

21 of 24 (2026-10-06 17:43 UTC), recorded as the new baseline. Every failure was read from
its run log:
- **`top-artists-revenue`, 1/3.** The trial that passed joined all four tables exactly as
  `Joins` lists them. One failure had the same correct joins, but wrote `SELECT TOP 5`
  (SQL Server's syntax), got `near "5": syntax error`, and sent the identical query twice
  more. The other attached a listed condition to the wrong join:
  `JOIN Artist ON Track.AlbumId = Album.AlbumId`. Joins are no longer the main failure.
  What fails now is putting a correct join into a correct query.
- **`acdc-albums`, 2/3 in both runs**, against 3/3 in the baseline. Both failures were
  `WHERE Title = 'AC/DC'`: the band read as an album title, with no join attempted.
  That's one trial per run, within the rule in 6.8. It is the one case below its baseline.
- **`customer-age`, 3/3, but only two are real declines.** Two trials declined: one
  before running any SQL ("There is no column in the schema …"), one after a nonsense
  first query. The third joined `Customer` to `Employee` to use employee birth dates,
  which is exactly the substitution this step targets. It failed only because it called a
  function SQLite doesn't have (`JULIANDIFF`), three times. The run ended with `error` set,
  so the scorer counted a pass. The scorer can't tell "declined" from "failed while
  inventing". The `/ask` contract returns the error message, not its kind. Only the sql
  domain's wording ("the data cannot answer this: …") tells them apart. A stricter
  unanswerable score needs the kind in the contract, which is a harness change for
  milestone 11, when there are more unanswerable cases to score.

**How much of 67% → 88% is real.** Over 24 trials each, the difference is about 1.7
standard errors. That is suggestive, but not conclusive on its own. What makes it
credible is that each per-case change has a mechanism in the logs:
- `tracks-per-genre`: `Genre.Name` is now always in the schema;
- `sales-2011`: totals are computed in SQL;
- `customer-age`: the decline;
- `top-artists-revenue`: the joins.

**What it cost.** 39 seconds per trial on average, against the baseline's 31. Every
selected table now brings all its columns, so prompts are longer on CPU. The baseline ran
in the cluster and these runs ran as local processes on the same machine, so part of the
difference may be the setting.

### The held-out probe

Nine questions that are not in the eval set were asked twice each through `/ask`, before
any change (the step-5 code) and after both. They were scored by `core.evals.score`
against rows from reference queries. They are a throwaway check, not committed:

| Question | Before | After |
|---|---|---|
| Which genre has the most tracks? | 2/2 | 1/2 |
| How many invoices were billed to customers in Germany? | 0/2 | 2/2 |
| What are the total sales for each billing country? | 2/2 | 2/2 |
| Which employee supports the most customers? Give the last name. | 1/2 | 2/2 |
| How many tracks are longer than five minutes? | 2/2 | 1/2 |
| What is the first name of the customer who has spent the most money? | 1/2 | 1/2 |
| How many tracks are there of each media type? | 0/2 | 1/2 |
| What is the average salary of our employees? (unanswerable) | 1/2 | 2/2 |
| What is the average rating of our tracks? (unanswerable) | 0/2 | 0/2 |
| **overall** | **9/18** | **12/18** |

The gains are in the same places as in the eval set, for the same reasons:
- Germany's invoices were joined `InvoiceId = CustomerId` before, and on the listed
  condition after.
- The support rep needed `SupportRepId`, which the old selection dropped.
- Salary was declined before any SQL ran.

So the changes generalise beyond the eight questions they were tested on, as far as 18
trials can show.

The losses are worth more than their size:
- **`Name` in five tables.** `Track`, `Genre`, `Artist`, `MediaType` and `Playlist` all
  have a `Name` column. With every column shown, the genre question once took `Track.Name`
  for the genre's name, as one `tracks-per-genre` trial did in the first measurement. This
  is the cost of showing every column, as predicted in 6.3. It is also the strongest case
  for a schema description: what each `Name` names.
- **Units.** "Longer than five minutes" became `Milliseconds > 5 * 60`. That is unrelated
  to this step, and a reminder that the eval set has no question about units.
- **Ratings.** "Average rating" was answered with `AVG(UnitPrice)` both times. A price is
  not a rating, but nothing in the schema says so. "Don't substitute something that only
  looks similar" works when the substitute is obviously about something else (employees
  for customers). It fails when the model can rationalise it.

### Findings for later

| Finding | Where it belongs |
|---|---|
| The same query sent three times after the same error (`SELECT TOP 5`) | Milestone 7 reads retries. A retry that changes nothing could stop early. |
| `TOP n`, a dialect slip | A one-line dialect rule ("LIMIT, not TOP"). Not added here, to keep the change count against the eval set at one. |
| The scorer passes a failed substitution on an unanswerable case | The error's kind in `/ask`, scored by `core.evals` (milestone 11) |
| `Name` in five tables | Column descriptions in the schema text |
| No question tests units, or a plausible substitute (price as rating) | Milestone 11's 20 questions |

---

## What was checked

- **The probe that started it.** The step-5 schema agent was asked 3 questions × 3 times
  before anything was written (6.3's table). The same probe after the change returned the
  full path with every column.
- **Error messages.** All three were produced by real failing queries on
  `data/chinook.db`, with DQS off, under Python 3.14.2 and SQLite 3.50.4. `ambiguous
  column name` also appears in the step-5 baseline, which ran in the image (SQLite 3.46.1).
  `misuse of aggregate` was not run in the image.
- **The row-cap hole.** Reproduced before the fix: the subquery-`LIMIT` query returned
  3,508 rows. After: 50, with `truncated` set. The same holds in a test.
- **Offline.** `pytest`: 221 passed. That is step 5's 182 plus 39 (route scenarios, the
  route table, selection, executor, agents, models, errors). `ruff check .` and `ruff
  format --check .` are clean.
- **Two live eval runs** of 16 minutes each, through the runner as 6.8 describes, against
  the local processes. Every failed trial was read from `runs/<run_id>.jsonl`: the schema
  selection, each query, each error, each route.
- **The held-out probe**, before and after: 18 trials each.
- **The new baseline loads.** `pytest tests/test_evals.py` validates it.

Not checked:
- **The cluster.** Docker Desktop wasn't running, so `make k8s-up` was not run on this
  branch. No image, manifest or dependency changed, and the executor image's Python
  (3.12.15) has `setconfig`. Step 5 confirmed DQS switching in that image. Run 6.8 in the
  cluster before relying on the image.
- **Anthropic models**: no key is configured.
- **The `lg` target**, which doesn't exist yet.

---

## What this sets up

| Later | What it builds on |
|---|---|
| Milestone 7: retry routes | Every routed step logs a `RouteDecision`, so a run's path is in its log. `ambiguous column name` and the DQS hint now reach `generate_sql`, so a forced failure (for example, a double-quoted string) exercises it. The baseline already shows a retry that changed nothing: the same `SELECT TOP 5` three times. |
| Milestone 8: observability | Unanswerable runs end with `error_type` `unanswerable` and `ok=false`. That makes them a series a dashboard can show apart from failures. |
| Milestone 9: LangGraph | Three conditional edges instead of one, all compiled the same way. The route-table tests are already parametrized over engines. |
| Milestone 11: 20 questions | More unanswerable cases, to measure false declines in both directions. Room for held-out questions. |
| The ops domain | The same two ideas carry over. A guard enforced where the data is read, not in a regex. An explicit "can't answer" exit that is a route, not a failure. |
