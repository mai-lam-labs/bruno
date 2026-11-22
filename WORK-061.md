# 0.6.1: what 0.6.0 left

A working note, kept out of the 0.6.0 branch and deleted before 0.6.1 merges. 0.6.0 (PR #18) is
the interface (the quote taking the call, the breakdown, the transfer to sign, refusals), the
funding as a bundle, the EntryPoint called directly, and calls spread across a chain's nodes.
What's below wasn't done in it.

## 0.6.1: atomic sponsorship

Designed and reviewed, not built: [sponsorship.md](docs/development/plans/sponsorship.md). Its
"Still to decide" section holds what's open before building:

- whether to build `transactions` alone first;
- whether the switching-over machinery goes (and the submission's `release` field with it);
- the sponsorship deadline's key and default (suggested: `sponsorship_deadline_blocks`, 3).

Its "Structure: what the code still needs" lists the four things the code lacks.

## Later

- **A refusal's data doesn't name the refusal.** An invalid-params refusal's `data` is the field
  at fault and, over a limit, `most` and `needs` (wire.py `InvalidField`). Each refusal has a
  name (refusals.py, `authority-undelegated`, `quote-no-gas`, `gas-over`, …) that isn't sent, and
  several share a field (those three, and `transaction-over`, are all `gas`), so a program tells
  them apart only by the message. Unavailable and unsupported errors already send theirs
  (`{category, reason}`). Sending the name as `data.reason` would give a client or an agent a
  stable code to act on, the message staying the prose. A wire addition, nothing broken without
  it; it changes errors-by-code's "data is `{field}`". To decide.
- **The client without `-dev`.** Its API settled, its `.d.ts` written as the reference a model
  reads, OpenAPI and OpenRPC aligned with it. Once it's not `-dev`, the API is a promise.
- **Leaner sending, the rest:** Safe and Multicall3 called without the runner, with the Safe
  work ([erc4337.md](docs/development/plans/erc4337.md)). The funding put ahead of the runs in
  a dropping bundle would save a block.
- **A Safe by one signature** ([safe.md](docs/development/plans/safe.md)): Safe's contracts
  vendored, deployed on first use, the SafeTx with a payment leg. Its four-item verification
  list first.
- **Money hygiene:** the cold wallet; persistence for the Python relay's queue and treasury
  snapshot, which it forgets on restart.
- **Base** ([morechains.md](docs/development/plans/morechains.md)): set aside while Bruno
  sends only through `eth_sendBundle`.
