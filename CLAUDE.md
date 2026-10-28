# Bruno — Claude Code guidelines

Bruno is a post-paid gas relay, a proof of concept kept self-contained. `docs/SPEC.md` is the
standard as a client sees it (the draft ERC: interface, contract, semantics); `docs/DEPLOYMENT.md`
running it; `docs/development/` working on it: `index.md` the tools and how it's tested, `design/`
the decisions behind Bruno and why, a page a topic, only what nothing else says (never the code,
whose docstrings carry the how, nor what the spec, the user docs or DEPLOYMENT.md say), `plans/` the
roadmap, known gaps included; the rest of `docs/` the documentation site, docs.mailam.app/bruno
(`make docs`), its overview included; `README.md` a pointer to it. The live target is Sepolia
(Glamsterdam, ePBS); everything is tested locally on anvil first, through a mock builder.

## Type errors and lint warnings

**Never reflexively add ignore/suppress comments to satisfy type checkers or linters.**

When a type error or lint warning appears:

1. **Understand the error.** What is the checker actually telling you? Is it a real type mismatch, a missing constraint, or a limitation in a third-party library's type stubs?
2. **Fix the root cause first.** Can the code be restructured to satisfy the checker? Is there a wrapper, a type narrowing, or a better API that avoids the issue?
3. **If suppression is genuinely needed**, explain why to the user and get explicit confirmation before adding an ignore comment. The comment must include a reason.
4. **Never batch-suppress.** Each suppression is a decision, not boilerplate.

This applies to `# noqa`, `# type: ignore`, `# pyrefly: ignore`, `# pyright: ignore`, per-file-ignores in ruff.toml, and any other mechanism that silences a checker.

The same goes for `Any`: never use it to satisfy a type checker. Find the correct concrete type (look up a generic's real parameters). If none exists, such as a third-party library's limitation, explain why and get explicit permission first.

The exception is per-file-ignores in `ruff.toml` for structural reasons documented in the config (e.g. tests using `assert`). Those are design decisions, not quick fixes.

## Error hierarchy

Errors are structural — each module owns its own error types inheriting from `BrunoError` in `common.py`. Do not create a centralised `errors.py`. When adding errors, put them in the module where they're raised.

## Types

Types are defined once as dataclasses in their owning module.

The HTTP interface uses Litestar and msgspec as they come, with no serialization layer of our own: its request and response types are msgspec Structs (`bruno/server/wire.py`), which Litestar decodes, validates and documents directly, each converting to or from the relay's dataclasses. msgspec also decodes the configuration into its dataclasses.

Use the object model. Never represent structured data as bare strings, tuples or ad-hoc dicts when a type exists for it. Something new gets its own dataclass.

No speculative types. Before adding one, check the owning module and its neighbours for an existing type of the same shape, and compose it. Don't add types "for later".

## Imports

Import names from the module that defines them. Don't add re-exports to a package's `__init__.py`, and don't restructure packages just to shorten imports.

## Python version

Target Python 3.14. Use modern features (PEP 649 deferred annotations, built-in generics, StrEnum). See `PYTHON-CODESTYLE.md`.

## Toolchain

`make check` must pass: ruff lint + ruff format + ty + pyrefly strict + the Worker's Deno format, lint, types and tests + pytest on Prague and Amsterdam, against both relays.

**Never run a tool binary directly**: not uv, ruff, ty, pyrefly, deno, workerd or anvil. Run it through its make target, adding one if none fits. The Makefile is the single source of truth for which checks run and how, and it points every tool's cache inside the project (`.uv/`, `.deno/`, `dist/`); run bare, a tool falls back to a cache in `$HOME`. Before adding a new tool, find where it puts caches and downloads by default.

While iterating, run only what a change affects: `make lint typecheck`, `make worker-check`, and pytest on the affected tests (`.venv/bin/python -m pytest bruno/tests/test_runner.py -k native`). Tests need `make anvil-start`, and the Worker's `make workerd-start`. Only one test run at a time: they share anvil.

## Python and the Worker

The Python relay (`bruno/`) is the reference; the Cloudflare Worker (`worker/`) is its port, module for module. Change both together, keeping their modules and names in step. What the Worker must reproduce byte for byte comes from the Python as golden vectors (`make vectors`), and the server tests run against both.

## Live runs

Live tests spend testnet funds and take minutes. Start one (`make check-sepolia`, deployments, sweeps) only when the user asks for that run; earlier approval doesn't carry over. Say how long it will take before asking.

## Tests

Test real flows and boundaries against anvil: the runner's initcode executed on-chain, the bundle path through the mock builder. No contrived tests that re-assert what the implementation obviously does.

## Specs

Implement external specs (EIP-7702, EIP-3009, x402, builder `eth_sendBundle`) exactly as written: fetch the normative text and follow it. Note genuine gaps briefly, as facts, not as proposals to deviate.

## Concurrency model

Single process, single asyncio event loop. Concurrency is async I/O, not threads or processes. Don't invent coordination: no watchers, event buses, schedulers or orchestrators. When X needs to know about Y, it holds a reference to Y and calls it directly.

## Self-contained project

Everything lives inside the project directory. Do not write to `/tmp`, `~/.local`, `~/`, or any path outside the working directory. Downloads, build artefacts, tool binaries, caches — all go under the project tree (`.uv/`, `dist/`, `.venv/`). One-off and exploratory scripts go in `scratchpad/` (gitignored).

## External repos

`~/Desktop/IntentSpace/teeswap/` is the project this one borrows its development model and fixtures from. Read from it; never write to it.
