# Axonate — one-command ops. Default profile is poc.
PROFILE ?= poc
COMPOSE = docker compose --profile $(PROFILE)

.DEFAULT_GOAL := help

.PHONY: help up down logs ps build config smoke login-claude login-codex set-claude-token add-user digest backup restore

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## start the stack (PROFILE=poc|prod)
	$(COMPOSE) up -d --build

down:  ## stop the stack (keeps volumes/logins)
	$(COMPOSE) down

logs:  ## tail logs
	$(COMPOSE) logs -f --tail=100

ps:  ## list running services
	$(COMPOSE) ps

build:  ## build images
	$(COMPOSE) build

config:  ## validate compose config
	$(COMPOSE) config >/dev/null && echo "compose config OK ($(PROFILE))"

smoke:  ## run the pre-launch smoke gate
	./scripts/smoke.sh

login-claude:  ## print a Claude OAuth token (headless). Then: make set-claude-token TOKEN=.. SLOT=A|B
	@docker compose --profile poc exec axonate-adapter claude setup-token

set-claude-token:  ## save a Claude token to .env + reload adapter (TOKEN=sk-ant-.. SLOT=A|B)
	@test -n "$(TOKEN)" || { echo "usage: make set-claude-token TOKEN=sk-ant-... SLOT=A|B"; exit 1; }
	@SLOT=$(or $(SLOT),A); \
	  if grep -q "^CLAUDE_OAUTH_TOKEN_$$SLOT=" .env; then \
	    sed -i.bak "s|^CLAUDE_OAUTH_TOKEN_$$SLOT=.*|CLAUDE_OAUTH_TOKEN_$$SLOT=$(TOKEN)|" .env && rm -f .env.bak; \
	  else echo "CLAUDE_OAUTH_TOKEN_$$SLOT=$(TOKEN)" >> .env; fi; \
	  echo "saved CLAUDE_OAUTH_TOKEN_$$SLOT; recreating adapter..."
	docker compose --profile poc up -d axonate-adapter

login-codex:  ## log Codex into the adapter (headless device-auth)
	docker compose --profile poc exec axonate-adapter \
	  env CODEX_HOME=/cfg/codex codex login --device-auth

add-user:  ## provision a LiteLLM virtual key (EMAIL=.. BUDGET=..)
	./scripts/add_user.sh "$(EMAIL)" "$(BUDGET)"

digest:  ## post the clean daily spend digest to Slack
	python3 scripts/spend_digest.py

backup:  ## dump postgres + config to ./backups
	./scripts/backup.sh

restore:  ## restore postgres from a dump (FILE=./backups/xx.sql.gz)
	./scripts/restore.sh "$(FILE)"
