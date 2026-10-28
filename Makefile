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
#   make hoodi-deploy     deploy the runner's deployer, Calibur and x402's Permit2 proxy on a
#                         testnet (sepolia-…, unichain-sepolia-…, base-sepolia-… too)
#   make serve            run the relay (CONFIG=, PORT=, FLOAT_KEY=, PAYEE=)
#   make rpc-check        every configured node checked on its own (read-only)
#   make worker-check     the Worker's format, lint, types and tests (Deno); make vectors
#                         rewrites the vectors its tests check against
#   make publish-dry-run  the JSR packages (client, AgentKit) checked as JSR takes them;
#                         make publish publishes them (CI does, on a version tag)
#   make npm-test         the same packages packed for npm, installed by npm, pnpm and Bun in
#                         containers: type-checked and run, AgentKit's registry included;
#                         make npm-e2e runs an AgentKit agent from them against both relays
#   make sepolia-agentkit AgentKit's ZeroDevWalletProvider paying the Worker on Sepolia, from
#                         the npm packages (spends Sepolia ETH and USDC)
#   make lint-fix         sort import blocks (ruff's I001), then `make format`
#   make vendor           re-fetch the mainnet bytecode the tests install (RPC=… to override)
#   make gas-probe        in-EVM gas measured on a chain, read-only (GAS_URL=…, default anvil);
#                         make gas-receipts measures transactions' gas on the local anvil
#   make clean             remove .venv + caches  |  make distclean  also .uv + dist

SRC  := bruno
# what lint, format and the type checkers cover: the relay, and the build tools beside it
PYSRC := $(SRC) tools
PYVER := 3.14

TOOLS := $(CURDIR)/.uv
UV    := $(TOOLS)/uv
# uv, pinned: its installer for that version, by hash.
# The installer holds each platform's archive hash and checks the download against it
# with sha256sum, so pinning the installer pins uv itself
# 0.13.5 at least: its root store drops eMudhra's emSign ECC Root CA - G3, and it refuses chains
# through eMudhra's compromised intermediates (October 2026)
UV_VERSION := 0.13.5
UV_INSTALLER_SHA256 := 1c6e7f025499664633ac2dce43a210800510e9ef0625c52e034a8afdf8bf177a
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
# 2.9.8 at least: it distrusts eMudhra's compromised certificate authorities (October 2026)
DENO_VERSION     := v2.9.8
DENO_SHA256      := e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609
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
CLIENT           := clients/typescript
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
# interpreters stay in ./.uv), then `make install`; UPGRADE="pkg ..." moves those packages to
# their latest versions too
lock: uv-bootstrap
	"$(UV)" lock --python $(PYVER) $(foreach p,$(UPGRADE),--upgrade-package $(p))

# the venv is what uv.lock says: a check that passes here passes in CI
env-check:
	@"$(UV)" sync --locked --group dev --python $(PYVER) --check >/dev/null 2>&1 || \
	  { echo "error: .venv differs from uv.lock: run 'make install'"; exit 1; }

lint:
	"$(RUFF)" check $(PYSRC)

format:
	"$(RUFF)" format $(PYSRC)

# sorts import blocks (ruff's isort rule, I001), and fixes nothing else
lint-fix:
	"$(RUFF)" check --fix --select I $(PYSRC)

format-check:
	"$(RUFF)" format --check $(PYSRC)

typecheck:
	"$(TY)" check $(PYSRC)
	"$(PYREFLY)" check --min-severity warn $(SRC)

test:
	"$(PY)" -m pytest $(SRC)/tests -q --tb=short --no-header --durations=0 -vv


# the relay, for trying it by hand: http://127.0.0.1:$(PORT)/ for its chains and documents.
# CONFIG is its configuration (DEPLOYMENT.md): by default dist/config.json, config.default.json
# with config.local.json (gitignored: nodes with API keys, say) merged over it. FLOAT_KEY, if given, is the float's key file for every chain,
# overriding the configuration's; PAYEE the payee of every chain that doesn't name its own;
# PAYEE_KEY the payee's key, running the treasuries the configuration names (by default
# .secrets/payee.key, unless PAYEE names another payee; PAYEE_KEY= runs none)
SECRETS := .secrets
CONFIG ?= dist/config.json
PORT ?= 8402
FLOAT_KEY ?= $(SECRETS)/float.key
PAYEE ?=
PAYEE_KEY ?= $(if $(PAYEE),,$(SECRETS)/payee.key)

# config.default.json with config.local.json merged over it, if there is one: objects key by
# key, lists and values replaced whole. What serve, preflight, verify and worker-deploy read
dist/config.json: config.default.json $(wildcard config.local.json)
	mkdir -p dist
	"$(PY)" -m $(SRC).config config.default.json $(wildcard config.local.json) > $@.tmp
	mv $@.tmp $@

serve: $(CONFIG) $(if $(filter $(SECRETS)/float.key,$(FLOAT_KEY)),$(SECRETS)/float.key) $(if $(PAYEE),,$(SECRETS)/payee.address)
	"$(PY)" -m $(SRC).server --config "$(CONFIG)" --port $(PORT) --float-key "$(FLOAT_KEY)" \
	  --payee "$(if $(PAYEE),$(PAYEE),$$(cat $(SECRETS)/payee.address))" \
	  $(if $(PAYEE_KEY),--payee-key "$(PAYEE_KEY)")

# the float and the payee `make serve` uses unless given FLOAT_KEY= or PAYEE=: each key created
# the first time it's needed, never overwritten, in .secrets/ (gitignored); only addresses
# printed. The float pays the concurrency accounts' gas, so fund its address; the payee
# receives the payments
$(SECRETS)/float.key $(SECRETS)/payee.key:
	@"$(PY)" -c "from pathlib import Path; from $(SRC).accounts import new_key; print('$@:', new_key(Path('$@')).address)"

$(SECRETS)/payee.address: $(SECRETS)/payee.key
	"$(PY)" -c "from pathlib import Path; from $(SRC).accounts import read_key; print(read_key(Path('$<')).address)" > $@



# the tests need anvil and the Worker: started here, stopped after, as for check
coverage: anvil-start workerd-start
	$(MAKE) coverage-run; rc=$$?; $(MAKE) workerd-stop anvil-stop; exit $$rc

coverage-run:
	"$(PY)" -m coverage run --source=$(SRC) -m pytest $(SRC)/tests -q --tb=short --no-header
	"$(PY)" -m coverage report -m --fail-under=70

# the CI gate: lint and types once, then the tests on a local chain at each fork
check: env-check lint format-check typecheck worker-check client-check integrations-check
	$(MAKE) test-fork ANVIL_HARDFORK=prague
	$(MAKE) test-fork ANVIL_HARDFORK=amsterdam

# the tests on a local chain at ANVIL_HARDFORK, started here and stopped after
test-fork: anvil-start workerd-start
	$(MAKE) test; rc=$$?; $(MAKE) workerd-stop anvil-stop; exit $$rc

ci: check

# a long randomised run on the local chain's fast clock, against both relays: its load seeded
# (BURNIN_SEED, printed, to replay), BURNIN_BLOCKS long. Minutes; not part of check
BURNIN_SEED   ?=
BURNIN_BLOCKS ?= 120
burn-in: anvil-start workerd-start
	BURNIN_SEED=$(BURNIN_SEED) BURNIN_BLOCKS=$(BURNIN_BLOCKS) "$(PY)" -m pytest $(SRC)/tests -m burnin \
	  -q --tb=short --no-header -rs -s -p no:logging; rc=$$?; $(MAKE) workerd-stop anvil-stop; exit $$rc

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

# the figures the forks' gas schedules (bruno/forks.py) are checked against: in-EVM gas by
# eth_call with a state override (any chain, read-only), and transactions' gas from receipts
# (the local anvil only: it changes the chain)
GAS_URL ?= http://127.0.0.1:$(ANVIL_PORT)
gas-probe: env-check
	"$(PY)" tools/gas_probe.py "$(GAS_URL)"

gas-receipts: env-check
	"$(PY)" tools/tx_gas_receipts.py http://127.0.0.1:$(ANVIL_PORT)

# what the Worker takes from the Python reference: the golden vectors its port is checked
# against, the OpenRPC and OpenAPI documents it serves, and its root's page; formatted as
# worker-check expects
vectors: deno-fetch
	"$(PY)" -m $(SRC).tests.vectors > $(WORKER)/test/vectors.json
	"$(PY)" -c "import json, sys; from $(SRC).server.rpc import openrpc; json.dump(openrpc([]), sys.stdout, indent=1)" > $(WORKER)/src/openrpc.json
	"$(PY)" -c "import json, sys; from $(SRC).server.app import create_app; json.dump(create_app({}).openapi_schema.to_schema(), sys.stdout, indent=1)" > $(WORKER)/src/openapi.json
	"$(PY)" -c "import json, sys; from $(SRC).server.front import ACCOUNTS, PAGE, TREASURY; from $(SRC).server.operations import DOCUMENTATION, REPOSITORY; json.dump({'template': PAGE.template, 'treasury': TREASURY.template, 'accounts': ACCOUNTS.template, 'documentation': DOCUMENTATION, 'repository': REPOSITORY}, sys.stdout, indent=1)" > $(WORKER)/src/front.json
	"$(PY)" -c "import json, sys; from $(SRC).server.x402 import exported; json.dump(exported(), sys.stdout, indent=1)" > $(WORKER)/src/x402.json
	"$(PY)" -c "import json, sys; from $(SRC).userops import SIMULATIONS_CODE; json.dump({'entryPointSimulations': SIMULATIONS_CODE}, sys.stdout, indent=1)" > $(WORKER)/src/userops.json
	cd $(WORKER) && "$(DENO)" fmt test/vectors.json src/openrpc.json src/openapi.json src/front.json src/x402.json src/userops.json

# the Worker bundled into one module, which workerd and Cloudflare run
worker-build: deno-fetch
	cd $(WORKER) && "$(DENO)" bundle --frozen --platform browser --output ../dist/worker/worker.js src/index.ts

# formatting, lint and types (strict), then the tests against the Python reference's vectors
worker-check: deno-fetch
	cd $(WORKER) && "$(DENO)" fmt --check && "$(DENO)" lint && "$(DENO)" check src test deploy.ts
	cd $(WORKER) && "$(DENO)" test --frozen --allow-read=test test

worker-format: deno-fetch
	cd $(WORKER) && "$(DENO)" fmt

# the TypeScript client (@mailam/bruno): formatting, lint and types (strict), then its tests,
# which check what it sends against the Worker's own parsing and recovery
client-check: deno-fetch
	cd $(CLIENT) && "$(DENO)" fmt --check && "$(DENO)" lint && "$(DENO)" check mod.ts test
	cd $(CLIENT) && "$(DENO)" test test

client-format: deno-fetch
	cd $(CLIENT) && "$(DENO)" fmt

# the integrations on the client (integrations/*): each formatted, linted, typed and tested
# against stand-ins; their npm dependencies fetched into .deno
INTEGRATIONS := integrations/agentkit
integrations-check: deno-fetch
	for i in $(INTEGRATIONS); do (cd $$i && "$(DENO)" fmt --check && "$(DENO)" lint && \
	  "$(DENO)" check mod.ts test) || exit 1; done
	$(MAKE) integrations-test

integrations-test: deno-fetch
	for i in $(INTEGRATIONS); do (cd $$i && "$(DENO)" test --allow-env test) || exit 1; done

integrations-format: deno-fetch
	for i in $(INTEGRATIONS); do (cd $$i && "$(DENO)" fmt) || exit 1; done

# the JSR packages (@mailam/bruno, @mailam/bruno-agentkit), from the Deno workspace (deno.json):
# checked as JSR would take them, or published: by CI on a version tag (GitHub's OIDC, no
# token), or from here, approved in the browser. A version already on JSR is skipped
publish-dry-run: deno-fetch
	"$(DENO)" publish --dry-run --allow-dirty

publish: deno-fetch
	"$(DENO)" publish

# the JSR packages as npm tarballs (dist/npm), built by Deno as JSR's npm compatibility layer
# builds them (the same transpile, the same fast-check .d.ts): what npm-test installs
NPM_DIST := dist/npm
npm-pack: deno-fetch
	mkdir -p $(NPM_DIST)
	cd $(CLIENT) && "$(DENO)" pack --allow-dirty --output "$(CURDIR)/$(NPM_DIST)/bruno.tgz"
	cd integrations/agentkit && \
	  "$(DENO)" pack --allow-dirty --output "$(CURDIR)/$(NPM_DIST)/bruno-agentkit.tgz"

# the packed packages installed by npm, pnpm and Bun, each in its own container (npm-test/),
# no dependency's install scripts run: type-checked by tsc against their declarations (the
# client's alone strictly, tsconfig.client.json; with AgentKit as AgentKit's own projects
# check, skipLibCheck), and run by Node and Bun. Containers only, as the host's user, their downloads cached in
# dist/npm-cache; images pinned by digest. The test's own viem is AgentKit's, which pins it
# exactly: a WalletClient of any other viem isn't its type
NODE_IMAGE := node:24-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205
BUN_IMAGE  := oven/bun:1-slim@sha256:1846adf22496bd711036aae8be147e57bdebbaea2e5264e3e0b5af1db187d9a4
NPM_CACHE  := dist/npm-cache
NPM_RUN     = mkdir -p $(NPM_CACHE)/$(1) && docker run --rm --user "$$(id -u):$$(id -g)" \
	-e HOME=/tmp/home -e npm_config_cache=/cache/npm -e BUN_INSTALL_CACHE_DIR=/cache/bun \
	-v "$(CURDIR)/npm-test:/src:ro" -v "$(CURDIR)/$(NPM_DIST):/pkg:ro" \
	-v "$(CURDIR)/$(NPM_CACHE)/$(1):/cache" $(2) sh -euc \
	'cp -r /src /tmp/app && cp -r /pkg /tmp/app/pkg && cd /tmp/app && $(3)'
npm-test: npm-pack
	$(call NPM_RUN,npm,$(NODE_IMAGE),npm install --no-audit --no-fund --ignore-scripts && \
	  npx tsc -p tsconfig.client.json && npx tsc -p . && node smoke.ts)
	$(call NPM_RUN,pnpm,$(NODE_IMAGE),npx --yes pnpm@12 install --store-dir /cache/store --ignore-scripts && \
	  ./node_modules/.bin/tsc -p tsconfig.client.json && ./node_modules/.bin/tsc -p . && \
	  node smoke.ts)
	$(call NPM_RUN,bun,$(BUN_IMAGE),bun install --ignore-scripts && \
	  bun ./node_modules/typescript/bin/tsc -p tsconfig.client.json && \
	  bun ./node_modules/typescript/bin/tsc -p . && bun smoke.ts)

# AgentKit end to end (bruno/tests/test_agentkit.py): the packed packages installed by npm into
# dist/npm-e2e, an agent run from there by Node in a container on the host's network, against
# both relays on the local chain
NPM_E2E := dist/npm-e2e
npm-e2e: npm-e2e-install anvil-start workerd-start
	NODE_IMAGE=$(NODE_IMAGE) NPM_E2E_APP="$(CURDIR)/$(NPM_E2E)" "$(PY)" -m pytest \
	  $(SRC)/tests -m "npm and not sepolia" -q --tb=short --no-header -rs

# the packed packages and the agent (npm-test/) installed by npm into dist/npm-e2e, in a container
npm-e2e-install: npm-pack
	rm -rf $(NPM_E2E) && mkdir -p $(NPM_E2E) $(NPM_CACHE)/npm
	cp -r npm-test/. $(NPM_E2E)/ && cp -r $(NPM_DIST) $(NPM_E2E)/pkg
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp/home -e npm_config_cache=/cache/npm \
	  -v "$(CURDIR)/$(NPM_E2E):/app" -v "$(CURDIR)/$(NPM_CACHE)/npm:/cache" -w /app $(NODE_IMAGE) \
	  npm install --no-audit --no-fund --ignore-scripts

# AgentKit's own ZeroDevWalletProvider on Sepolia (test_live.py): the agent installed as for
# npm-e2e, paying the Worker under workerd there. Spends Sepolia ETH and USDC; only when asked
sepolia-agentkit: npm-e2e-install workerd-start
	NODE_IMAGE=$(NODE_IMAGE) NPM_E2E_APP="$(CURDIR)/$(NPM_E2E)" "$(PY)" -m pytest \
	  $(SRC)/tests/test_live.py -m "sepolia and npm" -q --tb=short --no-header -rs -s; \
	  rc=$$?; $(MAKE) workerd-stop; exit $$rc

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
# configuration, the payee (else .secrets/payee.address), the float's key file and the payee's
# (else .secrets/payee.key, unless WORKER_PAYEE names another; empty, no treasury). In order:
# worker-deploy, worker-secret, worker-domain. deploy.ts reaches only api.cloudflare.com and
# reads only the files it's given
CLOUDFLARE       ?= $(SECRETS)/cloudflare
WORKER_NAME      ?= bruno
WORKER_DOMAIN    ?= bruno.mailam.app
WORKER_CONFIG    ?= dist/config.json
WORKER_PAYEE     ?=
WORKER_FLOAT_KEY ?= $(SECRETS)/float.key
WORKER_PAYEE_KEY ?= $(if $(WORKER_PAYEE),,$(SECRETS)/payee.key)
# the runtime the Worker's tested on locally (workerd.capnp)
WORKER_COMPAT    := $(shell sed -n 's/.*compatibilityDate = "\([0-9-]*\)".*/\1/p' $(WORKER)/workerd.capnp)
# deploy.ts, reading the credentials and the files $(1) names (space-separated) only
comma := ,
space := $(subst ,, )
DEPLOY = cd $(WORKER) && "$(DENO)" run --frozen --allow-net=api.cloudflare.com \
  --allow-read=$(subst $(space),$(comma),$(strip $(abspath $(CLOUDFLARE) $(1)))) deploy.ts

# create the Worker, or replace its code and bindings (its secrets kept): CONFIG checked first,
# every chain against its live nodes (preflight), and nothing deployed if one isn't ready
worker-deploy: worker-build $(WORKER_CONFIG) $(if $(WORKER_PAYEE),,$(SECRETS)/payee.address)
	"$(PY)" -m $(SRC).preflight "$(WORKER_CONFIG)" \
	  "$(if $(WORKER_PAYEE),$(WORKER_PAYEE),$$(cat $(SECRETS)/payee.address))"
	$(call DEPLOY,dist/worker/worker.js $(WORKER_CONFIG)) \
	  deploy --credentials=$(abspath $(CLOUDFLARE)) --name=$(WORKER_NAME) --compat=$(WORKER_COMPAT) \
	  --bundle=$(abspath dist/worker/worker.js) --config=$(abspath $(WORKER_CONFIG)) \
	  --payee="$(if $(WORKER_PAYEE),$(WORKER_PAYEE),$$(cat $(abspath $(SECRETS)/payee.address)))"

# the float's key, as the Worker's FLOAT_KEY secret, and the payee's as PAYEE_KEY (if any): never
# shown, their addresses printed
worker-secret: deno-fetch $(if $(filter $(SECRETS)/float.key,$(WORKER_FLOAT_KEY)),$(SECRETS)/float.key)
	$(call DEPLOY,$(WORKER_FLOAT_KEY)) \
	  secret --credentials=$(abspath $(CLOUDFLARE)) --name=$(WORKER_NAME) --key=$(abspath $(WORKER_FLOAT_KEY))
	$(if $(WORKER_PAYEE_KEY),$(call DEPLOY,$(WORKER_PAYEE_KEY)) \
	  secret --credentials=$(abspath $(CLOUDFLARE)) --name=$(WORKER_NAME) --key=$(abspath $(WORKER_PAYEE_KEY)) \
	  --binding=PAYEE_KEY)

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

# the mainnet bytecode the local chain installs (bruno/tests/fixtures/evm/): each of VENDOR's
# manifest entries fetched once and checked in, so setting up the chain needs no network.
# Re-run only to change what's vendored
RPC ?= https://ethereum-rpc.publicnode.com
vendor:
	"$(PY)" -m $(SRC).tests.vendor "$(RPC)" $(VENDOR)

# a testnet's wallet, the float the live tests spend from (NETWORK is hoodi or sepolia):
# created in .secrets/NETWORK/ (gitignored, kept by distclean) if there's none, and its address
# and balance printed, with a QR code of it (EIP-681), to fund from a faucet or a phone
%-wallet:
	"$(PY)" -m $(SRC).tests.live $*

# against a testnet itself, through a real builder (hoodi: Titan, dropping; sepolia: the nflaig
# ePBS builder, standalone): only when asked. Needs the network and a funded
# .secrets/NETWORK/wallet.key; spends testnet ETH. Ends by sweeping the test accounts back
# into the wallet, and saying what the run spent and how many more it can afford
check-hoodi check-sepolia: check-%: workerd-start
	before=$$("$(PY)" -m $(SRC).tests.live $* balance) && \
	  "$(PY)" -m pytest $(SRC)/tests -m "$* and not npm" -q --tb=short --no-header -rs -s; rc=$$?; \
	  $(MAKE) workerd-stop; \
	  "$(PY)" -m $(SRC).tests.live $* sweep $$before || rc=$$?; exit $$rc

# drains a testnet's live-test accounts back into its wallet (the float), to exactly 0
%-sweep:
	"$(PY)" -m $(SRC).tests.live $* sweep

# every chain CONFIG serves, checked against its live nodes, read-only: what the relay measures,
# every token priced, the treasury's router and WETH, every builder answering. Before deploying
preflight: $(CONFIG) $(if $(PAYEE),,$(SECRETS)/payee.address)
	"$(PY)" -m $(SRC).preflight "$(CONFIG)" "$(if $(PAYEE),$(PAYEE),$$(cat $(SECRETS)/payee.address))"

# every node CONFIG lists, each on its own, read-only: its chain, its head's lag, a batch of
# every method the relay reads by, and that batch back to back (a free tier's rate limit).
# Nodes named by host and their URL's last four characters, never in full
rpc-check: $(CONFIG)
	"$(PY)" -m $(SRC).rpccheck "$(CONFIG)"

# verifies a deployed relay with real transactions: through its URL (VERIFY_URL), a payment a
# unit short refused, a paid one landed, and one over x402 where it's taken, from a client the
# payee funds with the quotes, VERIFY_MOST whole tokens at most. Spends gas; only when asked
VERIFY_URL ?= https://bruno.mailam.app
VERIFY_MOST ?= 5
%-verify: $(CONFIG)
	"$(PY)" -m $(SRC).tests.verify $* "$(VERIFY_URL)" "$(CONFIG)" "$(VERIFY_MOST)"

# deploys the runner's deployer on NETWORK (ethereum, sepolia, hoodi), through Arachnid's and
# CONFIG's nodes, from KEY (by default .secrets/NETWORK/wallet.key; KEY=.secrets/float.key to
# pay from the float), unless it's there: checks what's there is its runtime. Spends ETH; only
# when asked
%-deployer-deploy: $(CONFIG)
	"$(PY)" -m $(SRC).deployer $* "$(if $(KEY),$(KEY),$(SECRETS)/$*/wallet.key)" "$(CONFIG)"

# deploys, from a testnet's wallet, what the relay needs that the network lacks: the runner's
# deployer, Calibur and x402's Permit2 proxy, through Arachnid's deployer (each skipped if it's
# there, each checked against the runtime the tests install). Spends ETH; only when asked
%-deploy:
	"$(PY)" -m $(SRC).tests.live $* deploy

# ---- the documentation site, docs.mailam.app/bruno (zensical.toml; Zensical's cache in .cache/) ----
# built into dist/docs, strict: a warning fails it; then its llms.txt, llms-full.txt and each
# page's Markdown beside its HTML (tools/llms.py). CI publishes it on every push to main
docs: env-check
	"$(VENV)/bin/zensical" build --strict
	"$(PY)" tools/llms.py zensical.toml dist/docs

# the site at http://localhost:8000/bruno/, rebuilt as its pages change; the llms files are
# `make docs`'s alone
docs-serve: env-check
	"$(VENV)/bin/zensical" serve

clean:
	rm -rf "$(VENV)" "$(TOOLS)/cache" .ruff_cache .deno

distclean: clean
	rm -rf "$(TOOLS)" dist
