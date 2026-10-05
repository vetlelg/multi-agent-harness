# Step 4 — What it's for

Companion to `step-4-instructions.md`. That file says what to do. This one says why.

---

## What Step 4 achieves

Same four images, same code, same answer as step 3. What changes is who runs them. In
step 3, Docker ran containers because a compose file said so. Now a scheduler runs them
because a set of declared objects says what *should* exist. Kubernetes keeps comparing
that declaration with what does exist, and acts on the difference. Delete a pod and
another appears. Change a ConfigMap and the pods roll. A container that fails its probe
is restarted.

Step 3 was designed so that this step would be a translation, not a redesign: one port
per container, service names that are valid DNS labels, numeric users, a `/healthz` in
every service, no state on local disk, stdout as the log. Every one of those decisions
gets used here. The parts that are actually new are those compose had no word for:
- namespaces;
- an admission policy that refuses insecure pods;
- configuration and secrets as cluster objects;
- probes the platform acts on;
- a way in (port-forward) that doesn't publish anything.

---

## Where each file lives, and why there

| File | Owner | Why it belongs there |
|---|---|---|
| `infra/base/k3d.yaml` | harness | One local cluster serves every domain |
| `infra/base/namespace.yaml` | harness | The security policy every domain runs under |
| `infra/base/orchestrator-scratch.yaml` | harness | The engine runs the same way for every domain |
| `infra/base/kustomization.yaml` | harness | How the engine reaches the host's Ollama: the same for every domain |
| `infra/overlays/sql/*.yaml` (3 services) | domain | Only sql knows it has an executor and two agents |
| `infra/overlays/sql/kustomization.yaml` | domain | Its namespace, its service URLs, its models |
| `Makefile` targets | harness | Generic over `DOMAIN`; derive everything from the overlay's files |

That is the acceptance criterion from CLAUDE.md applied to the cluster. A future docs
domain writes `infra/overlays/docs/` (a `kustomization.yaml`, one manifest per service)
and runs `make k8s-up DOMAIN=docs`. It lands in namespace `harness-docs`, under the same
policy, with its own `orchestrator-scratch` from the same base. No harness file changes.
`grep -rni sql infra/base` checks that the harness side still names no domain.

---

## 4.0 Why k3d, and why these versions

**Why k3d.** CLAUDE.md allowed k3d or minikube. k3d runs k3s (a complete, certified
Kubernetes in one binary) inside Docker containers. Nothing new has to be virtualised:
Docker Desktop is already running, and the cluster comes up in about 20 seconds. The
cluster is described by a file in the repo, so it is versioned like everything else.
k3d also brings three things later milestones need: `k3d image import` for local images,
`host.k3d.internal` for the host's Ollama, and a local-path storage provisioner for the
PersistentVolumeClaim the LangGraph checkpointer needs at milestone 10.
Docker Desktop's built-in Kubernetes was the other obvious option. You can't describe it
in a file, there is only one per machine, and its version moves with Docker Desktop
updates.

**Why k3s v1.35, not the newest.** k3s's stable channel is at v1.36.5 and the newest
release is v1.37.1. kubectl supports API servers one minor version older or newer than
itself. Docker Desktop's kubectl is 1.34.1, so v1.35 is the newest server it is supported
against. k3d 5.9.0's own default is also a 1.35 (v1.35.5). The pin takes 1.35's latest
patch, v1.35.9. When Docker Desktop's kubectl moves to 1.35, the cluster can move to 1.36.

**Why pinned by digest.** For the same reason as step 3's base image: a tag is a pointer
and a digest is bytes. The Kubernetes version is now part of what produced a run.

**Why kustomize comes from kubectl.** kubectl has embedded kustomize since 1.14
(`kubectl apply -k`, `kubectl kustomize`). One tool fewer to install and pin. The
embedded version (v5.7.1) moves with kubectl.

---

## 4.1 The cluster

**Why one cluster for every domain, with a namespace each.** A cluster per domain would
be the compose model (`name: harness-sql`) taken literally, at the cost of a control
plane per domain. Namespaces give the isolation that matters here, cheaply:
- **Names.** Every domain gets a Service called `orchestrator-scratch` without collision,
  and `http://executor:8000` resolves within the caller's own namespace.
- **Secrets.** Secrets are namespaced, so an sql pod cannot reference a docs Secret.
- **Teardown.** Deleting the namespace deletes everything in it.

The namespace name `harness-<domain>` mirrors the compose project name. It is also the
one convention the Makefile relies on.

**Why one node, and no agents.** One k3s server also runs workloads. Nothing in this
system needs to survive the loss of a node. A second node would double the import time
(images are imported per node) and change nothing that this step tests.

**Why the API server is on `127.0.0.1`.** k3d publishes the API server on `0.0.0.0` by
default, which on a laptop means every network you join. Client certificates still guard
it, but there is no reason to offer it. Step 3 published the orchestrator on loopback
only; this is the same rule for a more powerful port.

**Why no load balancer, Traefik or ServiceLB.**
- k3d's load balancer is an nginx container in front of the servers. It exists to spread
  API traffic over several servers and to carry port mappings. With one server and no
  mappings it has nothing to do.
- Traefik (an ingress controller) and ServiceLB (which backs `type: LoadBalancer`
  Services with host ports) are how a k3s cluster would publish services. This system
  publishes nothing: the only way in is a port-forward.

Disabling them means fewer pods, fewer open ports, and less to read in
`kubectl get pods -A`. Metrics-server stays, for `kubectl top`. The local-path
provisioner stays too: milestone 10 needs it.

**Why `host.k3d.internal`.** Ollama still runs on the host. k3d writes
`host.k3d.internal` into CoreDNS's host table when it creates the cluster. On Docker
Desktop it points at the host (192.168.65.254, the same address `host.docker.internal`
resolves to). It is the k3d counterpart of step 3's `host.docker.internal` plus
`extra_hosts`. Docker Desktop's own name happens to resolve inside pods too, but only on
Docker Desktop. k3d's name is the one k3d promises.

**Why k3d switches your kubeconfig context.** `kubectl` with no `--context` acts on the
current context. k3d sets that to `k3d-harness`, so the commands in the instructions are
short. The Makefile doesn't rely on it: it passes `--context k3d-harness` on every call,
so `make k8s-down` can never delete a namespace in some other cluster that happens to be
current.

---

## 4.2 What every Deployment shares

**Why a Deployment and a Service per service.** A Deployment declares "one pod like this
should be running". It replaces failed pods and rolls out changes. A Service gives those
pods a stable name and virtual IP that outlive any single pod. Pods come and go with new
IPs; `executor` stays. Service DNS is what makes `http://executor:8000` work in the
cluster exactly as compose's network DNS made it work in step 3. That is why step 3 chose
hyphenated names and one port.

**Why one label, and why not more.** A Deployment finds its pods, and a Service its
endpoints, by label selector. `app.kubernetes.io/name` is the recommended key, and one
label is enough to identify a service within its namespace. Selectors are immutable once
a Deployment exists. A label that later had to change in the selector would mean
deleting and recreating the Deployment. That is the classic trap of kustomize's
`commonLabels`, which writes into selectors. One stable label avoids it, and
`-l app.kubernetes.io/name` (the key exists) selects every pod in the domain at once, as
the log check does.

**Why `imagePullPolicy: Never`.** Step 3's explanation expected `IfNotPresent`, which
the `:dev` tag gives by default. During the trial run it turned out to be a supply-chain
hole. `harness/sql-executor:dev` is short for `docker.io/harness/sql-executor:dev`
(containerd lists it that way), and `docker.io/harness` is not an empty namespace: it
belongs to Harness, the CI/CD company, and has over 400 public repositories. With
`IfNotPresent`, a missing local image makes the node try to pull from that organisation.
Today that fails, and tomorrow it might not. `Never` states the truth about these images:
they come from `k3d image import` and from nowhere else. A missing one fails with
`ErrImageNeverPull`, which names the problem, instead of `ImagePullBackOff` after a
round-trip to Docker Hub.

**Why probes, and why two.** Kubernetes ignores the image's `HEALTHCHECK`; the kubelet
runs its own probes. They act on the same `/healthz` that `core.service.create_app` gave
every service in step 2.
- A failing **readiness** probe takes the pod out of the Service's endpoints. No traffic,
  no restart. It is what makes a rollout safe: the new pod gets traffic only once it
  answers.
- A failing **liveness** probe restarts the container. That is the remedy for a wedged
  process.

`/healthz` is deliberately shallow: it checks that the process can answer, not that
Ollama or the executor can. A deep check would turn a dependency outage into restarts of
healthy pods. If the executor went down, every agent would fail its liveness probe and
restart in a loop, fixing nothing. No startup probe either: the services answer within
about two seconds of starting.

**Why these resource numbers, and no CPU limit.**
- **Requests** are what the scheduler reserves. They were set from measurement: 37–46 MiB
  per pod after a full run (`kubectl top`), so 64Mi.
- **The memory limit** is a hard ceiling. Above it the container is OOM-killed. 256Mi is
  generous headroom for a Python process, and for the executor's 64Mi tmpfs, which counts
  against it.
- **CPU** is left unlimited on purpose. A CPU limit is enforced by throttling, even when
  the node is idle, and shows up as unexplained latency. A CPU request alone guarantees a
  share without capping bursts. The model calls run in Ollama, outside the cluster.
  Inside, these services mostly wait.

**Why this security context.** Step 3 built images that run as uid 10001. This step makes
the cluster insist on it.
- `runAsNonRoot: true` makes the kubelet refuse to start a container whose user is root,
  or a name it can't verify. That is why step 3 wrote `USER 10001`, not `USER app`.
  `runAsUser` is left unset: the image already declares the user, and repeating it would
  mean two places to keep in sync.
- `allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]` remove setuid tricks
  and the residual root-like capabilities a container gets by default.
- `seccompProfile: RuntimeDefault` filters dangerous syscalls.

Together these are exactly what the `restricted` Pod Security Standard demands (4.3).
`readOnlyRootFilesystem` goes beyond it. The image is immutable, so nothing in the
container can be rewritten at run time, including the code and the database. The trial
run showed what that costs (4.4).

**Why `automountServiceAccountToken: false`.** By default every pod gets a token for the
Kubernetes API, mounted as a file. None of these services talks to the API. A token they
don't need is a credential that a compromised dependency could use. The deferred ops
domain will be the first that needs one, and it will get its own ServiceAccount with a
read-only Role. That is a deliberate grant, not a default.

**Why `enableServiceLinks: false`.** Before cluster DNS existed, Kubernetes advertised
Services through environment variables, and it still does by default. Every pod gets
`EXECUTOR_SERVICE_HOST`, `EXECUTOR_PORT=tcp://10.43.…:8000` and so on, for every Service
in the namespace. Nothing here reads them; discovery is by DNS. They also share a
namespace with the harness settings, which have no prefix. A future Service named after a
setting would inject a variable with the same name. That is a known class of bug: a
Service called `redis` sets `REDIS_PORT=tcp://…`, which breaks any program expecting a
port number there. Off, the pod's environment is what the manifests say, plus the
`KUBERNETES_*` variables, which are always injected.

**Why ClusterIP Services only.** ClusterIP (the default) is reachable only inside the
cluster. It is step 3's "only the orchestrator is published" taken one step further:
nothing is published, and you reach the orchestrator by port-forward. NodePort or
LoadBalancer would open ports on the host. The executor in particular stays reachable
only by its neighbours.

---

## 4.3 The base

**Why the Namespace is in the base, with the policy on it.** Pod Security admission is
built into Kubernetes and configured by namespace labels. `enforce: restricted` makes the
API server reject any pod in the namespace that doesn't meet the restricted standard.
Putting the Namespace in the harness base gives every domain that policy without asking.
That is CLAUDE.md's "cross-cutting behaviour goes through the harness" applied to
security.
The base cannot know the domain's name, so it ships a placeholder. Kustomize's namespace
transformer renames a Namespace object to the overlay's `namespace:` (checked: the
rendered output contains only `harness-sql`).

**Why `warn` as well as `enforce`.** Enforcement applies to pods, and a Deployment is not
a pod. `kubectl apply` of a non-compliant Deployment succeeds. Its ReplicaSet then fails
to create pods, which is visible only in events and as a Deployment stuck at 0/1. `warn`
checks the pod template inside workloads at apply time and prints the violations right
there in your terminal. The trial run's `--dry-run=server` showed both: the warning on
`apply`, and the `Forbidden` on a bare pod.

**Why the orchestrator reads a ConfigMap called `domain-env`.** The orchestrator image
belongs to no domain until it is run with one (step 3, 3.5). In compose, the overlay
added `DOMAIN: sql` to the extended service. In Kubernetes, the base names a ConfigMap
that every overlay must provide, and the overlay fills it. No patch is needed, so the
base's Deployment stays exactly as written. An overlay that forgets the ConfigMap gets a
pod stuck in `CreateContainerConfigError` with "configmap not found", which is the
cluster's version of step 3's "Set DOMAIN …" crash.

**Why a generator, and what the hash buys.** `configMapGenerator` appends a hash of the
content to the name (`domain-env-4db8h44644`) and rewrites every reference to match. It
does so across the base/overlay boundary too: the orchestrator's reference is in the base,
the generator in the overlay, and the rendered output agrees. That matters because pods
read `envFrom` once, at start. Edit a plain ConfigMap and running pods never see the
change. With a generator, a new value means a new name, so the Deployment's pod template
changes, so Kubernetes rolls the pods. Configuration changes deploy themselves, and the
old ConfigMap is still there for a rollback.

**Why `OLLAMA_HOST` is in the base.** Where the model server is depends on the cluster,
not on the domain. Every model-calling pod in every domain needs the same answer. The
harness says it once, in `harness-env`, and domain pods reference it by name.

**Why the base has no `namespace:`.** The base doesn't know where it will live. Each
overlay places it. That is the same reason `infra/base/compose.yaml` has no ports or
`depends_on`.

---

## 4.4 The overlay

**Why the overlay sets the models.** In step 3, models came from your `.env` through
`env_file`. The cluster doesn't read your `.env`. It runs what the manifests declare, and
which model each role uses is part of that declaration. It is versioned, it shows in a
diff, and it is the same for anyone who deploys the overlay. This overlay is a local
development deployment: its pods reach the host's Ollama, so its models are Ollama
models, and it runs without any API key. Moving it to Claude is a three-line change,
plus a key in `.env` that `make k8s-up` carries into the Secret.

**Why nothing else from `.env` reaches the cluster.** In step 3, `env_file` delivered all
of `.env`, and the overlay then had to override every value that meant something else in
a container: the `localhost` URLs, `OLLAMA_HOST`, `RUNS_DIR=runs`. Forget one and a
container calls itself. Here only the API keys cross over (4.7). Everything else is
declared, so there is nothing to override and nothing to forget. `RUNS_DIR` is simply
unset, so no pod writes a run file, and `kubectl logs` is the source of truth, as
`core/config.py` has always said for the cluster.

**Why the executor gets no configuration.** As in step 3: it calls no model and holds no
credential. It doesn't receive `harness-env`, `domain-env` or the Secret. Its settings are
the defaults, and the database is baked in at the default path.

**Why the executor has a `/tmp`, and why in memory.** This was found during the trial
run. With the root filesystem read-only, ordinary queries worked, including step 6's
top-5-artists-by-revenue join. A query whose intermediate result outgrew SQLite's page
cache (a `DISTINCT` or `ORDER BY` over a few hundred thousand rows) failed with
`disk I/O error`. SQLite spills temporary B-trees to a file in `/tmp`, even for a
read-only database. The fix is a writable `/tmp` and nothing else writable.
- `medium: Memory` makes it a tmpfs. Its `sizeLimit` is a hard size, so a runaway query
  fails inside SQLite with `database or disk is full`, a classified error the route can
  handle. The trial run showed exactly that, with the pod untouched.
- A disk-backed `emptyDir` with a `sizeLimit` is enforced differently: the kubelet evicts
  the whole pod when usage exceeds it. That would kill every in-flight request to stop
  one query.
- The tmpfs counts against the container's memory limit, which is why that limit is
  256Mi. The executor's 5-second query timeout bounds how fast anything can fill it.

The agents and the orchestrator get no `/tmp`. Nothing in them writes, and the read-only
root proves it.

---

## 4.5 Rendering before applying

`kubectl kustomize` is `apply -k` without the apply. Reading its output once is the
fastest way to learn what kustomize does: namespaces stamped onto every object, the base
Namespace renamed, hashes on generated names, references rewritten. It is also the
fastest way to catch a mistake before the cluster does. In particular, it shows that no
Secret is in the rendered output, and so none is in git.

---

## 4.6 Images

**Why import instead of a registry.** A registry is how real clusters get images, and k3d
can run one (`k3d registry create`). For a single node it adds a container, a push step
and a naming scheme. `k3d image import` does `docker save` on the images and loads the
tarball into the node's containerd: about 12 seconds for all four, because shared layers
are saved once. The step-3 layer sharing pays off again here.

**Why compose is the build manifest.** The overlay's `compose.yaml` already says how to
build each image: Dockerfile, build args, tag. `docker compose config --images` lists the
tags. The Makefile builds and imports from that list, so it needs no knowledge of the
domain, and the cluster runs exactly the images compose runs. If a manifest names an
image the compose file doesn't build, `ErrImageNeverPull` says so.

**Why a re-import is not enough.** A running pod keeps the image it started with. Import
a new `:dev` and nothing changes until a pod is created again. `make k8s-up` therefore
always ends with `rollout restart`. It sets a timestamp annotation on each pod template,
which counts as a change, so each Deployment rolls. Content-addressed tags would make
restarts unnecessary (tools like Tilt and Skaffold do this). That is a fair upgrade once
deploys are frequent; for now, a restart costs about ten seconds.

---

## 4.7 The Secret

**Why only the keys.** CLAUDE.md: API keys live in `.env` locally and in a Kubernetes
Secret in the cluster, never in images or manifests. A Secret is the object type that
RBAC can fence off and that `kubectl describe` won't print. It is not encrypted by
default: the values are base64, which is an encoding. k3s stores them in its datastore
as-is unless started with secrets encryption. Keeping non-secret configuration out of
the Secret keeps it inspectable in a ConfigMap, and keeps the Secret small enough to
reason about.

**Why the `grep` is generic.** `^[A-Z0-9_]+_API_KEY=` picks up whichever provider keys
`.env` has, today and later, without the harness Makefile listing providers. That
mirrors `core/config.py`, which owns the credentials for every provider.

**Why a temporary file.** `kubectl create secret --from-env-file` needs a path.
`/dev/stdin` and `<(…)` are paths only to programs built for a POSIX layer. `kubectl.exe`
is a native Windows binary and, in the trial run, failed with
`error reading /proc/self/fd/0`. A file from `mktemp`, removed afterwards, works on
every platform.

**Why `create --dry-run=client -o yaml | apply -f -`.** `kubectl create` fails if the
object exists. The idiom renders the object locally and lets `apply` create or update
it, so the same command works on the first deploy and after a key rotation. After a
rotation the pods need a restart, since environment variables are read at container
start. `make k8s-up` restarts them anyway.

**Why the namespace is created first.** The Secret has to live in the namespace that
`apply -k` would otherwise create. Creating it beforehand (the same idiom) means the pods
find their Secret on the first try instead of failing and retrying. `apply -k` then adds
the pod-security labels to the existing namespace.

---

## 4.8 Applying

**Why `apply`, and why re-running it is safe.** `kubectl apply` is declarative: it sends
the desired state and the API server works out the difference. Re-applying an unchanged
overlay changes nothing, and every line says `unchanged`. Applying a changed overlay
changes only what changed. `make k8s-up` relies on this: it applies everything every
time.

**Why there is no `depends_on`.** The pods start in whatever order the scheduler picks.
Step 3's explanation predicted that this would be fine, and it was: every inter-service
call happens per request, not at import. A schema agent that starts before the executor
simply has no question to answer yet. Readiness probes stop traffic from reaching a pod
before it can answer.

**Why `rollout status`.** `apply` returns once the API server has accepted the objects,
not once pods are running. `rollout status` waits until each Deployment's newest
ReplicaSet is available, and fails after the timeout. It is the cluster's counterpart of
`compose up --wait`. A rollout replaces pods without a gap: by default a Deployment
starts the new pod, waits for its readiness probe, and only then stops the old one.

---

## 4.9 Port-forward

**What it is.** `kubectl port-forward` tunnels a local port through the API server and
the kubelet into one pod's network namespace. Nothing is published on the node, and the
local end binds to `127.0.0.1` only. It is the least exposure that still lets the CLI
reach `/ask`, which is why CLAUDE.md chose it for this milestone.

**Why it dies on a rollout.** `svc/orchestrator-scratch` is resolved to one pod when the
forward starts, and the tunnel is bound to that pod. Service load-balancing is not
involved. When the pod is replaced, the tunnel has nowhere to go. The forward doesn't
notice at once: it keeps listening, the next request gets an empty reply, and then it
exits with `lost connection to pod`. Both runs showed exactly this. Restarting the
forward picks the new pod. A real entry point (an Ingress or a LoadBalancer Service)
follows the Service, not a pod. This project doesn't need one yet.

**Why the CLI needed no change.** It is a thin client over `/ask`, pointed at
`localhost:8100`. Whether a process, a container or a pod answers there is not its
business. Steps 3 and 4 both kept that promise.

---

## 4.10 What the verification proves

**The log check** is step 3's check against a new source: four pods' stdout, read
through the API server. It is how Loki will collect it in milestone 8. One `run_id`
across four pods shows that correlation survived the move. The trial run also caught the
limit of timestamps directly: the executor's `tool_call` and the orchestrator's
`http_call` were stamped in the same millisecond, so their order in a sort depends on
which pod's logs were read first. Within one service, `seq` gives order. Across services,
only traces do (milestone 8).

**The hardening checks** turn the 4.2 table into facts:
- uid 10001;
- a root filesystem that refuses a write;
- a 64M tmpfs on `/tmp` and nothing else writable;
- no service-account token;
- no API key in the executor;
- the keys in the agents' environment;
- and the cluster refusing a pod that hasn't been hardened, with a message naming each
  missing field.

**The `/tmp` check** proves the volume earns its place. The same query fails without it.

**The self-healing check** is the first thing here that compose couldn't do. You delete a
pod, a new one with a new name and a new IP appears, and the next question still works,
because the orchestrator addresses the Service, not the pod. Milestone 10's resume test
(kill the LangGraph orchestrator mid-run, continue from its checkpoint) is this
experiment with state added.

---

## 4.11 The Makefile

- **`k8s-up` does everything, every time:** cluster if missing, build, import, Secret,
  apply, restart, wait. Each part is idempotent or cheap, so one target covers the first
  deploy, a code change, a prompt change, a config change and a key rotation. No sequence
  to remember.
- **The context is pinned** (4.1). The namespace and overlay derive from `DOMAIN`, as the
  compose targets do.
- **`k8s-down` deletes what `apply -k` created,** the namespace included, and the Secret
  with it. The cluster stays: recreating it is the slow part, and `cluster-delete` exists
  for when you want it gone.
- **`k8s-forward` runs in the foreground** because a port-forward is a session, not a
  service. It belongs in a terminal you can see die (4.9).

---

## Step 3's predictions, checked

Step 3's explanation ended with a table of what would carry over. How it went:

| Predicted | What happened |
|---|---|
| Same images, imported; `:dev` gives `IfNotPresent` | Same images, imported. `Never` instead: `docker.io/harness` is a real organisation (4.2). |
| Service `schema-agent`, port 8000, URL unchanged | Exactly. `http://<service>:8000` resolves by Service DNS within the namespace. |
| `environment` becomes `env` or a ConfigMap | ConfigMaps from generators (`harness-env`, `domain-env`), so changes roll the pods |
| `env_file: .env` becomes a Secret holding only the API keys | Exactly, and the rest of `.env` no longer needs overriding because it never arrives |
| `HEALTHCHECK` becomes readiness and liveness probes on `/healthz` | Exactly |
| `depends_on` becomes nothing | Nothing was needed |
| Published port becomes `kubectl port-forward` | Exactly; it binds to one pod (4.9) |
| `extends` becomes kustomize base and overlay | And more: the base also owns the namespace policy, and a contract ConfigMap replaces the per-domain patch |
| `USER 10001` becomes `runAsNonRoot: true` | Plus the full `restricted` standard, enforced by the namespace |
| `host.docker.internal` becomes `host.k3d.internal` | Exactly |
| `RUNS_DIR: ""` becomes unset | Exactly |

Not predicted: the read-only root filesystem, and the `/tmp` that SQLite turned out to
need.

---

## What this sets up

| Later | What it builds on |
|---|---|
| Milestone 5–6: evals | `pytest -m live` calls `/ask` through the same port-forward |
| Milestone 7: retry routes | `make k8s-logs` shows the `route_decision` events live, across pods |
| Milestone 8: observability | The Grafana stack goes in its own namespace. Loki collects the same stdout that `kubectl logs` reads. OTel spans replace the timestamp sort. |
| Milestone 9–10: LangGraph | A second Deployment in the base, `orchestrator-lg`, built with `ENGINE=orchestrator_lg`. Its SQLite checkpointer lives on a PersistentVolumeClaim from the local-path provisioner kept in 4.1, with `replicas: 1` and `strategy: Recreate`. Port 8200. |
| Deferred ops domain | Its tool server gets its own ServiceAccount and a read-only Role: the first pod with `automountServiceAccountToken: true`, as an explicit grant |

---

## What was checked

This step was prototyped in a scratch directory against a real k3d cluster before the
instructions were written. Then it was implemented in the repo and run again from
nothing.

- Docker Desktop 29.1.3, k3d 5.9.0 (winget), k3s v1.35.9+k3s1, kubectl 1.34.1 with
  kustomize v5.7.1, Ollama `llama3.1:8b` on the Windows host.
- Cluster creation took about 20 seconds. The API server was bound to `127.0.0.1` and no
  load-balancer container was created. `host.k3d.internal` resolved to 192.168.65.254,
  and Ollama answered from inside a pod.
- Kustomize resolved `domain-env` (generated in the overlay, referenced in the base) and
  `harness-env` (the other way round) to their hashed names, and renamed the base's
  Namespace to `harness-sql`.
- `apply -k`: four pods Running and Ready within about 7 seconds, admitted under
  `restricted`.
- The CLI through the port-forward returned
  `SELECT COUNT(*) FROM Customer WHERE Country = 'Brazil' LIMIT 50` and "There are 5
  customers from Brazil.", exit code 0, in about 30 seconds on CPU Ollama.
- Logs: 11 events under one `run_id` from four pods, with the orchestrator's numbered
  1–7. `tool_call` and `http_call seq=4` shared a millisecond.
- Pods ran as uid 10001 with a read-only root, no service-account token, and no API key
  in the executor. A bare `kubectl run` was rejected with the four missing fields named.
  A server-side dry-run of a bare Deployment printed the `warn` message.
- Memory after a run: 37–46 MiB per pod.
- Restarting the orchestrator broke the port-forward. The next request got an empty
  reply, and the forward exited with `lost connection to pod`.

The run from nothing, in the repo:
- `make cluster-delete`, then `make k8s-up`: cluster, cached build, import, Secret,
  apply and four rollouts in 38 seconds. A second `make k8s-up` took 17 seconds.
- `kubectl kustomize infra/overlays/sql`: 11 objects, every reference hashed, no Secret,
  four `imagePullPolicy: Never`. `grep -rni sql infra/base .dockerignore` printed nothing.
- `make k8s-forward`, then the CLI: the same SQL and answer, exit code 0. The log check
  gave the same 11 events.
- Every check in 4.10 passed as written: uid 10001, read-only root, a 64M tmpfs, no
  token, no key in the executor, `Forbidden` for a bare pod, 437875 rows from the `/tmp`
  query, and a deleted query-agent replaced within 2 seconds while the next question
  still worked.
- The key's path, with a dummy value put into the Secret by hand: after a restart, the
  schema agent's `settings.anthropic_api_key` was a `SecretStr` of the right length, and
  the executor's was `None`. The next `make k8s-up` put back the blank key from `.env`.
- `make k8s-logs` followed all four pods with prefixes. `make k8s-down` deleted the
  namespace with the Secret in it.
- `pytest` (142 passed) and `ruff check .` were clean.

Found during that run and folded into the instructions:
- `/tmp` for the executor. Large `DISTINCT` / `ORDER BY` subqueries failed with
  `disk I/O error` on a read-only root. With a 64Mi tmpfs, they succeeded, and an
  oversized one failed cleanly with `database or disk is full`.
- `imagePullPolicy: Never` instead of the predicted `IfNotPresent` (`docker.io/harness`
  exists).
- The Secret goes through a temporary file, because `kubectl.exe` can't read
  `/dev/stdin`.
- `kubectl logs -l` needs `--tail=-1`. With a selector it defaults to 10 lines per pod.
- `MSYS_NO_PATHCONV=1` for `kubectl exec` commands with absolute paths, as in step 3.

Not checked:
- Anthropic models in the cluster, since no key is configured. The key's path into the
  pods was checked with a dummy value (above); the provider call was not.
- A Linux Docker host, where `host.k3d.internal` resolves to the Docker network gateway
  and Ollama must listen on more than loopback.
- The cluster across a Docker Desktop restart. The node container's restart policy is
  `unless-stopped`, so it should come back with Docker. `k3d cluster start harness`
  covers the case where it doesn't.
