# Bruno Python code style

> Strict, honest typing and a straightforward path to idiomatic Rust.
> Where a Python-ism has no clean Rust analogue, prefer the form that does.

This describes rules and the reasons for them, not where things live: modules move, and
a guide that names paths goes stale. Where an example helps, it names a concept ("the
wire module", "the planner"), which outlasts a file name.

## 0. Toolchain and gates

Dev tooling lives under the project: `make install` puts `uv` in `./.uv` and a `./.venv`
with the package installed editable plus dev tools. Nothing touches `$HOME`.

The gate is `make check`: `ruff check`, `ruff format --check`, `ty check`, `pyrefly
check` (strict preset) and `pytest` against anvil. It runs in full in CI on every pull
request; while working, run what a change touches (`make lint typecheck`, and the tests
it affects). `make coverage` runs the suite under coverage and reports every module's
untested lines (see §9).

Target Python 3.14. Use PEP 695 type parameter syntax (`def f[T]():`), deferred
annotations (PEP 649), and all 3.14 features.

## 1. Modern Python

- Built-in generics and unions: `list[T]`, `dict[K, V]`, `tuple[...]`, `X | None`. Never
  `typing.List`, `Optional`, `Union`.
- PEP 695 generics: `def f[T](cls: type[T]) -> T:` not `TypeVar`.
- `type` statement for type aliases (except in hydrated fields, see §6).
- `StrEnum` for wire values. `@override` on every ABC override.
- `Annotated[T, Parameter(...)]` for Litestar field descriptions.
- `from __future__ import annotations` is unnecessary on 3.14. Do not add.

## 2. Types are strict and honest

The type checker is a design tool, not a formality.

1. No `Any` except at a genuinely dynamic boundary (JSON-RPC params, wire format dicts).
   Bare `dict` is implicit `Any`: pyrefly strict rejects it; always specify
   `dict[str, X]`.
2. The wire-to-typed boundary is `HasFromDict.from_dict` (see §6): never raw dict access
   with string keys scattered through business logic.
3. Never add type ignore/suppress comments without understanding the error and getting
   explicit confirmation. Each suppression is a decision, not boilerplate.

### ABCs for polymorphic interfaces

A tool, a tool response, an operation, a relay: the ABC defines the interface, each
subclass owns its implementation. `@override` on every overridden method (pyrefly strict
enforces this).

`@dataclass(frozen=True, slots=True)` is the default for value types.

### `tuple` over `list` for immutable data

Wire data, function returns, fixed sequences: `tuple`. `list` only when you actually
mutate it.

### `TypedDict` for raw wire format shapes

A JSON structure with known keys gets a `TypedDict` to document and type-check the
expected shape. The typed domain object is a frozen dataclass hydrated from it via
`from_dict`.

## 3. The import graph is a DAG

Modules import each other in one direction: a lower module never imports a higher one,
directly or through others. There is no `if TYPE_CHECKING:` import and no import inside a
function: both exist only to hide a cycle, and a cycle means something is in the wrong
module.

When two modules need each other, find what they share and move it down:

- a type both name goes to the module both can import (or a new one beneath them);
- a function that needs something higher takes it as an argument, or becomes a method
  of the higher type;
- a callback the lower module calls is typed by a `Callable` or an ABC it owns, which
  the higher module implements.

Fix a cycle when you meet one; don't route around it.

## 4. Enums, not string constants

A closed set of values is an enum.

- Values on the wire: `StrEnum` (stable string `.value`): an invoice's status, an order's
  status, a headroom's source.
- Typed outcomes: a union of frozen dataclasses, not an enum: `Verified |
  VerificationFailed`.

## 5. Errors: structural, module-local

Each module owns its error types, raised where they arise, all descending from
`BrunoError` in the common module. There is no central `errors.py`. A module's errors
form a small tree of their own: the invoice module's not-found, expired and state errors
under its own base, the router's "no route" under its route error.

`BrunoError` subclasses are domain failures: tools report them to the client (an
`isError` result, a 4xx). Anything else is unexpected: logged, and answered with
`-32603` / 500. An upstream node's JSON-RPC error is deliberately not a `BrunoError`: a
node failing is unexpected, not the client's doing.

Result-shaped outcomes are not exceptions: `Verified | VerificationFailed` is returned,
not raised.

## 6. Serialization and schemas

Types are defined once as dataclasses with `Annotated[T, Parameter(...)]` for
descriptions, in the module that owns them. Litestar derives JSON Schema from these
types, and MCP tool definitions derive `inputSchema` from the same types.

All wire handling lives in the wire module; Litestar only routes and receives bytes.

- **In:** JSON is parsed with fractional numbers as `Decimal`, never `float`, and
  anything built from a dict inherits `HasFromDict` and is hydrated with
  `T.from_dict(data)`: strict keys, `Validated` casts, str → enum, list → tuple, a whole
  number to a declared `Decimal`. No other code parses dicts into typed values.
- **Out:** `encode(obj)` writes every response, error body, pipe message and commitment
  input: sorted keys, compact, raw UTF-8, `null` for `None`. Its input type `Encodable`
  lists what it accepts, so the type checker rejects anything else; at runtime it raises
  on anything it doesn't know, including `float`, `bytes` and bare `datetime`.
- **Opt-in:** a dataclass crosses the wire only by inheriting `WireStruct` (`HasFromDict`
  + `HasToWire`, encoded field by field; override `to_wire` for a different shape).
  Internal dataclasses (an invoice with its signer) are not encodable. Hydrate-only types
  (MCP params) use `HasFromDict` alone.
- **Round trip:** for every wire type, `T.from_dict(decode_object(encode(x))) == x`.
- **Scalar wire types** subclass `Validated`: `__new__` parses the wire form, `to_wire()`
  produces it, `json_schema()` describes it. A scalar earns its own type when it has a
  range or convention to enforce (an `Amount` is a uint256 written as a decimal string,
  since JavaScript loses precision past 2**53; a `Percent` is 0 to 100, "0.3 means 0.3%";
  a `Timestamp` is whole-second RFC 3339). A unit or a meaning alone is a field's name
  and description, not a type.

Wire types use the wire's field names, camelCase included; there is no renaming layer.
Each camelCase field carries its own `# noqa: N815` with the reason. Open namespaces (MCP
`_meta`) are typed `dict[str, Any]` and the entries we use are hydrated from them
explicitly.

Aliases used in hydrated fields are plain unions (`Url = SecureUrl | str`), not `type`
statements: dacite cannot see through `TypeAliasType`.

## 7. I/O lives at the edges

The MCP dispatcher handles protocol logic (no HTTP); the HTTP app handles HTTP (no
protocol logic); attestation handles crypto and subprocess calls. Business logic takes
typed values and returns typed values. A function that both computes and serves is the
smell.

## 8. Constants

A constant in the code is one of four things, and says which:

1. **A fact** of a protocol, a chain or a spec (a JSON-RPC error code, an EVM transfer's
   21,000 gas, a message's byte layout): cite where it comes from.
2. **A measurement** (a relayed batch's gas, a swap's): say what was measured, how and
   when, so it can be measured again.
3. **A choice** the user or operator made (a quote's acceptance window, a float the tests
   keep): say whose.
4. **An engineering bound** (a timeout, a poll interval, a search budget): name it for
   what it bounds, and why that size.

A figure that moves money (a gas estimate, a margin, a default headroom) is never a
guess: if it can't be sourced or measured, ask before adding it, and prefer deriving it
from what the system already knows (a relay's own fee, the pools' prices, the request's
own headroom). Each constant is defined once, in the module that owns it, and imported
everywhere else: two copies of one figure drift.

**Tests never copy a production value or guess a market figure.** They import the
constant, or ask the system: a quote says how much to provide (an input left open),
the pricing module says what tokens are worth, a relay says its fee. A test that hard-codes
a price, a fee or a margin breaks when the market moves or the code changes, silently
testing something else until it does: that's test debt.

Constants are audited now and then: each still one of the four, still sourced, still
used, still defined once.

## 9. Size, grouping and coverage

Packages group modules by concern, and a module holds one concern. These aren't limits,
but prompts to look:

- a module past about 600 lines, or a function past about 80, often holds two concerns;
- a package past about 20 modules often holds two or three concerns that would read
  better as subpackages;
- one change that touches many files means a concern is spread out: gather it.

`make coverage` reports each module's untested lines. Untested code is a decision, not
an accident: either a test of a real flow covers it, or the gap is known and noted.

## 10. Small things

- Line length 100; ruff formats. Don't hand-align against the formatter.
- Prefer a validating constructor at a boundary over trusting input and failing later.
  Parse, don't validate.
- Comments name the specific regression that returns silently if deleted. If you cannot
  name one, delete the comment.
- `per-file-ignores` in `ruff.toml` for structural reasons only (tests using `assert`,
  subprocess calls in chain tools), documented in the config.
