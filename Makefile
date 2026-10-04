DB_URL := https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_Sqlite.sqlite
DB_PATH := data/chinook.db
DB_TMP := $(DB_PATH).tmp
DB_SHA256 := bdf635be69850bd3be09c9a2dbeef7ddfb80036bd3ef3381383cd03b61e4a61a
DB_SIZE := 1067008

# Which domain's compose stack the targets below drive: make up DOMAIN=<name>
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

db:
	@mkdir -p data
	@curl --fail --location --silent --show-error "$(DB_URL)" --output "$(DB_TMP)"
	@echo "$(DB_SHA256) *$(DB_TMP)" | sha256sum --check -
	@test "$$(wc -c < "$(DB_TMP)" | tr -d '[:space:]')" = "$(DB_SIZE)"
	@mv -f "$(DB_TMP)" "$(DB_PATH)"