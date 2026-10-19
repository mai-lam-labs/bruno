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
#   make serve            run the relay (CONFIG=, PORT=, FLOAT_KEY=)
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


# the relay, for trying it by hand: http://127.0.0.1:$(PORT)/schema for its OpenAPI documents.
# CONFIG is its configuration (DEPLOYMENT.md); FLOAT_KEY, if given, is the float's
# key file for every chain, overriding the configuration's
CONFIG ?= config.local.json
PORT ?= 8402
FLOAT_KEY ?=

serve:
	"$(PY)" -m $(SRC).server --config "$(CONFIG)" --port $(PORT) $(if $(FLOAT_KEY),--float-key "$(FLOAT_KEY)")



# the tests need anvil: started here, stopped after, as for check
coverage: anvil-start
	$(MAKE) coverage-run; rc=$$?; $(MAKE) anvil-stop; exit $$rc

coverage-run:
	"$(PY)" -m coverage run --source=$(SRC) -m pytest $(SRC)/tests -q --tb=short --no-header
	"$(PY)" -m coverage report -m --fail-under=70

# the CI gate: lint and types once, then the tests on a local chain at each fork
check: env-check lint format-check typecheck
	$(MAKE) test-fork ANVIL_HARDFORK=prague
	$(MAKE) test-fork ANVIL_HARDFORK=amsterdam

# the tests on a local chain at ANVIL_HARDFORK, started here and stopped after
test-fork: anvil-start
	$(MAKE) test; rc=$$?; $(MAKE) anvil-stop; exit $$rc

ci: check

build:
	"$(UV)" build --python "$(PY)" --out-dir dist

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
check-hoodi check-sepolia: check-%:
	"$(PY)" -m pytest $(SRC)/tests -m $* -q --tb=short --no-header -rs -s

# drains a testnet's live-test accounts back into its wallet (the float), to exactly 0
%-sweep:
	"$(PY)" -m $(SRC).tests.live $* sweep

# deploys, from a testnet's wallet, what the relay needs that the network lacks: the runner's
# deployer and Calibur, through Arachnid's deployer (each skipped if it's there). Spends ETH
%-deploy:
	"$(PY)" -m $(SRC).tests.live $* deploy

clean:
	rm -rf "$(VENV)" "$(TOOLS)/cache" .ruff_cache

distclean: clean
	rm -rf "$(TOOLS)" dist
