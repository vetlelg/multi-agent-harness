# Step 4 — Instructions

Exact actions only. For the reasoning behind any of it, see `step-4-explained.md`.

**Convention:** file sections give you names, fields, ordering and required behaviour.
They are specifications, not source. Exact strings are given only where the exact string
is the point: versions, digests, label keys, names that other files refer to, and
commands.

**Goal (milestone 4):** the sql domain runs in a local k3d cluster, deployed from a
kustomize base (`infra/base/`, the harness) plus an overlay (`infra/overlays/sql/`, the
domain). `python cli.py --target scratch "…"` answers through a `kubectl port-forward`
to the in-cluster orchestrator, with no change to the CLI.

**No Python changes in this step.** Every file is new except `Makefile` and `CLAUDE.md`.

**State of play:** 4.0–4.12 are done. Claude implemented them on request and ran every
check below on 2026-10-05. Step 4 is complete.

**Checked against:** Docker Desktop 29.1.3, k3d 5.9.0, k3s v1.35.9+k3s1, kubectl 1.34.1
(kustomize v5.7.1), Ollama `llama3.1:8b` on the Windows host. See "What was checked" at
the end of `step-4-explained.md`.

---

## 4.0 Before you start — DONE

- You are on `build/step-4`.
- Step 3 works: `make up` brings up four healthy containers. Then `make down`. Its
  orchestrator publishes host port 8100, the port the cluster's port-forward will use.
- Docker Desktop is running. Ollama is running: `curl http://localhost:11434/api/tags`
  lists `llama3.1:8b`.
- `kubectl` 1.34 or newer: `kubectl version --client`. Docker Desktop ships one (1.34.1,
  with kustomize v5.7.1 built in). You need no separate `kustomize`.
- Install k3d **5.9.0**, then open a new terminal so `PATH` picks it up:

  ```bash
  winget install --id k3d.k3d --version 5.9.0 --exact
  k3d version
  # expect: k3d version v5.9.0
  ```

---

## 4.1 The cluster: `infra/base/k3d.yaml` — DONE

A harness file. One local cluster runs every domain, each in its own namespace.

| Key | Value |
|---|---|
| `apiVersion` / `kind` | `k3d.io/v1alpha5` / `Simple` |
| `metadata.name` | `harness`. k3d names the kubeconfig context `k3d-harness`. |
| `servers` / `agents` | `1` / `0` |
| `image` | `rancher/k3s:v1.35.9-k3s1@sha256:ec9868c6a38d4e8c1869832fb5fd1eb8473c39794a0b44d2b952e7ba911951bc` |
| `kubeAPI.hostIP` | `"127.0.0.1"` |
| `options.k3d.disableLoadbalancer` | `true` |
| `options.k3s.extraArgs` | `--disable=traefik` and `--disable=servicelb`, each with `nodeFilters: [server:*]` |
| `options.kubeconfig` | `updateDefaultKubeconfig: true`, `switchCurrentContext: true` |

Get the digest yourself if the tag has moved:

```bash
curl -s https://hub.docker.com/v2/repositories/rancher/k3s/tags/v1.35.9-k3s1 \
  | python -c "import sys, json; print(json.load(sys.stdin)['digest'])"
# On 2026-10-05: sha256:ec9868c6a38d4e8c1869832fb5fd1eb8473c39794a0b44d2b952e7ba911951bc
```

Create it and look:

```bash
k3d cluster create --config infra/base/k3d.yaml
kubectl config current-context
# expect: k3d-harness
kubectl get nodes
# expect: one node, k3d-harness-server-0, Ready, VERSION v1.35.9+k3s1
kubectl -n kube-system get pods
# expect: coredns, local-path-provisioner, metrics-server. No traefik, no svclb.
kubectl -n kube-system get configmap coredns -o jsonpath='{.data.NodeHosts}'
# expect: a line "<ip> host.k3d.internal" (192.168.65.254 on Docker Desktop)
docker ps --filter name=k3d-harness --format '{{.Names}}  {{.Ports}}'
# expect: k3d-harness-server-0  127.0.0.1:<port>->6443/tcp. No 0.0.0.0, no serverlb container.
```

---

## 4.2 What every Deployment shares — DONE

This step writes four manifest files: one in the base, three in the overlay. Each holds a
Deployment and a Service for one service, separated by `---`. All four follow this table:

| Concern | Required |
|---|---|
| Names | Deployment, Service and the single container are all named after the service: `orchestrator-scratch`, `executor`, `schema-agent`, `query-agent` (the compose service names) |
| Labels | `app.kubernetes.io/name: <service>` in both objects' `metadata.labels`, in the pod template's labels, in the Deployment's `selector.matchLabels`, and as the Service's `selector`. No other labels. |
| Replicas | `1` |
| Image | The `harness/<…>:dev` tag compose builds for that service. `imagePullPolicy: Never`. |
| Port | `containerPort: 8000`, named `http` |
| Readiness probe | `httpGet` on path `/healthz`, port `http`, `periodSeconds: 5` |
| Liveness probe | The same `httpGet`, `periodSeconds: 10` |
| Resources | requests `cpu: 50m`, `memory: 64Mi`; limits `memory: 256Mi`. **No CPU limit.** |
| Pod spec | `automountServiceAccountToken: false`, `enableServiceLinks: false` |
| Pod `securityContext` | `runAsNonRoot: true`, `seccompProfile.type: RuntimeDefault`. Do **not** set `runAsUser`. |
| Container `securityContext` | `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]` |
| Configuration | Services that call a model: `envFrom` with, in this order, ConfigMap `harness-env`, ConfigMap `domain-env`, Secret `llm-credentials`. The executor gets no `envFrom` and no `env`. |
| Service | No `type` (so ClusterIP). One port: `name: http`, `port: 8000`, `targetPort: http`. |

The names `harness-env`, `domain-env` and `llm-credentials` are the contract between the
base and every overlay. Write them exactly. Kustomize rewrites the two ConfigMap names
when it renders. The Secret name is never rewritten.

---

## 4.3 Harness base: `infra/base/` — DONE

Three files. Nothing in them may name a domain, comments included.

**`namespace.yaml`**: one Namespace.
- `metadata.name`: `harness-domain`. It is a placeholder that the overlay's `namespace:`
  field renames.
- Labels, exactly:
  - `pod-security.kubernetes.io/enforce: restricted`
  - `pod-security.kubernetes.io/warn: restricted`

**`orchestrator-scratch.yaml`**: Deployment and Service `orchestrator-scratch`, per 4.2,
image `harness/orchestrator-scratch:dev`, with the three `envFrom` sources. It sets no
`DOMAIN`: `domain-env` brings it.

**`kustomization.yaml`**:
- `apiVersion: kustomize.config.k8s.io/v1beta1`, `kind: Kustomization`
- `resources`: `namespace.yaml`, `orchestrator-scratch.yaml`
- `configMapGenerator`: one entry, `name: harness-env`, with one literal:
  `OLLAMA_HOST=http://host.k3d.internal:11434`
- **No** `namespace:` field. The overlay sets it.

Like `infra/base/compose.yaml`, the base is never applied on its own. Without a
`domain-env` its orchestrator could not start.

---

## 4.4 sql overlay: `infra/overlays/sql/` — DONE

Four files next to the existing `compose.yaml`.

**`executor.yaml`**: Deployment and Service `executor`, per 4.2, image
`harness/sql-executor:dev`, no configuration. In addition, a writable `/tmp`:
- container `volumeMounts`: `name: tmp`, `mountPath: /tmp`
- pod `volumes`: `name: tmp`, `emptyDir` with `medium: Memory` and `sizeLimit: 64Mi`

**`schema-agent.yaml`**: Deployment and Service `schema-agent`, per 4.2, image
`harness/sql-schema-agent:dev`, with the three `envFrom` sources.

**`query-agent.yaml`**: as `schema-agent.yaml`, named `query-agent`, image
`harness/sql-query-agent:dev`.

**`kustomization.yaml`**:
- `apiVersion` / `kind` as in the base
- `namespace: harness-sql`
- `resources`: `../../base`, `executor.yaml`, `schema-agent.yaml`, `query-agent.yaml`
- `configMapGenerator`: one entry, `name: domain-env`, with these literals:

| Variable | Value |
|---|---|
| `DOMAIN` | `sql` |
| `SQL_EXECUTOR_URL` | `http://executor:8000` |
| `SQL_SCHEMA_AGENT_URL` | `http://schema-agent:8000` |
| `SQL_QUERY_AGENT_URL` | `http://query-agent:8000` |
| `SQL_SCHEMA_MODEL` | `ollama:llama3.1:8b` |
| `SQL_QUERY_MODEL` | `ollama:llama3.1:8b` |
| `SQL_ANSWER_MODEL` | `ollama:llama3.1:8b` |

The cluster does not read your `.env`, so the model per role is set here. To run on
Claude instead, set all three to `anthropic:claude-opus-5`. Put the key in `.env`, and
4.7 carries it into the cluster.

---

## 4.5 Render before you apply — DONE

```bash
kubectl kustomize infra/overlays/sql
```

This prints exactly what `apply -k` would send, and touches no cluster. Confirm:

- 11 objects: one Namespace, two ConfigMaps, four Services, four Deployments.
- The Namespace is named `harness-sql` and carries both pod-security labels. Every other
  object has `namespace: harness-sql`.
- The ConfigMaps are named `domain-env-<hash>` and `harness-env-<hash>`, and every
  `configMapRef` uses the hashed names. That includes the orchestrator's references,
  which were written in the base.
- `llm-credentials` is referenced but never defined. No Secret appears in the output.
- Exactly four `imagePullPolicy: Never`, and no `type:` on any Service.

```bash
grep -rni sql infra/base
# expect: no output
```

---

## 4.6 Images into the cluster — DONE

There is no registry. The images compose builds go straight into the cluster's
containerd.

```bash
C="docker compose -f infra/overlays/sql/compose.yaml"
$C build
$C config --images
# expect: the four harness/*:dev tags that the manifests name
k3d image import --cluster harness $($C config --images)
# expect: "Successfully imported 4 image(s) into 1 cluster(s)"
docker exec k3d-harness-server-0 crictl images | grep harness
# expect: four docker.io/harness/... images, tag dev
```

---

## 4.7 Namespace and Secret — DONE

The Secret holds the API keys from `.env` and nothing else. It is created from your file,
never written into a manifest. The namespace has to exist first:

```bash
kubectl create namespace harness-sql --dry-run=client -o yaml | kubectl apply -f -

tmp=$(mktemp)
grep -E '^[A-Z0-9_]+_API_KEY=' .env > "$tmp"
kubectl -n harness-sql create secret generic llm-credentials \
  --from-env-file="$tmp" --dry-run=client -o yaml | kubectl apply -f -
rm -f "$tmp"
```

Do not use `--from-env-file=/dev/stdin` or `<(…)`: on Windows, `kubectl.exe` cannot open
either. The temporary file is the portable way.

Check the key names, never the values:

```bash
kubectl -n harness-sql get secret llm-credentials \
  -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}'
# expect: ANTHROPIC_API_KEY GOOGLE_API_KEY OPENAI_API_KEY (whichever lines .env has)
```

Keys that are blank in `.env` arrive blank, which is harmless with Ollama models.

---

## 4.8 Apply and roll out — DONE

```bash
kubectl apply -k infra/overlays/sql
kubectl -n harness-sql rollout status deployment --timeout=120s
# expect: four "successfully rolled out", within about 10 seconds
kubectl -n harness-sql get deploy,pods,svc
# expect: four Deployments 1/1; four pods Running, READY 1/1, RESTARTS 0;
#         four Services of TYPE ClusterIP on 8000/TCP, none with an EXTERNAL-IP
```

Re-run `apply` to see that it is idempotent: every line now says `unchanged`.

---

## 4.9 Port-forward and ask — DONE

Port 8100 must be free, so the compose stack must be down (`make down`). In a **second
terminal**, leave this running:

```bash
kubectl -n harness-sql port-forward svc/orchestrator-scratch 8100:8000
# expect: Forwarding from 127.0.0.1:8100 -> 8000
```

Back in the first terminal:

```bash
curl http://localhost:8100/healthz
# expect: {"status":"ok","service":"orchestrator_scratch"}

python cli.py --target scratch "How many customers are from Brazil?"
# expect: run_id on stderr, the SQL, a blank line, an answer saying 5; exit code 0
```

The CLI is unchanged: `SQL_SCRATCH_URL` in `.env` already says `http://localhost:8100`.

---

## 4.10 Verify from the logs and the cluster — DONE

**One run across four pods.** From the repo root with the venv active:

```bash
kubectl -n harness-sql logs -l app.kubernetes.io/name --tail=-1 --prefix=false | python -c "
import sys
from core.events import parse_event
events = sorted((parse_event(l) for l in sys.stdin if l.startswith('{')), key=lambda e: e.ts)
for e in events:
    print(f'{e.run_id[:8]}  {e.service:22s} {e.type:15s} seq={e.seq}')
"
```

Expect the same 11 events as step 3's 3.9: one `run_id`, all four services, the
orchestrator's events numbered 1–7. The executor's `tool_call` and the orchestrator's
`http_call seq=4` are often stamped in the same millisecond and may print in either order.
`--tail=-1` matters: with a selector, `kubectl logs` shows only 10 lines per pod by
default.

**The hardening holds.** (`K` is a shorthand; `MSYS_NO_PATHCONV=1` stops Git Bash from
rewriting the absolute paths.)

```bash
K="kubectl -n harness-sql"
$K exec deploy/executor -- id
# expect: uid=10001(app)
$K exec deploy/executor -- python -c "open('x', 'w')"
# expect: OSError: [Errno 30] Read-only file system: 'x'
MSYS_NO_PATHCONV=1 $K exec deploy/executor -- df -h /tmp
# expect: tmpfs 64M, mounted on /tmp
MSYS_NO_PATHCONV=1 $K exec deploy/executor -- ls /var/run/secrets/kubernetes.io/serviceaccount
# expect: No such file or directory -- no API token in the pod
$K exec deploy/executor -- env | grep -c API_KEY
# expect: 0
$K exec deploy/schema-agent -- env | grep API_KEY | cut -d= -f1
# expect: the key names from 4.7, and no values on screen
$K run probe --image=harness/sql-executor:dev --image-pull-policy=Never --restart=Never
# expect: Error from server (Forbidden): ... violates PodSecurity "restricted:latest" ...
```

**The executor's `/tmp` is in use.** Forward the executor too, in a third terminal:
`kubectl -n harness-sql port-forward svc/executor 8000:8000`. Then:

```bash
curl -s -X POST http://localhost:8000/execute -H "Content-Type: application/json" \
  -d "{\"run_id\":\"tmp-check\",\"sql\":\"SELECT COUNT(*) FROM (SELECT a.Name || b.Name AS k FROM Track a, Genre b, MediaType c ORDER BY k)\"}"
# expect: "ok":true ... "rows":[[437875]]
```

Without the volume, the same query fails with `disk I/O error`. Stop that forward
afterwards. Only the orchestrator should normally be reachable from outside.

**The cluster heals.**

```bash
$K delete pod -l app.kubernetes.io/name=query-agent
$K get pods
# expect: a new query-agent pod with a new name, Running within seconds
python cli.py --target scratch "How many customers are from Brazil?"
# expect: still answers. The orchestrator found the new pod through the Service name.
$K top pods
# expect: each pod well under its 256Mi limit (about 40-50Mi)
```

---

## 4.11 Makefile targets — DONE

Add to the `Makefile`, below the compose targets:

- `CLUSTER := harness`
- `KUBECTL := kubectl --context k3d-$(CLUSTER)`. Every `kubectl` in the Makefile uses it,
  so `make` never acts on another cluster.
- `NAMESPACE := harness-$(DOMAIN)`, `OVERLAY := infra/overlays/$(DOMAIN)`
- `PORT ?= 8100`

| Target | Does |
|---|---|
| `cluster` | Creates the cluster from `infra/base/k3d.yaml`, unless `k3d cluster get $(CLUSTER)` says it already exists |
| `cluster-delete` | `k3d cluster delete $(CLUSTER)` |
| `k8s-up` | Depends on `cluster`. In order: `$(COMPOSE) build`; `k3d image import` the images listed by `$(COMPOSE) config --images`; the namespace and Secret exactly as in 4.7; `apply -k $(OVERLAY)`; `rollout restart deployment` in the namespace; `rollout status deployment --timeout=120s` |
| `k8s-down` | `delete -k $(OVERLAY) --ignore-not-found` |
| `k8s-ps` | `get deploy,pods,svc` in the namespace |
| `k8s-logs` | `logs -f` in the namespace for selector `app.kubernetes.io/name`, with `--prefix`, `--tail=20` and `--max-log-requests=20` |
| `k8s-forward` | `port-forward svc/orchestrator-scratch $(PORT):8000` in the namespace |

Add them all to `.PHONY`. In the Secret recipe, the `grep` exits 1 when `.env` has no key
lines; follow it with `|| true` so the target still creates an empty Secret. Remove the
temporary file with a `trap`, so it is removed even when `kubectl` fails.

Verify from nothing:

```bash
make cluster-delete
make k8s-up          # creates the cluster, builds, imports, deploys; ends with four rollouts
make k8s-ps
make k8s-forward     # second terminal
python cli.py --target scratch "How many customers are from Brazil?"
make k8s-up          # again: every pod is replaced. The old forward still listens, but
                     # the next request gets an empty reply and it exits with
                     # "lost connection to pod". Restart it.
make k8s-down        # the namespace and everything in it, Secret included, are gone
```

---

## 4.12 CLAUDE.md — DONE

- Milestones: mark 4 ✅, and "k3d/minikube up" becomes "k3d up".
- The domain contract tree: the `infra/overlays/<name>/` line becomes "its deployment:
  `compose.yaml` + a kustomize overlay (`kustomization.yaml`, one manifest per service)".
- Below the tree, add the cluster contract. An overlay sets `namespace: harness-<name>`
  and generates ConfigMap `domain-env` holding `DOMAIN=<name>` plus its `<NAME>_*`
  settings. Model-calling pods read `harness-env`, `domain-env` and the Secret
  `llm-credentials`, which holds only API keys and is made by `make k8s-up` from `.env`.
  Images are imported (`imagePullPolicy: Never`). Pods run under the `restricted` Pod
  Security Standard.
- Stack: "Docker, k3d or minikube, kustomize" becomes "Docker, k3d 5.9 (k3s v1.35,
  pinned by digest), kustomize (built into kubectl)". The API-keys line names the Secret:
  `llm-credentials`, made from `.env` by `make k8s-up`.
- Repo layout, under `infra/`: `base/k3d.yaml` (the local cluster),
  `base/kustomization.yaml` with `namespace.yaml` and `orchestrator-scratch.yaml` (the
  engines' half of every domain's cluster deployment), and `overlays/<domain>/` gaining
  `kustomization.yaml` plus one manifest per service (`make k8s-up DOMAIN=<name>`).

---

## Done when

```bash
pytest                                       # still green; no Python changed
ruff check .                                 # clean
grep -rni sql infra/base .dockerignore       # no output: the harness files name no domain
make k8s-up                                  # four rollouts succeed
make k8s-forward                             # (second terminal)
python cli.py --target scratch "How many customers are from Brazil?"   # correct; exit 0
```

- Events from all four pods share one `run_id`, and the orchestrator's events are
  numbered 1–7.
- Every pod runs as uid 10001 on a read-only root filesystem, without an API token,
  admitted under `restricted`. Only model-calling pods have the API keys.
- The only way in from your machine is the port-forward. Every Service is ClusterIP.
- Outside `infra/overlays/sql/`, the step touched only harness files, `Makefile` and
  `CLAUDE.md`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `k3d: command not found` after `winget install` | The terminal predates the `PATH` change | Open a new terminal |
| Pod `ErrImageNeverPull` | The image isn't in the cluster's containerd: never imported, or the cluster was recreated | 4.6, or `make k8s-up` |
| Pod `CreateContainerConfigError`, event says `secret "llm-credentials" not found` | The Secret was never created in this namespace | 4.7, or `make k8s-up` |
| … `configmap "domain-env-…" not found` or `"domain-env" not found` | The overlay has no `domain-env` generator, or you applied the base on its own | Add the generator (4.4). Apply the overlay, never the base. |
| `apply` prints `Warning: would violate PodSecurity "restricted"`; the Deployment shows 0/1 and has no pods | A securityContext field from 4.2 is missing. Enforcement rejects the pods, not the Deployment. | `kubectl -n harness-sql get events` names the field. Fix the manifest. |
| `curl: (52) Empty reply from server` on 8100, then `port-forward` prints `lost connection to pod` and exits | The pod it was bound to was replaced (rollout, restart, deletion). The forward notices only on the next connection. | Run it again. It binds to whichever pod is current. |
| `unable to listen on port 8100` | The compose stack (or an old port-forward) holds the port | `make down`; stop the other forward |
| `/ask` fails; orchestrator logs show `ConnectError` to `host.k3d.internal:11434` | Ollama isn't running, or (Linux) only listens on loopback | Start Ollama. On Linux, run the Ollama server with `OLLAMA_HOST=0.0.0.0`. |
| A code or prompt change doesn't show up | Pods keep running the image they started with, even after a re-import under the same tag | `make k8s-up` (it restarts the Deployments) |
| A config change in `domain-env` doesn't show up | You edited a running object by hand instead of the overlay | Edit the overlay and re-apply. The new hash rolls the pods. |
| Executor returns `disk I/O error` for large sorts or `DISTINCT` | No writable `/tmp`: SQLite spills temporary B-trees to disk | The `/tmp` volume in 4.4 |
| `kubectl` says `connection refused` to `127.0.0.1:<port>`, or talks to the wrong cluster | The cluster is stopped (Docker restarted), or another context is current | `k3d cluster start harness`; `kubectl config use-context k3d-harness` |
| Git Bash turns `/tmp` or `/var/run/...` into `C:/...` | MSYS path conversion of absolute paths | Prefix the command with `MSYS_NO_PATHCONV=1` |
