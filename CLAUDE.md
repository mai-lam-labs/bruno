# Bruno — Claude Code guidelines

Bruno is a post-paid gas relay, a proof of concept kept self-contained. `SPEC.md` is the
standard as a client sees it (the draft ERC: interface, contract, semantics); `DESIGN.md` the
decisions and limits behind Bruno, never a description of the code; `DEPLOYMENT.md` running
it; `README.md` the overview. The live target is Sepolia (Glamsterdam, ePBS); everything is
tested locally on anvil first, through a mock builder.

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

`make check` must pass: ruff lint + ruff format + ty + pyrefly strict + pytest. Always use the Makefile, never run uv, ruff, ty, or pyrefly directly — the Makefile is the single source of truth for which checks run and how, and it keeps every tool's state inside the project.

While iterating, run only what a change affects: `make lint typecheck`, and pytest on the affected tests (`.venv/bin/python -m pytest bruno/tests/test_runner.py -k native`). Tests need `make anvil-start`. Only one test run at a time: they share anvil.

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
