DB_URL := https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_Sqlite.sqlite
DB_PATH := data/chinook.db
DB_TMP := $(DB_PATH).tmp
DB_SHA256 := bdf635be69850bd3be09c9a2dbeef7ddfb80036bd3ef3381383cd03b61e4a61a
DB_SIZE := 1067008

# Which domain the targets below drive: make up DOMAIN=<name>, make k8s-up DOMAIN=<name>
DOMAIN ?= sql
COMPOSE := docker compose -f infra/overlays/$(DOMAIN)/compose.yaml

.PHONY: db up down ps logs

up:
	$(COMPOSE) up --build --wait

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f

# --- Kubernetes: one local k3d cluster, one namespace per domain -------------------
CLUSTER := harness
# Pinned, so make never acts on whatever other cluster happens to be current.
KUBECTL := kubectl --context k3d-$(CLUSTER)
NAMESPACE := harness-$(DOMAIN)
OVERLAY := infra/overlays/$(DOMAIN)
# Local end of k8s-forward: where <DOMAIN>_SCRATCH_URL points.
PORT ?= 8100

.PHONY: cluster cluster-delete k8s-up k8s-down k8s-ps k8s-logs k8s-forward

cluster:
	k3d cluster get $(CLUSTER) >/dev/null 2>&1 || k3d cluster create --config infra/base/k3d.yaml

cluster-delete:
	k3d cluster delete $(CLUSTER)

# Everything, every time: each part is idempotent or cheap. Images are the ones the
# domain's compose file builds, imported rather than pulled. The Secret holds only the
# API keys from .env; it goes through a temp file because kubectl.exe on Windows can't
# read /dev/stdin. Pods keep the image they started with, so a re-imported :dev needs
# the restart.
k8s-up: cluster
	$(COMPOSE) build
	k3d image import --cluster $(CLUSTER) $$($(COMPOSE) config --images)
	$(KUBECTL) create namespace $(NAMESPACE) --dry-run=client -o yaml | $(KUBECTL) apply -f -
	@tmp=$$(mktemp) && trap 'rm -f "$$tmp"' EXIT && \
	{ grep -E '^[A-Z0-9_]+_API_KEY=' .env > "$$tmp" || true; } && \
	$(KUBECTL) -n $(NAMESPACE) create secret generic llm-credentials \
		--from-env-file="$$tmp" --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) apply -k $(OVERLAY)
	$(KUBECTL) -n $(NAMESPACE) rollout restart deployment
	$(KUBECTL) -n $(NAMESPACE) rollout status deployment --timeout=120s

# The namespace and everything in it, Secret included. The cluster stays.
k8s-down:
	$(KUBECTL) delete -k $(OVERLAY) --ignore-not-found

k8s-ps:
	$(KUBECTL) -n $(NAMESPACE) get deploy,pods,svc

k8s-logs:
	$(KUBECTL) -n $(NAMESPACE) logs -f -l app.kubernetes.io/name --prefix --tail=20 --max-log-requests=20

# Foreground, in a second terminal. Bound to one pod: rerun it after every k8s-up.
k8s-forward:
	$(KUBECTL) -n $(NAMESPACE) port-forward svc/orchestrator-scratch $(PORT):8000

db:
	@mkdir -p data
	@curl --fail --location --silent --show-error "$(DB_URL)" --output "$(DB_TMP)"
	@echo "$(DB_SHA256) *$(DB_TMP)" | sha256sum --check -
	@test "$$(wc -c < "$(DB_TMP)" | tr -d '[:space:]')" = "$(DB_SIZE)"
	@mv -f "$(DB_TMP)" "$(DB_PATH)"