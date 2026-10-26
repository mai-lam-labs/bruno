# SELFDESTRUCT and EIP-8037: outline

Audience: Ethereum engineers and core devs. Name the EIPs, don't restate them; our bytecode,
measurements and decisions are the content.

## Working title

"One account per transaction: what EIP-8037 charges a contract that cleans up after itself"

## Hook

A relay that creates a contract for every transaction it sends, and leaves nothing behind.
Under EIP-8037 as it stood, doing the right thing for state cost 183,600 gas more per
transaction than leaving the account there would have saved. (Check the framing against the
numbers below before using it.)

## 1. Why a contract per transaction

- The runner: initcode generated per submission, CREATE2'd by our deployer at an address bound
  to the sending account and the block (salt `keccak(caller ‖ block)`).
- Isolation is the point: a fresh address that nothing has ever approved, delegated to or
  trusted, so nothing granted to it can outlive the transaction or be reused by the next.
- Its payee, token, amount and call are constants in its code; it reads no calldata, trusts no
  input at run time.

## 2. What it used to leave behind

- It ended with `RETURN(0, 0)`: no code deployed, but the account exists: nonce 1, per
  EIP-161.
- One empty account per transaction, forever: state growth by design, for nothing.

## 3. SELFDESTRUCT, as EIP-6780 left it

- Created and destroyed in the same transaction, the account is deleted: the one case 6780
  kept.
- Ending: `ORIGIN SELFDESTRUCT` (`0x32ff`): 2 gas + the 5,000 base EIP-8038 keeps; the
  beneficiary is the account that sent the transaction, warm and existing, so nothing new is
  touched. (Why ORIGIN rather than the float: warm, already exists, no balance moves; the
  runner holds none.)
- Verified on Sepolia (live Amsterdam): the runner's address has no account after its
  transaction. (Link a transaction.)

## 4. What EIP-8037 charges

- The account's creation at `STATE_BYTES_PER_NEW_ACCOUNT × CPSB`: 183,600 gas on Amsterdam.
- No refill when the account is destroyed in the same transaction: decided in Repricings #6,
  dropped by execution-specs#2845 for bal-devnet-7. milonite_2's #46 asked for it back.
- The measurements we posted (#51), `eth_estimateGas` through our deployer
  (`0x48F47C9c5FC7bb594e31172a3FdEC10D8c51144c`), empty-return initcode `0x5f5ff3` vs
  `0x32ff`:

  | Network | `RETURN(0,0)` | `ORIGIN SELFDESTRUCT` |
  |---|---|---|
  | Sepolia (Amsterdam) | 213,621 | 218,643 |
  | Ethereum (Prague) | 53,975 | 58,977 |

  The same ~5,000 for cleaning up on both; the ~160,000 difference between networks is
  creation. Cleaning up earns nothing back.
- Authorizations too (DESIGN.md's table): intrinsic / new delegation / empty authority,
  Prague 25,000 / 0 / 0, Amsterdam 7,816 / 44,190 / 183,600.

## 5. The incentive this sets

- An account destroyed in its own transaction adds no state, yet is charged as if it did,
  forever. The pricing makes "leave it" and "clean up" cost the same creation charge, and
  cleaning up 5,000 more.
- Patterns hit: per-transaction contracts (runners, counterfactual wallets, one-shot
  executors), CREATE2-and-destroy flows. (Keep to what we can show; don't speculate on
  others' contracts.)

## 6. Test on the testnet that runs the fork

- Repricings land on patterns most projects never exercise outside mainnet; we found this
  because Bruno runs live on Sepolia's Amsterdam rules, not on a simulation.
- Bruno measures the gas schedule from the chain and refuses a fork it doesn't recognise
  rather than misprice: a schedule change stops it until it's understood.

## 7. Where it stands

- ACDE, 2026-10-22: the Hoodi migration was cancelled. 8037 came up through ChainSecurity's
  exploit, which works because state gas comes back when a child frame reverts: gas left
  rising mid-execution. Ansgar: if the spec changes, refunds and the reserve are the parts most
  likely revisited. SELFDESTRUCT and the refill thread weren't discussed.
- The argument this sets up: a same-transaction refill doesn't need to raise gas left during
  execution. The account is deleted at the end of the transaction; credit its creation charge
  there, at settlement, alongside the other end-of-transaction accounting. That's the version
  that survives the exploit's lesson.
- If it returns, the runner's Amsterdam cost drops by 183,600.

## To check before writing

- Current 8037 text; the ACDE recording for the exact wording (the notes came from a summary);
  ChainSecurity's write-up of the exploit, to cite.
- Re-measure the table if the devnet specs have moved since 2026-10-22.
- A Sepolia runner transaction to link, and its absent account afterwards.
- Links: Magicians thread (#46, #51), execution-specs#2845, the Repricings #6 notes.
