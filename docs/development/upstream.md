---
description: "Upstream issues and PRs: what Bruno ran into in others' code, specs and services, the issue or PR tracking each, the workaround in place, and what to do once it's fixed"
---

# Upstream issues

What Bruno ran into in others' packages, specs and services: where it's tracked, what works
around it here, and what to undo once it's fixed. Check the open ones when touching what they
affect, and at each release. A fixed one moves to the last table once its workaround is gone.

## Open, worked around

| Upstream | The problem | Our workaround | Once it's fixed |
|---|---|---|---|
| [coinbase/agentkit#1607](https://github.com/coinbase/agentkit/issues/1607) (ours), from [x402-foundation/x402#3391](https://github.com/x402-foundation/x402/issues/3391); fix in [coinbase/agentkit#1608](https://github.com/coinbase/agentkit/pull/1608), not merged | `@x402/svm` 2.29.0 needs `@solana/kit` 8, AgentKit 0.10.4 depends on kit 2: a fresh install of AgentKit doesn't load under pnpm or Bun (npm nests it, and loads). | `@x402/svm` held at 2.28.0: `overrides` in `npm-test/package.json` (npm, Bun) and `npm-test/pnpm-workspace.yaml` (pnpm 11+). | Drop both overrides once an AgentKit release has kit 8. |
| [zerodevapp/sdk#281](https://github.com/zerodevapp/sdk/issues/281) (ours) | `@zerodev/ecdsa-validator` 5.4.9 requires `tslib` without depending on it; its build's check for that can't fail, since the monorepo installs `tslib`. Hidden wherever something else brings `tslib` (AgentKit's tree does). | `tslib` in `npm-userops/package.json`, and in the install line of the four user docs' scripts that use the validator. | Drop `tslib` from both. |
| [zerodevapp/sdk#279](https://github.com/zerodevapp/sdk/issues/279) (ours), [zerodevapp/intent-sdk#33](https://github.com/zerodevapp/intent-sdk/issues/33) (ours), [coinbase/agentkit#1532](https://github.com/coinbase/agentkit/issues/1532) | AgentKit's dependencies' declarations type-check against no viem: `@zerodev/sdk` 5.5.11's need viem 2.45 or later (its peer range says 2.28), `@zerodev/intent` 0.0.28's import a type viem no longer exports, and AgentKit pins viem exactly. | `skipLibCheck` in `npm-test/tsconfig.json`; the client checked strictly on its own (`tsconfig.client.json`), and `smoke.ts` checking nothing it uses is `any`. | Drop `skipLibCheck` from `npm-test/` once all three are fixed. |
| Not filed | `@zerodev/ecdsa-validator` 5.4.9's declarations import viem's internals by path (`viem/_types/…`), which viem's exports don't allow: they don't resolve on current viem. | `skipLibCheck` in `npm-userops/tsconfig.json`. | File it; drop `skipLibCheck` from `npm-userops/` once fixed. |
| Not filed | MetaMask's Smart Accounts Kit, for its EIP-7702 account (`Implementation.Stateless7702`): refuses to name a factory until the EOA is delegated (an EIP-7702 account has none), names its contracts only on chains it lists, and reports its use to MetaMask unless `DO_NOT_TRACK` or `CI` is set. | The user docs' script answers `getFactoryArgs` with nothing, takes Ethereum's contracts, and is run with `DO_NOT_TRACK=1`. | Perhaps file the first; simplify the script if the kit changes. |

## Open, nothing to work around

| Upstream | The problem | Where we stand |
|---|---|---|
| [Uniswap/docs#1183](https://github.com/Uniswap/docs/issues/1183) (ours), after the Flashbots Collective's [Bundles are coming to Unichain](https://collective.flashbots.net/t/bundles-are-coming-to-unichain/5072/2?u=mailam) | Since Unichain moved to OP Enterprise (2026-10-29), its public RPC refuses `eth_sendBundle`, and its bundle docs page redirects elsewhere. OP Labs' Subblocks replaced op-rbuilder; OP Enterprise has no public contact. | Unichain, like the other OP Enterprise chains, is `upfront`-only ([More chains](plans/morechains.md)). Pushing OP Labs is sidelined. |
| [EIP-8037 on Ethereum Magicians](https://ethereum-magicians.org/t/eip-8037-state-creation-gas-cost-increase/25694/51?u=harryr) (ours) | The runner's SELFDESTRUCT under EIP-8037: the state gas refund must apply at the transaction's end. All Core Devs (2026-10-22) postponed Hoodi's move to Amsterdam; refunds are likely revisited. | The relay prices Amsterdam as Sepolia charges it today. Revisit when EIP-8037 changes. |
| Not filed | Circle's paymaster quickstart gives the paymaster a verification limit of 200,000 gas, short on Amsterdam (EIP-8037's state gas). A docs problem, and Amsterdam isn't on Ethereum yet. | The relay measures a paymaster's limits it isn't given. Perhaps a public note to Circle once Amsterdam's gas is settled. |
| Not filed | x402's payment nonces aren't bound to the request they pay for. | Planned (Priorities, Alongside). |

## Fixed, workaround still in place

| Upstream | The problem | What's left to do |
|---|---|---|
| [foundry-rs/foundry#17432](https://github.com/foundry-rs/foundry/pull/17432), merged 2026-10-07 | Anvil charged Prague's gas on Amsterdam (EIP-8037, EIP-2780). | The Makefile pins a nightly with the fix (`FOUNDRY_VERSION`); move to a release once one has it. |
