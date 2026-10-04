# Step 3 — Instructions

Exact actions only. For the reasoning behind any of it, see `step-3-explained.md`.

**Convention:** file sections give you names, fields, ordering and required behaviour.
They are specifications, not source. You write the files. Exact strings are given only
where the exact string is the point: ignore patterns, health-check flags, image tags,
service names and URLs.

**Goal (milestone 3):** the sql domain runs with `docker compose up`: four containers,
with the orchestrator image started as `DOMAIN=sql`. `python cli.py --target scratch "…"`
answers through it with no change to the CLI.

**No Python changes in this step.** Every file is new except `Makefile` and `CLAUDE.md`.

**State of play:** 3.0–3.11 are done. Step 3 is complete.

**Checked against:** Docker Desktop 29.1.3 (Engine 29.1.3, Compose v5.0.1), Ollama
`llama3.1:8b` on the Windows host, on a throwaway copy of this branch. See "What was
checked" at the end of `step-3-explained.md`.

---

## 3.0 Before you start — DONE

- You are on `build/step-3`.
- Docker Desktop is running. `docker version` shows Server 25 or newer (needed for the
  health check's `--start-interval`).
- `data/chinook.db` exists (`make db`). The executor image bakes it in, so the build fails
  without it.
- `.env` exists. Compose hands it to the containers at run time.
- Ollama is running: `curl http://localhost:11434/api/tags` lists `llama3.1:8b`.
- Nothing is listening on ports 8000, 8020 or 8100. Stop the step-2 `uvicorn` processes.

---

## 3.1 `.dockerignore` — DONE

Create it at the repo root, which is the build context for every image. Exactly:

```
# Secrets: never in a build context, never in an image.
.env

# Local machinery and artefacts.
.git
.venv
runs/
checkpoints/
**/__pycache__
**/*.py[cod]
.pytest_cache/
.ruff_cache/

# Not needed at runtime.
**/tests
**/evals
docs/
```

Do **not** add `data/` or `*.db`. Git ignores them, but Docker must not: the executor
image copies `data/chinook.db`.

---

## 3.2 What every image shares — DONE

This step has three Dockerfiles. All three follow this table:

| Concern | Required |
|---|---|
| Base image | `python:3.12.15-slim`, pinned by digest: `python:3.12.15-slim@sha256:<digest>` |
| Build context | The repo root. Every `COPY` source is repo-relative. |
| Environment | `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1` |
| Working dir | `/app` |
| Dependencies | Copy the requirements file(s) and `pip install --no-cache-dir -r` **before** any source is copied |
| User | `useradd --system --uid 10001 app`, right after the install. Switch with `USER 10001`: **numeric**, not `USER app`. |
| Port | The server listens on `0.0.0.0:8000`; `EXPOSE 8000` |
| Health check | Exactly the line below |
| PID 1 | The `uvicorn` process itself, never a shell that started it |

Get the digest:

```bash
docker buildx imagetools inspect python:3.12.15-slim
# use the "Digest:" line. On 2026-10-04 it was
# sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d
```

The health check, identical in all three:

```dockerfile
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)"]
```

**Shared prefix.** `service.Dockerfile` (3.3) and `orchestrator.Dockerfile` (3.5) must
begin with the same instructions, in the same order, with identical text, up to and
including `useradd`:

1. `FROM` the pinned base.
2. `ENV` the two variables.
3. `WORKDIR /app`.
4. `ARG REQUIREMENTS` with default `requirements.txt`.
5. Copy `requirements*.txt` into `/app`. Copy all of them, because
   `requirements-lg.txt` pulls in `requirements.txt` with `-r`.
6. `pip install --no-cache-dir -r` the file named by `REQUIREMENTS`.
7. `useradd` as in the table.

Do not declare any other `ARG` or `ENV` before step 7. A build arg declared earlier
becomes part of the cache key of every later `RUN`. The pip layer would then stop being
shared, and every image would reinstall every package.

---

## 3.3 Agent image: `infra/base/docker/service.Dockerfile` — DONE

A harness file. It builds the harness plus exactly one domain, serving one app, and it
must not name any domain.

Build args:

| Arg | Default | Meaning |
|---|---|---|
| `REQUIREMENTS` | `requirements.txt` | Which requirements file to install. `requirements-tool.txt` serves a tool server that needs nothing beyond Python packages. |
| `DOMAIN` | none (required) | Which `domains/<name>/` to copy |
| `APP` | none (required) | The uvicorn import string, e.g. `domains.sql.agents.schema_agent.app:app` |

After the shared prefix, in order:

1. Declare `DOMAIN` and `APP`.
2. Add a `RUN` that fails the build if `DOMAIN` or `APP` is empty, with a message naming
   both args.
3. Copy `APP` from the build arg into an `ENV` of the same name. The `CMD` reads it at
   run time.
4. Copy `core/`, `domains/__init__.py`, and `domains/$DOMAIN/` to the same paths under
   `/app`.
5. `USER 10001`, `EXPOSE 8000`, the health check.
6. `CMD`: run `uvicorn "$APP" --host 0.0.0.0 --port 8000`. Exec form cannot expand
   `$APP`, so wrap it in `sh -c`. Start the shell command with `exec`, so uvicorn
   replaces the shell as PID 1.

Verify from the repo root:

```bash
docker build -f infra/base/docker/service.Dockerfile \
  --build-arg DOMAIN=sql \
  --build-arg APP=domains.sql.agents.query_agent.app:app \
  -t harness/sql-query-agent:dev .

docker run --rm -d --name qa -p 127.0.0.1:8020:8000 harness/sql-query-agent:dev
curl http://localhost:8020/healthz
# expect: {"status":"ok","service":"query_agent"}
docker top qa
# expect: ONE process, UID 10001, command /usr/local/bin/python3.12 /usr/local/bin/uvicorn domains.sql...
#         (two processes, one of them "sh -c", means the exec is missing)
docker exec qa ls -A domains
# expect: __init__.py  sql        -- nothing else
docker stop qa

docker build -f infra/base/docker/service.Dockerfile .
# expect: the build FAILS at your check, printing your message
```

---

## 3.4 Executor image: `domains/sql/tools/executor/Dockerfile` — DONE

A domain file. The executor needs a file that no other image may contain.

In order:

1. `FROM`, `ENV`, `WORKDIR` as in 3.2.
2. Copy `requirements-tool.txt` only, then install it with `--no-cache-dir`.
3. `useradd` as in 3.2.
4. Copy `core/`, `domains/__init__.py`, and `domains/sql/`.
5. Copy `data/chinook.db` to `data/chinook.db`. The default `SQL_DB_PATH`
   (`data/chinook.db`) then resolves from `/app` without any setting.
6. `USER 10001`, `EXPOSE 8000`, the health check.
7. `CMD` in exec form: `uvicorn domains.sql.tools.executor.app:app --host 0.0.0.0 --port 8000`.
   There is no variable to expand, so no shell is needed.

Verify:

```bash
docker build -f domains/sql/tools/executor/Dockerfile -t harness/sql-executor:dev .

docker run --rm -d --name ex -p 127.0.0.1:8000:8000 harness/sql-executor:dev
curl -s http://localhost:8000/schema | python -m json.tool | head -20
# expect: tables with columns and foreign keys, as in step 1
curl -s -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d "{\"run_id\":\"test\",\"sql\":\"SELECT COUNT(*) FROM Customer WHERE Country = 'Brazil'\"}"
# expect: "ok":true ... "rows":[[5]]
docker logs ex | grep tool_call
# expect: the ToolCall event for that execution, as one JSON line
docker exec ex python -c "import httpx"
# expect: ModuleNotFoundError: No module named 'httpx'
docker stop ex
```

---

## 3.5 Orchestrator image: `infra/base/docker/orchestrator.Dockerfile` — DONE

A harness file. It packages one engine with every domain. It must not name a domain; the
`DOMAIN` setting chooses one at startup.

Build args:

| Arg | Default | Meaning |
|---|---|---|
| `REQUIREMENTS` | `requirements.txt` | `requirements-lg.txt` for the LangGraph engine (milestone 9) |
| `ENGINE` | `orchestrator_scratch` | The engine package to copy and serve |

After the shared prefix, in order:

1. Declare `ENGINE` and copy it into an `ENV` of the same name.
2. Copy `core/`, all of `domains/`, and `$ENGINE/`.
3. `USER 10001`, `EXPOSE 8000`, the health check.
4. `CMD`: run `uvicorn "${ENGINE}.app:app" --host 0.0.0.0 --port 8000`, with `sh -c` and
   `exec` as in 3.3.

Verify:

```bash
docker build -f infra/base/docker/orchestrator.Dockerfile -t harness/orchestrator-scratch:dev .
# watch the output: the pip install step should say CACHED, reused from the 3.3 build

docker run --rm harness/orchestrator-scratch:dev
# expect: it exits with
#   RuntimeError: Set DOMAIN to the domain this orchestrator serves; available: ['sql']
```

That failure is the correct result: the image belongs to no domain until it is run with
one. `available: ['sql']` shows that `domains/` made it in.

---

## 3.6 Harness compose file: `infra/base/compose.yaml` — DONE

A harness file: the engines' half of every domain's compose deployment. It is never run
on its own. Overlays reuse its services with `extends`.

It has one service, `orchestrator-scratch`:

| Key | Value |
|---|---|
| `build.context` | `../..`, the repo root relative to this file |
| `build.dockerfile` | `infra/base/docker/orchestrator.Dockerfile`, relative to the context |
| `build.args` | `ENGINE: orchestrator_scratch` |
| `image` | `harness/orchestrator-scratch:dev` |
| `env_file` | `../../.env` |
| `environment` | `OLLAMA_HOST: http://host.docker.internal:11434` and `RUNS_DIR: ""` |
| `extra_hosts` | `host.docker.internal:host-gateway` |

Rules:

- No `depends_on`, no `ports`. What the orchestrator depends on, and where it is
  published, is the domain's wiring and belongs in the overlay. An extending service
  inherits a base's `depends_on`, so one here would point every domain at services it
  may not have.
- Nothing in it names a domain.

---

## 3.7 sql overlay: `infra/overlays/sql/compose.yaml` — DONE

A domain file: the sql domain's local deployment.

Top level: `name: harness-sql`.

Add an extension field `x-sql-env` with a YAML anchor (`&sql-env`). It holds what every
model-calling sql container needs on top of `.env`:

| Variable | Value |
|---|---|
| `OLLAMA_HOST` | `http://host.docker.internal:11434` |
| `RUNS_DIR` | `""` |
| `SQL_EXECUTOR_URL` | `http://executor:8000` |
| `SQL_SCHEMA_AGENT_URL` | `http://schema-agent:8000` |
| `SQL_QUERY_AGENT_URL` | `http://query-agent:8000` |

Four services. Use these names exactly: they are the hostnames in the URLs above, and
step 4 reuses them as Kubernetes Service names, which cannot contain underscores.

**`executor`**
- build: context `../../..`, dockerfile `domains/sql/tools/executor/Dockerfile`
- image: `harness/sql-executor:dev`
- no `env_file`, no `environment`, no `ports`

**`schema-agent`**
- build: context `../../..`, dockerfile `infra/base/docker/service.Dockerfile`,
  args `DOMAIN: sql`, `APP: domains.sql.agents.schema_agent.app:app`
- image: `harness/sql-schema-agent:dev`
- env_file: `../../../.env`
- environment: `*sql-env`
- extra_hosts: `host.docker.internal:host-gateway`
- depends_on: `executor` with `condition: service_healthy`

**`query-agent`**
- as `schema-agent`, with `APP: domains.sql.agents.query_agent.app:app`, image
  `harness/sql-query-agent:dev`, and no `depends_on`

**`orchestrator-scratch`**
- extends: file `../../base/compose.yaml`, service `orchestrator-scratch`
- environment: `<<: *sql-env` merged with `DOMAIN: sql`
- ports: `127.0.0.1:8100:8000`
- depends_on: `executor`, `schema-agent` and `query-agent`, each with
  `condition: service_healthy`

Rules:

- Only the orchestrator publishes a port, and only on `127.0.0.1`. Host port 8100 is
  where `SQL_SCRATCH_URL` in `.env` already points, so the CLI needs no change.
- Service URLs use the container port (8000), never a published one.

Check the merged result before building:

```bash
docker compose -f infra/overlays/sql/compose.yaml config
```

In the output, confirm:

- Every `build.context` is the absolute repo root, including the orchestrator's (resolved
  from the base file's location).
- `orchestrator-scratch.environment` has `DOMAIN: sql`, the three `http://<service>:8000`
  URLs, `OLLAMA_HOST: http://host.docker.internal:11434` and `RUNS_DIR: ""`. These
  override the `localhost` values the same keys have in `.env`.
- `executor` has no `environment` at all.

`config` prints the values it read from `.env`, including any API key. Don't paste that
output anywhere.

---

## 3.8 Run the stack — DONE

```bash
docker compose -f infra/overlays/sql/compose.yaml up --build --wait
```

`--wait` returns once every container reports healthy, and fails if one doesn't. The
first build takes a few minutes; later builds reuse the dependency layers.

```bash
docker compose -f infra/overlays/sql/compose.yaml ps
# expect: four services, all "(healthy)"; only orchestrator-scratch shows a published port:
#   127.0.0.1:8100->8000/tcp

curl http://localhost:8100/healthz
# expect: {"status":"ok","service":"orchestrator_scratch"}

python cli.py --target scratch "How many customers are from Brazil?"
# expect: the SQL, a blank line, an answer saying 5 (wording varies); exit code 0
```

---

## 3.9 Verify from the logs and the images — DONE

One run now spans four containers. Rebuild it from their combined stdout, from the repo
root with the venv active:

```bash
docker compose -f infra/overlays/sql/compose.yaml logs --no-log-prefix | python -c "
import sys
from core.events import parse_event
events = sorted((parse_event(l) for l in sys.stdin if l.startswith('{')), key=lambda e: e.ts)
for e in events:
    print(f'{e.run_id[:8]}  {e.service:22s} {e.type:15s} seq={e.seq}')
"
```

Expect, for the run's `run_id`:

```
<run_id>  orchestrator_scratch   run_start       seq=1
<run_id>  schema_agent           http_call       seq=None     # its GET /schema to the executor
<run_id>  schema_agent           model_call      seq=None
<run_id>  orchestrator_scratch   http_call       seq=2
<run_id>  query_agent            model_call      seq=None
<run_id>  orchestrator_scratch   http_call       seq=3
<run_id>  executor               tool_call       seq=None
<run_id>  orchestrator_scratch   http_call       seq=4
<run_id>  orchestrator_scratch   route_decision  seq=5
<run_id>  orchestrator_scratch   model_call      seq=6
<run_id>  orchestrator_scratch   run_end         seq=7
```

- Every JSON line parses with `parse_event`.
- One `run_id` appears across all four services.
- Only the orchestrator numbers its events.

`docker compose logs` does not interleave containers in time order, hence the sort on
`ts`. Lines that don't start with `{` are uvicorn's own logs.

Then check what is in the images and how they run:

```bash
C="docker compose -f infra/overlays/sql/compose.yaml"
$C exec executor python -c "import httpx"        # expect: ModuleNotFoundError
$C exec executor python -c "import anthropic"    # expect: ModuleNotFoundError
$C exec schema-agent ls -A                        # expect: core  domains  requirements*.txt (no data/, no .env)
$C exec orchestrator-scratch ls -A domains        # expect: __init__.py  sql
docker top harness-sql-orchestrator-scratch-1     # expect: one uvicorn process, UID 10001

time $C down
# expect: a few seconds. Much longer means a shell is PID 1 and swallowing SIGTERM (3.3, CMD).
```

---

## 3.10 Makefile targets — DONE

Add to the `Makefile`:

- `DOMAIN ?= sql`, so `make up` works now and `make up DOMAIN=docs` will later.
- A variable holding `docker compose -f infra/overlays/$(DOMAIN)/compose.yaml`.
- These targets, all added to `.PHONY`:

| Target | Runs |
|---|---|
| `up` | `… up --build --wait` |
| `down` | `… down` |
| `ps` | `… ps` |
| `logs` | `… logs -f` |

Verify: `make up`, `make ps`, `python cli.py --target scratch "How many customers are from Brazil?"`, `make down`.

---

## 3.11 CLAUDE.md — DONE

- Milestones: mark 3 ✅.
- The domain contract tree:
  - the `tools/<tool>/` line gains "+ a Dockerfile when the tool needs more than Python
    packages (the sql executor bakes in its database)";
  - the `infra/overlays/<name>/` line becomes "its deployment: `compose.yaml` now,
    kustomize from milestone 4".
- Repo layout, under `infra/`: `base/docker/` (the generic `service` and `orchestrator`
  Dockerfiles), `base/compose.yaml` (the engines, extended by every overlay),
  `overlays/<domain>/compose.yaml`.

---

## Done when

```bash
pytest                                                          # still green; no Python changed
ruff check .                                                    # clean
docker compose -f infra/overlays/sql/compose.yaml up --build --wait   # four containers healthy
python cli.py --target scratch "How many customers are from Brazil?"  # correct; exit 0
grep -rni sql infra/base .dockerignore                          # no output: the harness files name no domain
```

- Events from all four containers share one `run_id`, and the orchestrator's events are
  numbered 1–7.
- The executor image has no `httpx` and no provider SDK. Only the executor image contains
  `chinook.db`. No image contains `.env`.
- Every container runs as uid 10001 with uvicorn as PID 1. `down` takes seconds.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Build: `"/data/chinook.db": not found` | The database was never downloaded, or `.dockerignore` excludes it | `make db`; remove any `data/` or `*.db` line from `.dockerignore` |
| Build fails at the DOMAIN/APP check | Missing `build.args` in the overlay | Add both args to the service's `build` |
| `orchestrator-scratch` exits: `Set DOMAIN …` | The overlay's environment lacks `DOMAIN: sql` | Add it (3.7) |
| A container stays "starting", then turns "unhealthy" | The app crashed at import, or uvicorn isn't listening on 8000 | `docker compose … logs <service>` shows the traceback |
| `/ask` returns 500; logs show `ConnectError` to `localhost:8000` | A URL from `.env` reached the container: `localhost` is the container itself | Check the `x-sql-env` overrides in `config` output |
| `ConnectError` to `host.docker.internal:11434` | Ollama isn't running, or only listens on loopback (Linux) | Start Ollama. On Linux, run the Ollama server with `OLLAMA_HOST=0.0.0.0`. |
| `PermissionError: … 'runs'` | `RUNS_DIR=runs` from `.env` reached a container; uid 10001 can't create `/app/runs` | `RUNS_DIR: ""` in the service's environment |
| `Bind for 127.0.0.1:8100 failed: port is already allocated` | A step-2 `uvicorn` is still running | Stop it, or `docker ps` to find the container holding the port |
| A code or prompt change doesn't show up | The containers run the image you last built | `up --build`, or `make up` |
| Git Bash turns `/app` into `C:/Program Files/Git/app` | MSYS path conversion of absolute paths | Use paths relative to `/app` (as above), or prefix the command with `MSYS_NO_PATHCONV=1` |
