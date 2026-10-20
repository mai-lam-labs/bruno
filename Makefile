# developer toolchain — Astral uv + ruff (lint/format) + ty + pyrefly (typecheck).
#
# FULLY SELF-CONTAINED: uv installs into ./.uv (not ~/.local/bin), its cache is
# ./.uv/cache, Python interpreters go to ./.uv/python, and the venv is ./.venv —
# nothing touches $HOME or the system.
# The venv is VSCode-compatible (.vscode/settings.json points at .venv/bin/python).
#
#   make install          project-local uv + create .venv + install tools
#   make lock             re-resolve uv.lock after a dependency change
#   make check            lint + format-check + typecheck, then the tests on prague and amsterdam
#   make hoodi-wallet     create/show a testnet wallet (.secrets/hoodi/; sepolia-wallet too)
#   make check-hoodi      the live bundle test on Hoodi (check-sepolia too; spends testnet ETH)
#   make hoodi-sweep      drain a testnet's live-test accounts into its wallet (sepolia-… too)
#   make hoodi-deploy     deploy the runner's deployer and Calibur on a testnet (sepolia-… too)
#   make serve            run the relay (CONFIG=, PORT=, FLOAT_KEY=, PAYEE=)
#   make worker-check     the Worker's format, lint, types and tests (Deno); make vectors
#                         rewrites the vectors its tests check against
#   make lint-fix         sort import blocks (ruff's I001), then `make format`
#   make vendor           re-fetch the mainnet bytecode the tests install (RPC=… to override)
#   make clean             remove .venv + caches  |  make distclean  also .uv + dist

SRC  := bruno
PYVER := 3.14

TOOLS := $(CURDIR)/.uv
UV    := $(TOOLS)/uv
# uv, pinned: its installer for that version, by hash.
# The installer holds each platform's archive hash and checks the download against it
# with sha256sum, so pinning the installer pins uv itself
UV_VERSION := 0.12.16
UV_INSTALLER_SHA256 := 889a5745afddc78b389841caede74711003b782e2ec8c2f29b3c03cbd7b580a9
VENV  := $(CURDIR)/.venv
PY    := $(VENV)/bin/python
RUFF    := $(VENV)/bin/ruff
TY      := $(VENV)/bin/ty
PYREFLY := $(VENV)/bin/pyrefly

export UV_CACHE_DIR            := $(TOOLS)/cache
export UV_PYTHON_INSTALL_DIR   := $(TOOLS)/python
# the venv uv sync installs into: the one the tools above run from
export UV_PROJECT_ENVIRONMENT  := $(VENV)


# ---- dev tools (project-local, in dist/tools/) ----
# a nightly: the first builds with Amsterdam's gas (EIP-8037, EIP-2780: foundry-rs/foundry#17432,
# merged 2026-10-07; 1.8.3's amsterdam charges Prague's). The tarball's sha256 is GitHub's for
# the release asset. Checked 2026-10-19: in-EVM gas matches Sepolia's exactly
FOUNDRY_VERSION  := nightly-6c21ffe229e9963bf7e97c4879cd95c8a0463be3
FOUNDRY_TARBALL  := foundry_nightly_linux_amd64.tar.gz
FOUNDRY_SHA256   := 897cfbad2ba986f766645eb2a2b2acb3131db89074d17118ff7e8904a36401e9
FOUNDRY_URL      := https://github.com/foundry-rs/foundry/releases/download/$(FOUNDRY_VERSION)/$(FOUNDRY_TARBALL)
ANVIL_DIR        := dist/tools/$(FOUNDRY_VERSION)
ANVIL            := $(ANVIL_DIR)/anvil
# teeswap's anvil takes 8545: bruno's runs beside it
ANVIL_PORT       ?= 8546
export ANVIL_PORT
ANVIL_PID        := dist/tools/anvil.pid
# the spec's chain: EIP-7702 (type-4 transactions) needs Prague. The mock builder mines a
# bundle as one block from the pool: --order fifo keeps the bundle's order, and
# --disable-pool-balance-checks admits a sender that the bundle's funding tx pays for in the
# same block (a tx still invalid when mined is left out, as a builder's would be)
# the fork the local chain runs: `make check` runs the tests on each of these in turn, and
# exports it, so the tests price gas by its schedule (bruno/forks.py)
ANVIL_HARDFORK   ?= prague
export ANVIL_HARDFORK
ANVIL_ARGS       := --port $(ANVIL_PORT) --hardfork $(ANVIL_HARDFORK) --accounts 10 --balance 1000 \
                    --order fifo --disable-pool-balance-checks
# the Cloudflare Worker's toolchain (worker/): Deno type-checks, lints, formats, tests and
# bundles the TypeScript; workerd is Cloudflare's runtime, run locally. Both pinned by sha256
# (Deno's own .sha256sum; workerd's, GitHub's release asset digest). Deno's caches, the JSR
# packages included, stay in ./.deno; it runs no npm install scripts
DENO_VERSION     := v2.9.7
DENO_SHA256      := c6527f24f4b16031d3ae4fa9f658d5f11534c8d84ce7dc8502420280919c3490
DENO_URL         := https://github.com/denoland/deno/releases/download/$(DENO_VERSION)/deno-x86_64-unknown-linux-gnu.zip
DENO_DIR_TOOL    := dist/tools/deno-$(DENO_VERSION)
DENO             := $(CURDIR)/$(DENO_DIR_TOOL)/deno
WORKERD_VERSION  := v1.20261019.1
WORKERD_SHA256   := ddf34551fb6283cf1020f6f417026d386e660fee7573fbfc18e989282e2d848a
WORKERD_URL      := https://github.com/cloudflare/workerd/releases/download/$(WORKERD_VERSION)/workerd-linux-64.gz
WORKERD_DIR      := dist/tools/workerd-$(WORKERD_VERSION)
WORKERD          := $(CURDIR)/$(WORKERD_DIR)/workerd
# the tests run the bundled Worker under workerd, on this port (beside anvil's)
WORKERD_PORT     ?= 8547
export WORKERD_PORT
WORKER           := worker
export DENO_DIR             := $(CURDIR)/.deno
export DENO_NO_UPDATE_CHECK := 1
export DENO_NO_PROMPT       := 1

# how long anvil, started for the tests, is given to answer:
# CI's runners start them several times slower than a workstation
START_WAIT_SECONDS := 60


uv-bootstrap:
	@test -x "$(UV)" && [ "$$("$(UV)" --version | cut -d' ' -f2)" = "$(UV_VERSION)" ] || { \
	  echo ">> installing project-local uv $(UV_VERSION) into $(TOOLS)"; \
	  mkdir -p "$(TOOLS)" && \
	  curl -LsSf "https://astral.sh/uv/$(UV_VERSION)/install.sh" -o "$(TOOLS)/uv-install.sh" && \
	  echo "$(UV_INSTALLER_SHA256)  $(TOOLS)/uv-install.sh" | sha256sum -c --quiet && \
	  env UV_UNMANAGED_INSTALL="$(TOOLS)" UV_NO_MODIFY_PATH=1 sh "$(TOOLS)/uv-install.sh"; }

# exactly what uv.lock says, here and in CI alike: never a fresh resolve
install: uv-bootstrap
	"$(UV)" sync --locked --group dev --python $(PYVER)
	@echo ">> toolchain ready (project-local); 'make check' to run everything"



# re-resolve uv.lock after changing pyproject.toml's dependencies (here, so uv's cache and
# interpreters stay in ./.uv), then `make install`
lock: uv-bootstrap
	"$(UV)" lock --python $(PYVER)

# the venv is what uv.lock says: a check that passes here passes in CI
env-check:
	@"$(UV)" sync --locked --group dev --python $(PYVER) --check >/dev/null 2>&1 || \
	  { echo "error: .venv differs from uv.lock: run 'make install'"; exit 1; }

lint:
	"$(RUFF)" check $(SRC)

format:
	"$(RUFF)" format $(SRC)

# sorts import blocks (ruff's isort rule, I001), and fixes nothing else
lint-fix:
	"$(RUFF)" check --fix --select I $(SRC)

format-check:
	"$(RUFF)" format --check $(SRC)

typecheck:
	"$(TY)" check $(SRC)
	"$(PYREFLY)" check --min-severity warn $(SRC)

test:
	"$(PY)" -m pytest $(SRC)/tests -q --tb=short --no-header --durations=0 -vv


# the relay, for trying it by hand: http://127.0.0.1:$(PORT)/ for its chains and documents.
# CONFIG is its configuration (DEPLOYMENT.md): config.local.json (gitignored) if there is one,
# else config.default.json. FLOAT_KEY, if given, is the float's key file for every chain,
# overriding the configuration's; PAYEE the payee of every chain that doesn't name its own
SECRETS := .secrets
CONFIG ?= $(if $(wildcard config.local.json),config.local.json,config.default.json)
PORT ?= 8402
FLOAT_KEY ?= $(SECRETS)/float.key
PAYEE ?=

serve: $(if $(filter $(SECRETS)/float.key,$(FLOAT_KEY)),$(SECRETS)/float.key) $(if $(PAYEE),,$(SECRETS)/payee.address)
	"$(PY)" -m $(SRC).server --config "$(CONFIG)" --port $(PORT) --float-key "$(FLOAT_KEY)" \
	  --payee "$(if $(PAYEE),$(PAYEE),$$(cat $(SECRETS)/payee.address))"

# the float and the payee `make serve` uses unless given FLOAT_KEY= or PAYEE=: each key created
# the first time it's needed, never overwritten, in .secrets/ (gitignored); only addresses
# printed. The float pays the concurrency accounts' gas, so fund its address; the payee
# receives the payments
$(SECRETS)/float.key $(SECRETS)/payee.key:
	@"$(PY)" -c "from pathlib import Path; from $(SRC).accounts import new_key; print('$@:', new_key(Path('$@')).address)"

$(SECRETS)/payee.address: $(SECRETS)/payee.key
	"$(PY)" -c "from pathlib import Path; from $(SRC).accounts import read_key; print(read_key(Path('$<')).address)" > $@



# the tests need anvil: started here, stopped after, as for check
coverage: anvil-start
	$(MAKE) coverage-run; rc=$$?; $(MAKE) anvil-stop; exit $$rc

coverage-run:
	"$(PY)" -m coverage run --source=$(SRC) -m pytest $(SRC)/tests -q --tb=short --no-header
	"$(PY)" -m coverage report -m --fail-under=70

# the CI gate: lint and types once, then the tests on a local chain at each fork
check: env-check lint format-check typecheck worker-check
	$(MAKE) test-fork ANVIL_HARDFORK=prague
	$(MAKE) test-fork ANVIL_HARDFORK=amsterdam

# the tests on a local chain at ANVIL_HARDFORK, started here and stopped after
test-fork: anvil-start workerd-start
	$(MAKE) test; rc=$$?; $(MAKE) workerd-stop anvil-stop; exit $$rc

ci: check

build:
	"$(UV)" build --python "$(PY)" --out-dir dist

# ---- the Worker (worker/): Deno and workerd ----
deno-fetch:
	@if test -x "$(DENO)"; then :; else \
	  mkdir -p $(DENO_DIR_TOOL) && \
	  curl -LsSf "$(DENO_URL)" -o $(DENO_DIR_TOOL)/deno.zip && \
	  { [ "$$(sha256sum $(DENO_DIR_TOOL)/deno.zip | cut -d' ' -f1)" = "$(DENO_SHA256)" ] || \
	    { echo "error: deno zip hash mismatch (expected $(DENO_SHA256))"; rm -f $(DENO_DIR_TOOL)/deno.zip; exit 1; }; } && \
	  unzip -q -o $(DENO_DIR_TOOL)/deno.zip -d $(DENO_DIR_TOOL) && rm $(DENO_DIR_TOOL)/deno.zip && \
	  echo ">> deno $(DENO_VERSION) ready at $(DENO)"; \
	fi

workerd-fetch:
	@if test -x "$(WORKERD)"; then :; else \
	  mkdir -p $(WORKERD_DIR) && \
	  curl -LsSf "$(WORKERD_URL)" -o $(WORKERD_DIR)/workerd.gz && \
	  { [ "$$(sha256sum $(WORKERD_DIR)/workerd.gz | cut -d' ' -f1)" = "$(WORKERD_SHA256)" ] || \
	    { echo "error: workerd hash mismatch (expected $(WORKERD_SHA256))"; rm -f $(WORKERD_DIR)/workerd.gz; exit 1; }; } && \
	  gunzip $(WORKERD_DIR)/workerd.gz && chmod +x $(WORKERD) && \
	  echo ">> workerd $(WORKERD_VERSION) ready at $(WORKERD)"; \
	fi

# what the Worker takes from the Python reference: the golden vectors its port is checked
# against, the OpenRPC and OpenAPI documents it serves, and its root's page; formatted as
# worker-check expects
vectors: deno-fetch
	"$(PY)" -m $(SRC).tests.vectors > $(WORKER)/test/vectors.json
	"$(PY)" -c "import json, sys; from $(SRC).server.rpc import openrpc; json.dump(openrpc([]), sys.stdout, indent=1)" > $(WORKER)/src/openrpc.json
	"$(PY)" -c "import json, sys; from $(SRC).server.app import create_app; json.dump(create_app({}).openapi_schema.to_schema(), sys.stdout, indent=1)" > $(WORKER)/src/openapi.json
	"$(PY)" -c "import json, sys; from $(SRC).server.front import PAGE; from $(SRC).server.operations import REPOSITORY, SPEC; json.dump({'template': PAGE.template, 'spec': SPEC, 'repository': REPOSITORY}, sys.stdout, indent=1)" > $(WORKER)/src/front.json
	cd $(WORKER) && "$(DENO)" fmt test/vectors.json src/openrpc.json src/openapi.json src/front.json

# the Worker bundled into one module, which workerd and Cloudflare run
worker-build: deno-fetch
	cd $(WORKER) && "$(DENO)" bundle --frozen --platform browser --output ../dist/worker/worker.js src/index.ts

# formatting, lint and types (strict), then the tests against the Python reference's vectors
worker-check: deno-fetch
	cd $(WORKER) && "$(DENO)" fmt --check && "$(DENO)" lint && "$(DENO)" check src test deploy.ts
	cd $(WORKER) && "$(DENO)" test --frozen --allow-read=test test

worker-format: deno-fetch
	cd $(WORKER) && "$(DENO)" fmt

# the bundled Worker under workerd, for the tests: its Durable Objects' storage fresh each start
WORKERD_PID     := dist/tools/workerd.pid
WORKERD_STORAGE := dist/workerd-storage
workerd-start: worker-build workerd-fetch
	@if test -f "$(WORKERD_PID)" && kill -0 $$(cat "$(WORKERD_PID)") 2>/dev/null; then \
	  echo ">> workerd already running (pid $$(cat "$(WORKERD_PID)"))"; \
	else \
	  rm -rf $(WORKERD_STORAGE) && mkdir -p $(WORKERD_STORAGE) && \
	  nohup "$(WORKERD)" serve $(WORKER)/workerd.capnp --socket-addr http=127.0.0.1:$(WORKERD_PORT) \
	    --directory-path do-storage=$(CURDIR)/$(WORKERD_STORAGE) > dist/tools/workerd.log 2>&1 & \
	  echo $$! > "$(WORKERD_PID)"; \
	  for i in $$(seq 1 $(START_WAIT_SECONDS)); do \
	    if curl -sf -m 2 -o /dev/null http://127.0.0.1:$(WORKERD_PORT)/openrpc.json; then \
	      echo ">> workerd answering on :$(WORKERD_PORT) (pid $$(cat "$(WORKERD_PID)"))"; break; fi; \
	    sleep 1; \
	  done; \
	  curl -sf -m 2 -o /dev/null http://127.0.0.1:$(WORKERD_PORT)/openrpc.json || { \
	    echo "error: workerd didn't answer:"; tail -5 dist/tools/workerd.log; \
	    kill $$(cat "$(WORKERD_PID)") 2>/dev/null; rm -f "$(WORKERD_PID)"; exit 1; }; \
	fi

workerd-stop:
	@if test -f "$(WORKERD_PID)" && kill -0 $$(cat "$(WORKERD_PID)") 2>/dev/null; then \
	  kill $$(cat "$(WORKERD_PID)") && echo ">> workerd stopped"; \
	fi; rm -f "$(WORKERD_PID)"

# ---- the Worker on Cloudflare, through its API (DEPLOYMENT.md, Cloudflare) ----
# CLOUDFLARE holds the account's account_id and api_token. Each setting can be given, so anyone
# deploys their own: the Worker's name and hostname (its zone found in the account), the
# configuration, the payee (else .secrets/payee.address) and the float's key file. In order:
# worker-deploy, worker-secret, worker-domain. deploy.ts reaches only api.cloudflare.com and
# reads only the files it's given
CLOUDFLARE       ?= $(SECRETS)/cloudflare
WORKER_NAME      ?= bruno
WORKER_DOMAIN    ?= bruno.mailam.app
WORKER_CONFIG    ?= config.default.json
WORKER_PAYEE     ?=
WORKER_FLOAT_KEY ?= $(SECRETS)/float.key
# the runtime the Worker's tested on locally (workerd.capnp)
WORKER_COMPAT    := $(shell sed -n 's/.*compatibilityDate = "\([0-9-]*\)".*/\1/p' $(WORKER)/workerd.capnp)
# deploy.ts, reading the credentials and the files $(1) names (space-separated) only
comma := ,
space := $(subst ,, )
DEPLOY = cd $(WORKER) && "$(DENO)" run --frozen --allow-net=api.cloudflare.com \
  --allow-read=$(subst $(space),$(comma),$(strip $(abspath $(CLOUDFLARE) $(1)))) deploy.ts

# create the Worker, or replace its code and bindings (its secrets kept): CONFIG checked first
worker-deploy: worker-build $(if $(WORKER_PAYEE),,$(SECRETS)/payee.address)
	$(call DEPLOY,dist/worker/worker.js $(WORKER_CONFIG)) \
	  deploy --credentials=$(abspath $(CLOUDFLARE)) --name=$(WORKER_NAME) --compat=$(WORKER_COMPAT) \
	  --bundle=$(abspath dist/worker/worker.js) --config=$(abspath $(WORKER_CONFIG)) \
	  --payee="$(if $(WORKER_PAYEE),$(WORKER_PAYEE),$$(cat $(abspath $(SECRETS)/payee.address)))"

# the float's key, as the Worker's FLOAT_KEY secret: never shown, its address printed
worker-secret: deno-fetch $(if $(filter $(SECRETS)/float.key,$(WORKER_FLOAT_KEY)),$(SECRETS)/float.key)
	$(call DEPLOY,$(WORKER_FLOAT_KEY)) \
	  secret --credentials=$(abspath $(CLOUDFLARE)) --name=$(WORKER_NAME) --key=$(abspath $(WORKER_FLOAT_KEY))

# WORKER_DOMAIN routed to the Worker: Cloudflare makes its DNS record and certificate
worker-domain: deno-fetch
	$(call DEPLOY) \
	  domain --credentials=$(abspath $(CLOUDFLARE)) --name=$(WORKER_NAME) --domain=$(WORKER_DOMAIN)

# ---- anvil: local EVM node for integration tests ----
anvil-fetch:
	@if test -x "$(ANVIL)"; then echo ">> anvil already at $(ANVIL)"; else \
	  mkdir -p $(ANVIL_DIR) && \
	  curl -LsSf "$(FOUNDRY_URL)" -o $(ANVIL_DIR)/$(FOUNDRY_TARBALL) && \
	  { [ "$$(sha256sum $(ANVIL_DIR)/$(FOUNDRY_TARBALL) | cut -d' ' -f1)" = "$(FOUNDRY_SHA256)" ] || \
	    { echo "error: foundry tarball hash mismatch (expected $(FOUNDRY_SHA256))"; rm -f $(ANVIL_DIR)/$(FOUNDRY_TARBALL); exit 1; }; } && \
	  tar -xzf $(ANVIL_DIR)/$(FOUNDRY_TARBALL) -C $(ANVIL_DIR) anvil && \
	  rm $(ANVIL_DIR)/$(FOUNDRY_TARBALL) && \
	  echo ">> anvil $(FOUNDRY_VERSION) ready at $(ANVIL)"; \
	fi

anvil-run: anvil-fetch
	$(ANVIL) $(ANVIL_ARGS)

anvil-start: anvil-fetch
	@if test -f "$(ANVIL_PID)" && kill -0 $$(cat "$(ANVIL_PID)") 2>/dev/null; then \
	  echo ">> anvil already running (pid $$(cat "$(ANVIL_PID)"))"; \
	else \
	  nohup $(ANVIL) $(ANVIL_ARGS) --silent \
	    > dist/tools/anvil.log 2>&1 & echo $$! > "$(ANVIL_PID)"; \
	  for i in $$(seq 1 $(START_WAIT_SECONDS)); do \
	    if ! kill -0 $$(cat "$(ANVIL_PID)") 2>/dev/null; then break; fi; \
	    if curl -sf -m 2 -o /dev/null -X POST -H 'content-type: application/json' \
	      --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' \
	      http://127.0.0.1:$(ANVIL_PORT); then \
	      echo ">> anvil answering on :$(ANVIL_PORT) (pid $$(cat "$(ANVIL_PID)"))"; break; fi; \
	    sleep 1; \
	  done; \
	  curl -sf -m 2 -o /dev/null -X POST -H 'content-type: application/json' \
	    --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' \
	    http://127.0.0.1:$(ANVIL_PORT) || { \
	    echo "error: anvil didn't answer within $(START_WAIT_SECONDS)s:"; tail -5 dist/tools/anvil.log; \
	    kill $$(cat "$(ANVIL_PID)") 2>/dev/null; rm -f "$(ANVIL_PID)"; exit 1; }; \
	fi
	@"$(PY)" -m $(SRC).tests.localchain http://127.0.0.1:$(ANVIL_PORT)

anvil-stop:
	@if test -f "$(ANVIL_PID)" && kill -0 $$(cat "$(ANVIL_PID)") 2>/dev/null; then \
	  kill $$(cat "$(ANVIL_PID)") && echo ">> anvil stopped"; \
	  rm -f "$(ANVIL_PID)"; \
	else \
	  echo ">> anvil not running"; \
	  rm -f "$(ANVIL_PID)"; \
	fi

# the mainnet bytecode the local chain installs (bruno/tests/vendor/): fetched once and
# checked in, so setting up the chain needs no network. Re-run only to change what's vendored
RPC ?= https://ethereum-rpc.publicnode.com
vendor:
	"$(PY)" -m $(SRC).tests.vendor "$(RPC)"

# a testnet's wallet, the float the live tests spend from (NETWORK is hoodi or sepolia):
# created in .secrets/NETWORK/ (gitignored, kept by distclean) if there's none, and its address
# and balance printed, to fund from a faucet
%-wallet:
	"$(PY)" -m $(SRC).tests.live $*

# against a testnet itself, through a real builder (hoodi: Titan, dropping; sepolia: the nflaig
# ePBS builder, standalone): only when asked. Needs the network and a funded
# .secrets/NETWORK/wallet.key; spends testnet ETH
check-hoodi check-sepolia: check-%: workerd-start
	"$(PY)" -m pytest $(SRC)/tests -m $* -q --tb=short --no-header -rs -s; rc=$$?; \
	  $(MAKE) workerd-stop; exit $$rc

# drains a testnet's live-test accounts back into its wallet (the float), to exactly 0
%-sweep:
	"$(PY)" -m $(SRC).tests.live $* sweep

# deploys, from a testnet's wallet, what the relay needs that the network lacks: the runner's
# deployer and Calibur, through Arachnid's deployer (each skipped if it's there). Spends ETH
%-deploy:
	"$(PY)" -m $(SRC).tests.live $* deploy

clean:
	rm -rf "$(VENV)" "$(TOOLS)/cache" .ruff_cache .deno

distclean: clean
	rm -rf "$(TOOLS)" dist
