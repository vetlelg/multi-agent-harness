# Step 3 — What it's for

Companion to `step-3-instructions.md`. That file says what to do. This one says why.

---

## What Step 3 achieves

The four services, their code and their answer are the same as in step 2. What changes is
how they run. Each service now runs from an image, in its own container, with its own
filesystem and its own `localhost`, and reaches the others over a network by name. No
Python changes. What gets tested is everything the code silently relied on: that all four
processes shared one disk, one `.env`, one working directory and one loopback interface.

That is why compose comes before Kubernetes. Packaging bugs surface here one at a time:
a file missing from an image, a URL that says `localhost`, a process that can't write
where it expects. `docker compose logs` is all you need to find them. Step 4 then changes
only the scheduler. The images, service names, ports and environment variables stay the
same.

---

## Where each file lives, and why there

| File | Owner | Why it belongs there |
|---|---|---|
| `.dockerignore` | harness | One build context, the repo root, serves every image |
| `infra/base/docker/service.Dockerfile` | harness | Every agent in every domain has the same shape: Python, `core/`, one domain |
| `infra/base/docker/orchestrator.Dockerfile` | harness | One image per engine, with every domain inside |
| `infra/base/compose.yaml` | harness | How to run an engine in a container; the same for every domain |
| `domains/sql/tools/executor/Dockerfile` | domain | Only the sql domain knows its executor needs a database file |
| `infra/overlays/sql/compose.yaml` | domain | Which services sql has, and how they connect |

This is the acceptance criterion from CLAUDE.md applied to packaging. Adding a domain
later (docs and ops are deferred) means writing `infra/overlays/<name>/compose.yaml`,
plus a Dockerfile for any tool server that needs more than Python packages.
No harness file changes. The `grep` in "Done when" checks that the harness files name no
domain.

Two sql-specific things remain in harness territory, both from before this step: the
`make db` target and the root `data/` directory. `DOMAIN ?= sql` in the Makefile is a
default, not a dependency; `make up DOMAIN=docs` needs no edit.

---

## 3.1 The build context and `.dockerignore`

**Why the build context is the repo root.** Every image needs `core/`, and a Dockerfile
can only `COPY` from inside its context. The root is the only directory that contains
`core/`, `domains/` and the requirements files. So every image builds from the root, and
`-f` (or `dockerfile:` in compose) chooses which Dockerfile to use.

**Why it is not a copy of `.gitignore`.** The two files answer different questions:
`.gitignore` lists what must not be versioned, `.dockerignore` what must not reach an
image. They agree on `.env` and the caches. They disagree on `data/`: the database is not
versioned (`make db` downloads it and checks its hash), but it must be in the executor
image. Copy `.gitignore` over and the executor build breaks. They also disagree on
`tests/`, which is versioned but has no place in an image.

**Why it matters even though no Dockerfile copies `.env`.** BuildKit only transfers the
files a `COPY` asks for, so today the ignore file mainly keeps `tests/` and `__pycache__/`
out of `COPY domains/`. Its real job is defence. The day someone writes `COPY . .`, the
API key stays out of the image. A secret copied into a layer stays in the image history
even if a later layer deletes it. `**/evals` is there ahead of time because `evals/` is
part of the domain contract, and adding a domain must not mean editing a harness file.

---

## 3.2 What every image shares

**Why pin the base image by digest.** A tag is a pointer that moves: `3.12.15-slim` is
re-pushed whenever Debian ships security fixes. A digest names exact bytes. CLAUDE.md's
"pin whatever you install" applies to the OS layer as much as to pip. The cost is that a
security update becomes a deliberate edit: re-run `imagetools inspect` and bump the
digest. That trade suits a project that wants to know exactly which versions produced a
run. The tag in front of the digest is for humans; Docker uses the digest.

**Why slim.** It is Debian with glibc, so pip installs the same manylinux wheels as on
any Linux machine, without build tools and without musl surprises. The image weighs
~250 MB with dependencies instead of ~1 GB for the full image.

**Why requirements are copied before source.** Docker caches each layer and reuses it
until something it depends on changes. Installing from the requirements files alone makes
the slow layer (pip) depend only on those files. Editing a prompt or a step then
invalidates only the cheap `COPY` layers after it. Reverse the order and every code change
reinstalls every package. That is why the first build takes minutes and later ones take
seconds.

**Why the shared prefix, and why `DOMAIN`, `APP` and `ENGINE` come after it.** This is
the subtle one. BuildKit gives each instruction a cache key made from the instruction and
everything before it. Two Dockerfiles that begin with identical text therefore produce
identical layers, and the second build reuses the first's pip install. But a build arg is
visible to every later `RUN` as an environment variable, so its value becomes part of
those `RUN`s' cache keys. Declare `APP` before `pip install` and the schema agent and the
query agent each get their own copy of the same ~125 MB of packages, installed twice.
Declared after it, the orchestrator and both agents share every layer through `useradd`.
That was checked by comparing their layer digests. The cost is that a missing `DOMAIN`
fails after the (cached) install rather than before it.

**Why `--no-cache-dir`, `PYTHONDONTWRITEBYTECODE` and `PYTHONUNBUFFERED`.**
- pip's download cache is dead weight in a layer that will never run pip again.
- Bytecode files would be written into a filesystem that is thrown away, by a user who
  isn't allowed to write there.
- Unbuffered output matters because stdout is now the log. `emit` already flushes, but
  uvicorn's lines and, above all, the traceback of a crash would otherwise sit in a
  buffer that dies with the process.

**Why `0.0.0.0`.** Uvicorn binds `127.0.0.1` by default, and inside a container that is
the container's own loopback. This failure is nasty because it hides. The health check
runs inside the same container, so it passes: the service reports healthy, and every
other container gets "connection refused". `0.0.0.0` means every interface, including the
one on the compose network.

**Why port 8000 everywhere.** In local dev the four processes shared one network
interface, so they needed 8000, 8010, 8020 and 8100. Each container has its own network
namespace, so the ports cannot collide. One port means one `EXPOSE`, one health check and
one `CMD` shape, and URLs that differ only by hostname (`http://<service>:8000`). That is
exactly the shape Kubernetes Service DNS gives you in step 4.

**Why a non-root user, and why a numeric uid.** A compromised process (through a
dependency, say) gets the privileges of its user. Root in a container is not root on the
host, but it is closer than it needs to be. As uid 10001, the server can read its code
and the database but cannot change them: `COPY` leaves files owned by root, whenever the
user was created. The uid is numeric in `USER` for Kubernetes' sake: a pod with
`runAsNonRoot: true` refuses an image whose user is a name ("image has non-numeric user
(app), cannot verify user is non-root"). A fixed uid above the system range also behaves
predictably once volumes are mounted.

**Why the health check is Python.** Slim ships no `curl` or `wget`, and installing one for
a one-line probe adds a package and something more to patch. The interpreter is already
there. `urlopen` raises on a refused connection or a non-2xx status, so the exit code is
right without any parsing. `--start-interval=1s` probes every second during the start
period, so `--wait` returns as soon as a service is up instead of on the next 10-second
tick. The probe hits the `/healthz` that `core.service.create_app` gave every service in
step 2. Written once, it now serves every image, and the Kubernetes probes in step 4 will
use it too (Kubernetes ignores `HEALTHCHECK` and runs its own probes).

**Why PID 1 must be uvicorn.** `docker stop` sends SIGTERM to PID 1, waits 10 seconds,
then sends SIGKILL. The kernel ignores any signal that PID 1 has no handler for, and `sh`
has none for SIGTERM. Nor does it forward the signal to its child. Debian's `sh` (dash)
also does not replace itself with a lone `-c` command: that was checked in this base
image, and PID 1 stays `sh`. Without `exec`, uvicorn therefore never hears the signal and
is killed mid-request ten seconds later. With `exec`, uvicorn becomes PID 1, receives
SIGTERM, finishes in-flight requests and exits. `docker compose down` shows the
difference: about three seconds for the whole stack. In Kubernetes the same mechanics
decide whether a rolling update drops requests.

---

## 3.3 The agent image

**Why the harness owns a generic Dockerfile.** An agent is pure Python plus `core/`. The
only differences between domains are which package to copy and which app to serve, so the
harness provides the Dockerfile and the domain supplies two build args. A docs agent is a
new compose entry, not a new Dockerfile. `REQUIREMENTS` lets the same file serve a tool
server that needs nothing but its Python packages, built from `requirements-tool.txt`.

**Why one image per service, not one per domain.** You could build a single `sql-agents`
image and choose the app with a command override. Baking `APP` in makes each image
self-describing: `docker run harness/sql-schema-agent:dev` starts the schema agent, and a
Kubernetes manifest names an image without restating its command. Thanks to the shared
prefix the extra images cost almost nothing; they differ only in their last few small
layers.

**Why the build fails on an empty arg.** Without the check, `DOMAIN` unset makes
`COPY domains/${DOMAIN}/` into `COPY domains//`. That copies every domain, with no error,
into an image that is supposed to hold one. The check turns a silent breach of "agent
images carry their own domain only" into a build failure that says what is missing.

**Why `APP` is copied into an `ENV`.** Build args exist only while the image is built. The
`CMD` runs later, in a container, where only `ENV` values survive.

---

## 3.4 The executor image

**Why the executor brings its own Dockerfile.** Tool servers are where domain-specific
runtime needs live. Today that is a database file. For a future ops domain it would be a
`kubectl` binary; for docs, a search index. A generic Dockerfile can't
anticipate those needs, and shouldn't try. Agents never have such needs, which is why
they share one.

**Why the database is not in the agent images.** Step 1 made the executor the only
service with database access, and argued that a single entry point makes the boundary "a
fact rather than a convention". A database file inside the agent images would be readable
by code that has no business reading it, and the boundary would become a convention again.
Moving the database into `domains/sql/` would have let the generic Dockerfile copy it,
but into every sql image. Hence a separate Dockerfile.

**Why the database is baked in at all.** CLAUDE.md's decision: the data is small, sample
and read-only. Baking it in means no seed jobs, no volume to provision, and no way for
dev and cluster to disagree about the data. The image tag now versions the code and the
data together.

**Why `requirements-tool.txt`, and what the failing `import httpx` proves.**
`test_tool_servers_need_only_requirements_tool` checks statically that nothing the
executor imports needs httpx or a provider SDK. The executor image is built from that
file alone, so its starting and answering `/execute` is the runtime proof of the same
claim. `import httpx` failing inside it shows the image really is that small. A guard
that enforces "never trust the model" runs better without the model's SDK installed next
to it.

---

## 3.5 The orchestrator image

**Why it contains every domain and no `DOMAIN`.** CLAUDE.md: one image per engine, with
the domain picked at startup. The image is the engine, and the engine knows no domain
until it runs. A docs deployment and an sql deployment run the same image with different
environment. Step 2's `orchestrator_scratch/app.py` already refuses to start without
`DOMAIN` and lists what is available. In a container that refusal becomes a crashed
container with a readable reason, which is the right failure. A default domain baked into
the image would be wrong in every deployment but one.

**Why `ENGINE` is a build arg.** At milestone 9 the LangGraph engine needs the same image
with `ENGINE=orchestrator_lg` and `REQUIREMENTS=requirements-lg.txt`, and no new
Dockerfile.

---

## 3.6–3.7 Compose: base and overlay

**Why split compose into a base and an overlay.** Step 4 uses kustomize: a harness base
(the orchestrator) plus a per-domain overlay (the domain's services and patches). The
compose files rehearse that split. The harness defines how its engine image is built and
run, the overlay says "run it with `DOMAIN=sql`, wired to my services", and `extends`
joins them. Step 4 then becomes a translation, not a redesign. Relative paths in the base
(`../..`, `../../.env`) resolve from the base file's location, not the overlay's. That is
why the `config` check looks at the orchestrator's build context.

**Why the base has no `depends_on`.** An extending service inherits it (checked against
Compose v5), so a dependency in the base would point every domain's orchestrator at
services that only one domain has. Dependencies and ports are wiring, and wiring belongs
to the domain.

**Why it is configured with environment variables, not new code.** The containers use the
same settings classes as local dev, with different values:
- `env_file` hands each container the values from `.env` at run time, including the API
  key. Secrets reach the process without ever being in an image.
- `environment` then overrides the values that meant something else on your machine.
  Compose gives `environment` precedence over `env_file`.
- Inside the container, pydantic-settings looks for `/app/.env`. There is none (it was
  never copied and is excluded anyway), so environment variables are the only source.

**Why `localhost` had to go.** In a container `localhost` is the container itself. Every
`http://localhost:8010` in `.env` would have pointed each service at itself. Compose runs
a DNS server on the project network that resolves service names, so `http://schema-agent:8000`
works anywhere on that network. The names use hyphens because Kubernetes Service names
must be DNS labels, without underscores. Choosing them now keeps the URLs identical in
step 4.

**Why `host.docker.internal`, and `extra_hosts` with it.** Ollama runs on your Windows
host, not in the stack. Docker Desktop resolves `host.docker.internal` to the host;
Docker Engine on Linux does not unless told to. `host.docker.internal:host-gateway` makes
the same file work on both. On this machine the default Ollama install (listening on
loopback) was reachable this way from inside the containers.

**Why `RUNS_DIR` is blank in every container.** There is a practical reason and a
fundamental one:
- The practical reason: `.env` says `RUNS_DIR=runs`, and uid 10001 cannot create
  `/app/runs`. The first `emit` would raise, and every request would fail.
- The fundamental reason: in local dev all four processes appended to the same
  `runs/<run_id>.jsonl` because they shared a disk. Containers have four separate disks,
  so a run file per container would hold a quarter of a run each.

The source of truth in containers is stdout, merged by `docker compose logs`, then
`kubectl logs`, then Loki in milestone 8. `core/config.py` already says this for the
cluster, and its `_blank_is_unset` validator is what makes `RUNS_DIR: ""` mean "off".

**Why the executor gets no `env_file`.** It has no model to call and no credentials to
use. Least privilege is cheap to state here: a service that never receives the API key
cannot leak it.

**Why only the orchestrator is published, and only on `127.0.0.1`.** It mirrors the
cluster, where only the entry point is reached from outside, by port-forward. The
executor stays reachable only by its neighbours. Docker publishes on every interface by
default, which on a laptop can mean the office network; `127.0.0.1` prevents that. Host
port 8100 is where `SQL_SCRATCH_URL` already points, which is why the CLI didn't change.
The CLI is a thin client over `/ask`, and it no longer cares whether the orchestrator is
a process or a container.

**Why `depends_on: service_healthy` and `--wait`, and what they don't do.** They order
startup, so the first request doesn't race a starting agent, and they give you one
command that either returns a working stack or fails. They don't keep anything running
afterwards, and Kubernetes has no equivalent: pods start in any order and readiness
probes gate traffic. The code already copes, because every inter-service call happens per
request rather than at import. The schema agent starts fine with no executor and only
needs one when a question arrives.

**Why the tag is `:dev`, not `:latest`.** In step 4 these locally built images are
imported into the cluster rather than pulled from a registry. Kubernetes defaults
`imagePullPolicy` to `Always` for `:latest` and would try, and fail, to pull them. Any
other tag defaults to `IfNotPresent`.

---

## 3.9 What the verification proves

**The logs check** is the first time the `run_id` correlation from step 1 is proven
across processes that share nothing (no disk, no loopback), from one merged stream. That
is how Loki will see it in milestone 8. It also shows the limit of sorting by `ts`. Here
every container reads the same host clock. Across cluster nodes, clocks drift by
milliseconds, which is the same gap that separates an agent's event from the
orchestrator's, so a sort can put them out of order. The orchestrator's `seq` orders its
own log of record. Ordering across services is the job of traces (milestone 8), which
record parent and child spans rather than trusting wall clocks.

**The image checks** turn CLAUDE.md sentences into facts:
- the executor has no httpx or provider SDK;
- only the executor carries the database;
- no image carries `.env`;
- the orchestrator image holds every domain;
- nothing runs as root;
- PID 1 is the server.

**The shutdown time** is the cheapest test of signal handling there is.

---

## 3.10 Makefile targets

`docker compose -f infra/overlays/sql/compose.yaml` is too long to type twenty times a
day. The targets are generic over `DOMAIN`, so adding a domain doesn't touch them.
`up` always passes `--build`: forgetting it is the classic "my change isn't showing up",
and with layer caching an unchanged build takes a second or two.

---

## What this sets up for Step 4

| In compose (step 3) | In Kubernetes (step 4) |
|---|---|
| Image `harness/<service>:dev` | The same image, imported into the cluster (`k3d image import`); `:dev` keeps the pull policy `IfNotPresent` |
| Service `schema-agent`, port 8000 | Service `schema-agent`, port 8000, so `http://schema-agent:8000` is unchanged |
| `environment` | `env` on the Deployment, or a ConfigMap |
| `env_file: .env` | A Secret, holding only the API keys |
| `HEALTHCHECK` | Readiness and liveness probes on the same `/healthz` |
| `depends_on` | Nothing; readiness probes plus per-request calls |
| `ports: 127.0.0.1:8100:8000` | `kubectl port-forward svc/orchestrator-scratch 8100:8000` |
| `extends`, base and overlay | kustomize base and overlay |
| `USER 10001` | `securityContext.runAsNonRoot: true` |
| `host.docker.internal` | The cluster's own name for the host (k3d: `host.k3d.internal`) |
| `RUNS_DIR: ""` | Unset; `kubectl logs` is the source of truth |

---

## What was checked

These instructions were run before you got them, on a throwaway `git archive` copy of
`build/step-3` with `data/chinook.db` and a `.env` set to `ollama:llama3.1:8b`. The copy
and its images were deleted afterwards. The `python:3.12.15-slim` base image is still in
your Docker, so your first build skips that pull.

- Docker Desktop 29.1.3 (Engine 29.1.3, Compose v5.0.1, Buildx 0.30.1), Windows host,
  Ollama on default settings.
- `config` resolved the base file's relative paths from the base, and the overlay's
  environment overrode the `localhost` values from `.env`.
- `up --build --wait`: all four containers healthy in about 30 seconds on the first build.
- `cli.py --target scratch "How many customers are from Brazil?"` returned
  `SELECT COUNT(*) FROM Customer WHERE Country = 'Brazil' LIMIT 50` and
  "5 customers are from Brazil.", exit code 0.
- Logs: 11 events under one `run_id`, from all four services, with the orchestrator's
  numbered 1–7.
- Layer digests: the orchestrator and both agents shared every layer through `useradd`,
  and pip ran once for all three.
- Every container ran as uid 10001, with a single uvicorn process. In the executor,
  `import httpx` and `import anthropic` failed. The schema agent's image had no `data/`.
- The orchestrator image without `DOMAIN` exited with the `RuntimeError` listing
  `['sql']`. The service Dockerfile without build args failed at the check.
- `down`: under 3 seconds.

Found during that run and folded into the instructions:
- `USER app` became `USER 10001`, for Kubernetes.
- The build args moved after the install, so the pip layer is shared.
- The log check sorts by `ts`, because `docker compose logs` doesn't interleave in time
  order.
- Container paths are relative, because Git Bash rewrites `/app`.
- `depends_on` in an extends base is inherited, not rejected, so the reason for keeping
  it out of the base is wiring, not a compose error.

Not checked:
- The Anthropic provider from inside the containers, since no key is configured. The key
  arrives through `env_file` like every other `.env` value.
- A Linux Docker host, where `host-gateway` and file ownership on bind mounts matter
  (Docker Desktop hides both).
- The retry routes in containers, which is milestone 7.
