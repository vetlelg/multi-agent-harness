# Step 5 — Instructions

Exact actions only. For the reasoning behind any of it, see `step-5-explained.md`.

**Convention:** file sections give you names, signatures, fields and required behaviour.
They are specifications, not source. Exact strings are given only where the exact string
is the point: the eval set (it is data), option names, file names, and commands.

**Goal (milestone 5):** the sql domain has an eval set, `domains/sql/evals/questions.yaml`:
8 questions, each with the result its answer must rest on. Among them are a join,
aggregations, and one question the data cannot answer. A `live` runner asks every question
several times through `/ask`, scores the `query` evidence against the expected rows, and
reports the pass rate per question and overall. Its first report against the deployed
system is committed as the baseline.

**What changes:** one new harness module (`core/evals.py`), the runner and its tests under
`tests/`, the eval set and its own test in the domain, and `CLAUDE.md`. No service, prompt,
image or manifest changes. The baseline measures the system exactly as step 4 left it.

**State of play:** 5.0–5.7 are done. Claude implemented them on request and ran every
check below on 2026-10-05. The baseline is recorded in `domains/sql/evals/baseline.json`:
16 of 24 trials, 67%. Nothing is committed yet. The commit (5.6) is yours.

**Checked against:** the step-4 deployment (k3d 5.9.0, k3s v1.35.9), rebuilt from this
branch with `make k8s-up`; Ollama `llama3.1:8b` on CPU for all three roles; pytest 9.1.1,
PyYAML 6.0.3. See "What was checked" at the end of `step-5-explained.md`.

---

## 5.0 Before you start — DONE

- You are on `build/step-5`, branched from `build/step-4`.
- `data/chinook.db` exists (`make db`). The eval set's own test runs every reference query
  on it.
- The venv has `requirements-dev.txt` installed. It already pins `pyyaml==6.0.3`, which is
  the only thing the runner needs beyond what is there. Nothing to install.
- Step 4 runs. `make k8s-up`, then `make k8s-forward` in a second terminal.
  `curl http://localhost:8100/healthz` answers. Ollama is running.
- `.env` has `RUNS_DIR=runs`. The runner writes its report under it.

---

## 5.1 Harness: `core/evals.py` — DONE

The harness side of evaluation, for every domain. It parses no YAML and opens no network
connection: the runner (5.3) does those. It imports `core.models`, `core.registry`,
pydantic and the standard library. It ships in every image with the rest of `core/`, but
no service imports it.

### Where a domain keeps its evals

| Name | What |
|---|---|
| `EVAL_SET` | `"questions.yaml"` |
| `BASELINE` | `"baseline.json"` |
| `DECIMALS` | `2`: the precision numbers are compared at |
| `evals_dir(domain: str) -> Path` | `domains/<name>/evals/`, found from the imported package's `__path__[0]`, not from a hard-coded root |
| `domains_with_eval_sets() -> list[str]` | The names from `core.registry.available_domains()` whose `evals_dir(name) / EVAL_SET` is a file |

### The eval set

`EvalCase(Strict)`: one question, and what its answer must rest on.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str`, `Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")` | required | Names the case in reports and baselines |
| `question` | `str` | required | Sent as `AskRequest.question` |
| `reference` | `str \| None` | `None` | How `rows` was derived, in the domain's query language. The harness never runs it. |
| `rows` | `list[Row] \| None` | `None` | The expected result |
| `ordered` | `bool` | `False` | Row order is part of the answer (a ranking) |
| `unanswerable` | `bool` | `False` | The data cannot answer: the run must end with `error` set |

An `after` model validator raises `ValueError`, naming the case id, unless:
- exactly one of these holds: `rows` is not `None`, or `unanswerable` is true;
- `ordered` is true only together with `rows`;
- every row in `rows` has the same length, and that length is not 0.

`EvalSet(Strict)` has one field, `cases: list[EvalCase]`, with `Field(min_length=1)`. An
`after` validator raises `ValueError` naming any case id that appears twice.

### Matching rows

```
def rows_match(expected: list[Row], actual: list[Row], *, ordered: bool) -> bool
```

True when `actual` holds `expected`:

1. Same number of rows, or False. Two empty lists match.
2. Every value is normalised before comparing. `int` and `float` (but not `bool`) become
   `round(float(v), DECIMALS)`. Everything else is compared as it is: strings exactly,
   `None` as `None`.
3. Columns: try every choice of `len(expected[0])` distinct columns of `actual`, in every
   order (`itertools.permutations(range(len(actual[0])), k)`). It is a match if any one of
   them projects `actual` onto `expected`. Column names play no part (they aren't passed
   in). Neither does column order, or extra columns in `actual`.
4. Rows: compared as lists of tuples when `ordered`, otherwise as multisets
   (`collections.Counter` of tuples).

### Scoring one response

```
class Verdict(Strict):  passed: bool, reason: str
def score(case: EvalCase, response: AskResponse) -> Verdict
```

The first row that applies decides:

| Case | Response | Verdict |
|---|---|---|
| `unanswerable` | `error` set | pass, `declined: <error>` |
| `unanswerable` | `error` is `None` | fail, `answered, but the data cannot answer this` |
| has `rows` | `error` set | fail, `error: <error>` |
| has `rows` | no `QueryEvidence` whose `rows` is not `None` | fail, `no query result in the evidence` |
| has `rows` | any such `QueryEvidence` whose rows match | pass, `result matches` |
| has `rows` | otherwise | fail, `result differs: got <n> row(s) of <columns>, expected <m> row(s) of <k> column(s)`, taken from the last such evidence |

The answer's wording is never read.

### The report

`Trial(Strict)`: one asking of one case.

| Field | Type | Meaning |
|---|---|---|
| `run_id` | `str` | Finds the run in the log |
| `passed` | `bool` | |
| `reason` | `str` | The verdict's reason, or why there was no response |
| `attempts` | `int \| None` | From the response. `None` when there was no response. |
| `query` | `str \| None` | The first `QueryEvidence`'s `query` |
| `answer` | `str \| None` | The response's `answer` |
| `elapsed_ms` | `int` | Wall time of the `/ask` call |

`CaseResult(Strict)`: `id: str`, `question: str`, `trials: list[Trial]` with
`Field(min_length=1)`. Properties: `passes` (the trials that passed), `rate`
(`passes / len(trials)`).

`EvalReport(Strict)`: `domain: str`, `target: str`, `repeats: int` with `Field(ge=1)`,
`started_at: str` (UTC, ISO 8601 to the second, `Z` suffix), `cases: list[CaseResult]`.
Properties: `passes`, `total` (all trials) and `rate` (`passes / total`).

Rates are properties, not fields. The file stores what happened, and a rate computed on
read can't disagree with the trials it came from.

```
def render(report: EvalReport, baseline: EvalReport | None = None) -> str
```

Plain text for the terminal, in this order:
- A header line: domain, target, trials per case. With a baseline, also its
  `started_at` date and its target.
- One row per case: id, `passes/trials`, the rate as a whole percent, and, with a baseline,
  the baseline's rate for the same id. Print `-` when the baseline has no such case. Without
  a baseline, leave that column out.
- An `overall` row with the same columns.
- If any trial failed, a `failures:` line, then one line per failed trial: case id, full
  `run_id`, reason.

---

## 5.2 Harness tests: `tests/test_evals.py` — DONE

Offline, in the default run.

**`rows_match`**, one parametrized test:

| expected | actual | ordered | result | why |
|---|---|---|---|---|
| `[[5]]` | `[[5]]` | no | True | |
| `[["USA"]]` | `[["USA", 13]]` | no | True | extra column |
| `[["Rock", 1297]]` | `[[1297, "Rock"]]` | no | True | column order |
| `[["Rock", 1297]]` | `[[1, 1297]]` | no | False | an id where a name should be |
| `[["Rock", 1297]]` | `[["Rock"]]` | no | False | missing column |
| `[["a"], ["b"]]` | `[["b"], ["a"]]` | no | True | row order |
| `[["a"], ["b"]]` | `[["b"], ["a"]]` | yes | False | row order, when it matters |
| `[["a"], ["b"]]` | `[["a"], ["b"], ["c"]]` | no | False | extra row |
| `[[1], [1], [2]]` | `[[1], [2], [2]]` | no | False | multiset, not set |
| `[[105.93]]` | `[[105.92999999999999]]` | no | True | float noise |
| `[[5]]` | `[[5.0]]` | no | True | int and float |
| `[[469.58]]` | `[[469.6]]` | no | False | rounded too far |
| `[["USA"]]` | `[["usa"]]` | no | False | strings are exact |
| `[]` | `[]` | no | True | |
| `[]` | `[[1]]` | no | False | |

**Validation.** Each of these raises `pydantic.ValidationError`:
- a case with neither `rows` nor `unanswerable`;
- a case with both;
- `ordered` on an `unanswerable` case;
- ragged rows, `[[1], [1, 2]]`;
- an empty row, `[[]]`;
- the id `Brazil customers`;
- an `EvalSet` with two cases of the same id;
- an `EvalSet` with no cases.

**`score`.** One test per row of the table in 5.1, each with an `AskResponse` built by hand.
Assert `passed`, and that the reason contains what tells it apart: the error text, the
returned columns.

**`render`.** Build a report of two cases, three trials each: one with 3 passes, one with 2.
Then:
- With a baseline that lacks the second case, the output contains both ids, `3/3`, `2/3`,
  `67%`, the baseline's rate for the first case, a `-` for the second, an `overall` row with
  `5/6`, and the failed trial's `run_id` and reason.
- Without a baseline, the output has no baseline column.

**Every domain's eval set.** Parametrize over `domains_with_eval_sets()`. For each domain,
`EvalSet.model_validate(yaml.safe_load(...))` the eval set. If the domain has a
`BASELINE`, `EvalReport.model_validate_json` it too: a baseline the runner can no longer
read is a broken baseline.

---

## 5.3 The runner — DONE

### `tests/conftest.py`

- `pytest_addoption`: `--eval-repeats`, `type=int`, default `3`. Help text: how many times
  the live eval runner asks each question.
- A fixture `eval_repeats` that returns the option's value.

### `tests/test_evals_live.py`

`pytestmark = pytest.mark.live`. One test, `test_eval`, parametrized over domain × target:
- domains: `domains_with_eval_sets()`;
- targets: `scratch` and `lg`. The URL is the domain settings' `<target>_url`, from
  `load_domain_settings`.

For each (domain, target):

1. `GET <url>/healthz`, with a 5-second timeout. On any `httpx.HTTPError`, call
   `pytest.skip("no <target> orchestrator at <url>")`.
2. Load the eval set: `yaml.safe_load`, then `EvalSet.model_validate`. Load
   `evals_dir(domain) / BASELINE` as an `EvalReport`, if it exists.
3. For each case, run `eval_repeats` trials. Each trial:
   - Generates a `run_id` with `core.events.new_run_id` and sends it in the `AskRequest`.
     That way a failed trial can still be found in the log.
   - `POST <url>/ask` with the CLI's timeout, `settings.llm_timeout_s * 4`.
   - `httpx.TimeoutException`: a failed trial, reason `no response within <n> s`.
   - Status 500 or above: a failed trial, reason `HTTP <status>`. The service broke on
     this question, and that counts against the pass rate.
   - Any other status that isn't 200: `raise_for_status()`. The runner itself is wrong, so
     stop.
   - A response whose `domain` is not this domain: `pytest.fail`. Something else is
     listening at that URL.
   - Otherwise: `score` the response.
   - Connection errors are not caught. If the orchestrator goes away mid-run, the test
     errors.
   - After each trial, print one progress line inside `with capsys.disabled():`: case id,
     trial number, pass or FAIL, seconds.
4. Build the `EvalReport`. `started_at` is taken before the first trial.
5. Print `render(report, baseline)` inside `with capsys.disabled():`, so it shows without
   `-s`.
6. If `settings.runs_dir` is set, write `report.model_dump_json(indent=2)` to
   `<runs_dir>/evals/<domain>-<target>-<UTC %Y%m%dT%H%M%SZ>.json`. Create the directory
   first. Write with `newline="\n"`. Print the path.

The test makes no assertion on the pass rate. It fails only when the run itself goes wrong.

---

## 5.4 The sql eval set: `domains/sql/evals/questions.yaml` — DONE

The cases are data, so here they are exactly:

| id | covers |
|---|---|
| `brazil-customers` | one table: filter and count. Milestone 1's question. |
| `longest-track` | one table: order and limit |
| `acdc-albums` | a join |
| `tracks-per-genre` | a join and `GROUP BY`; 25 rows |
| `top-customer-country` | an aggregation, ranked |
| `sales-2011` | an aggregation over dates stored as text |
| `top-artists-revenue` | four tables joined, aggregated, ranked; order matters. Milestone 6's question. |
| `customer-age` | unanswerable: `Customer` has no birth date (`Employee` does) |

```yaml
# Eval set v0 for the sql domain (milestone 5).
#
# Each case is a question and the result its answer must rest on. The live runner
# (tests/test_evals_live.py) asks each one through /ask and scores the query evidence
# with core.evals: column names, column order and extra columns don't matter; the rows
# must match exactly, in order only when `ordered: true`, numbers at two decimals.
# `unanswerable: true` means the data cannot answer: the run must end with `error` set.
#
# `reference` is how `rows` was derived. domains/sql/tests/test_evals.py runs it on
# data/chinook.db, so an expected result can't drift from the data.

cases:
  - id: brazil-customers
    question: How many customers are from Brazil?
    reference: SELECT COUNT(*) FROM Customer WHERE Country = 'Brazil'
    rows: [[5]]

  - id: longest-track
    question: What is the name of the longest track?
    reference: SELECT Name FROM Track ORDER BY Milliseconds DESC LIMIT 1
    rows: [["Occupation / Precipice"]]

  - id: acdc-albums
    question: How many albums does AC/DC have?
    reference: >-
      SELECT COUNT(*) FROM Album JOIN Artist ON Artist.ArtistId = Album.ArtistId
      WHERE Artist.Name = 'AC/DC'
    rows: [[2]]

  - id: tracks-per-genre
    question: How many tracks are there in each genre?
    reference: >-
      SELECT Genre.Name, COUNT(*) FROM Track JOIN Genre ON Genre.GenreId = Track.GenreId
      GROUP BY Genre.GenreId
    rows:
      - ["Rock", 1297]
      - ["Latin", 579]
      - ["Metal", 374]
      - ["Alternative & Punk", 332]
      - ["Jazz", 130]
      - ["TV Shows", 93]
      - ["Blues", 81]
      - ["Classical", 74]
      - ["Drama", 64]
      - ["R&B/Soul", 61]
      - ["Reggae", 58]
      - ["Pop", 48]
      - ["Soundtrack", 43]
      - ["Alternative", 40]
      - ["Hip Hop/Rap", 35]
      - ["Electronica/Dance", 30]
      - ["Heavy Metal", 28]
      - ["World", 28]
      - ["Sci Fi & Fantasy", 26]
      - ["Easy Listening", 24]
      - ["Comedy", 17]
      - ["Bossa Nova", 15]
      - ["Science Fiction", 13]
      - ["Rock And Roll", 12]
      - ["Opera", 1]

  - id: top-customer-country
    question: Which country has the most customers?
    reference: SELECT Country FROM Customer GROUP BY Country ORDER BY COUNT(*) DESC LIMIT 1
    rows: [["USA"]]

  - id: sales-2011
    question: What were the total sales in 2011?
    reference: SELECT SUM(Total) FROM Invoice WHERE strftime('%Y', InvoiceDate) = '2011'
    rows: [[469.58]]

  - id: top-artists-revenue
    question: Who are the top 5 artists by total invoice revenue?
    reference: >-
      SELECT Artist.Name FROM InvoiceLine
      JOIN Track ON Track.TrackId = InvoiceLine.TrackId
      JOIN Album ON Album.AlbumId = Track.AlbumId
      JOIN Artist ON Artist.ArtistId = Album.ArtistId
      GROUP BY Artist.ArtistId
      ORDER BY SUM(InvoiceLine.UnitPrice * InvoiceLine.Quantity) DESC LIMIT 5
    ordered: true
    rows: [["Iron Maiden"], ["U2"], ["Metallica"], ["Led Zeppelin"], ["Lost"]]

  - id: customer-age
    question: What is the average age of our customers?
    unanswerable: true
```

Strings in `rows` are quoted on purpose. Unquoted YAML turns some words into booleans or
numbers.

---

## 5.5 The eval set's own test: `domains/sql/tests/test_evals.py` — DONE

- Load the eval set once, at import: `evals_dir("sql") / EVAL_SET`.
- The same `needs_db` skip as `test_services.py`.
- `test_reference_returns_expected_rows`, parametrized over the cases that have `rows`,
  with the case ids as test ids:
  - `reference` is set;
  - run it on a read-only connection (`file:<db_path>?mode=ro`, `uri=True`), and close the
    connection afterwards;
  - the result has exactly `len(case.rows[0])` columns (`cursor.description`), and
    `rows_match(case.rows, result, ordered=case.ordered)`.
- `test_the_runner_finds_this_eval_set`: `"sql" in domains_with_eval_sets()`.

```bash
pytest domains/sql/tests/test_evals.py -v
# expect: 7 reference tests and the discovery test pass
```

---

## 5.6 Run it, and record the baseline — DONE

```bash
pytest                                  # everything offline, including 5.2 and 5.5
ruff check .
make k8s-up                             # the cluster runs this tree
make k8s-forward                        # second terminal; leave it running
pytest -m live tests/test_evals_live.py
```

Expect one progress line per trial: 24 trials at 10–95 seconds each on CPU Ollama, about
12 minutes in all. Then come the table, `report: runs/evals/sql-scratch-<stamp>.json`, and
`1 passed, 1 skipped`. The skip is `lg`: nothing listens on 8200 until milestone 9.

The recorded run gave this table. Yours will differ, because model output varies:

```
sql on scratch: 3 trial(s) per case
  case                   passed   rate
  brazil-customers          3/3   100%
  longest-track             3/3   100%
  acdc-albums               3/3   100%
  tracks-per-genre          1/3    33%
  top-customer-country      3/3   100%
  sales-2011                2/3    67%
  top-artists-revenue       0/3     0%
  customer-age              1/3    33%
  overall                 16/24    67%
```

followed by a `failures:` section with 8 lines: run id and reason for each failed trial.

Record it:

```bash
cp runs/evals/sql-scratch-<stamp>.json domains/sql/evals/baseline.json
```

The file in the repo is the report of 2026-10-05T01:36:45Z. Commit `baseline.json` with the
rest of the step. Put the date, the commit and the models in the commit message (the
overlay's `ollama:llama3.1:8b` for all three roles). The report records what happened, not
what it ran on.

Check that the runner reads it back:

```bash
pytest -m live tests/test_evals_live.py --eval-repeats 1
# expect: the header says "baseline from 2026-10-05 on scratch", and the table has a
#         baseline column, filled for all 8 cases (about 4 minutes)
```

Check that a failed trial leads to its events. Take a `run_id` from the `failures:` section:

```bash
kubectl -n harness-sql logs -l app.kubernetes.io/name --tail=-1 --prefix=false \
  --max-log-requests=20 | grep <run_id> | python -c "
import sys
from core.events import parse_event
for e in sorted((parse_event(l) for l in sys.stdin if l.startswith('{')), key=lambda e: e.ts):
    print(f'{e.service:22s} {e.type:15s} seq={e.seq}')
"
# expect: the run's events from all four pods; route_decision lines for its retries
```

The pods' logs start at the last `make k8s-up`. Run this before the next one.

---

## 5.7 CLAUDE.md — DONE

- Harness modules table: add `evals.py`. Its responsibility: eval sets
  (`EvalCase`/`EvalSet`), scoring against `/ask` evidence (`rows_match`, `score`), and the
  pass-rate report (`EvalReport`, `render`).
- The domain contract tree: the `evals/` line becomes `questions.yaml` (cases with
  expected results) and `baseline.json` (the recorded report), promptfoo config later.
- Below the tree, one paragraph on how cases are scored. Answerable cases: the `query`
  evidence must hold the expected rows. Column names, column order and extra columns are
  ignored. Rows match exactly, ordered only when the case says so, and numbers are compared
  at two decimals. Unanswerable cases: the run must end with `error` set. Then the runner
  command: `pytest -m live tests/test_evals_live.py [--eval-repeats N]`.
- Repo layout: the `tests/` line also names the live eval runner.
- Milestones: mark 5 ✅.

---

## Done when

```bash
pytest                                       # green, including the new offline tests
ruff check .                                 # clean
pytest -m live tests/test_evals_live.py      # a table with 8 cases; 1 passed, 1 skipped (lg)
```

- `domains/sql/evals/baseline.json` is committed, and the runner prints its rates next to
  the new ones.
- Every expected result in `questions.yaml` is checked against the database by
  `domains/sql/tests/test_evals.py`.
- Outside `domains/sql/`, the step touched only harness files (`core/evals.py`, `tests/`)
  and `CLAUDE.md`. A second domain gets the runner by adding
  `domains/<name>/evals/questions.yaml`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `1 skipped` for scratch: `no scratch orchestrator at http://localhost:8100` | No port-forward, or the forward died on a rollout | `make k8s-forward` again (step 4, 4.9) |
| `pytest: error: unrecognized arguments: --eval-repeats` | pytest didn't load `tests/conftest.py`. It loads it from the test paths you pass, and from `testpaths` when you pass none. | Run from the repo root, with a path under `tests/` or no path at all |
| Trials fail with `HTTP 500` | An agent raised: a truncated or unparsable model response, or a timeout. The orchestrator returns 500. | Expected now and then with an 8B model, and counted. `make k8s-logs`, or grep the trial's `run_id` in `kubectl logs`. |
| `Failed: http://localhost:8100 answers for domain …` | Another domain's orchestrator holds the port | Forward the right namespace's orchestrator |
| `ValidationError` loading `baseline.json` | `core/evals.py`'s report models changed after the baseline was recorded | Record the baseline again (5.6). It is a measurement, not a hand-written file. |
| A reference test fails after editing `questions.yaml` | The expected rows don't match what the reference returns, or a string was left unquoted | Fix the case. The database is the authority. |
| No `report:` line | `RUNS_DIR` is unset | Set `RUNS_DIR=runs` in `.env` |
| Rates move between two runs with nothing changed | Model output varies. In the recheck right after the baseline, `customer-age` went from 1/3 to 1/1. | Nothing to fix. Compare per-case rates over more trials (`--eval-repeats 5`) before reading a change into them. |
| A trial's `run_id` finds nothing in `kubectl logs` | Pod logs start at the pod's start, and `make k8s-up` replaces every pod | Look it up before the next `make k8s-up`. Durable logs come with Loki in milestone 8. |
