/**
 * Swapping a token for ETH through Uniswap's SwapRouter02 (swap-router-contracts), in one call:
 * multicall of each leg of the token's path, then unwrapWETH9 paying the ETH out (the Python
 * reference's bruno/router.py, whose calldata make vectors checks this against).
 *
 * A leg is a run of hops of one kind: a v3 leg is exactInput along its pools, a v2 leg
 * swapExactTokensForTokens along its tokens (the router finds each pair from its own Uniswap V2
 * factory). Each leg's output stays in the router (ADDRESS_THIS); the first leg pays in from the
 * caller, each later one swaps the router's whole balance (CONTRACT_BALANCE). Only the last leg,
 * and the unwrap, hold to the minimum out.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { type Address, bytes, keccak, utf8 } from "./evm.ts";
import type { PoolKind } from "./prices.ts";
import { bytesOf } from "./transactions.ts";

/** swap-router-contracts' Constants: an amountIn of 0 swaps the router's own balance, and
 * address 2 is the router itself. */
export const CONTRACT_BALANCE = 0n;
export const ADDRESS_THIS = "0x0000000000000000000000000000000000000002" as Address;

const EXACT_INPUT = selector("exactInput((bytes,address,uint256,uint256))");
const SWAP_EXACT_TOKENS_FOR_TOKENS = selector(
  "swapExactTokensForTokens(uint256,uint256,address[],address)",
);
const UNWRAP_WETH9 = selector("unwrapWETH9(uint256,address)");
const MULTICALL = selector("multicall(bytes[])");
const APPROVE = selector("approve(address,uint256)");
const WITHDRAW = selector("withdraw(uint256)");

/** A hop of a token's path, as swapped: from `tokenIn` to `tokenOut` through a pool of `kind`,
 * its fee tier (v3; 0 for v2). */
export interface Step {
  readonly kind: PoolKind;
  readonly tokenIn: Address;
  readonly tokenOut: Address;
  readonly fee: number;
}

/** The router's multicall swapping `amountIn` of the first step's token along `steps` to WETH,
 * unwrapped, at least `minOut` wei paid to `recipient`. */
export function swapCalldata(
  steps: readonly Step[],
  amountIn: bigint,
  minOut: bigint,
  recipient: Address,
): Uint8Array {
  const calls: Uint8Array[] = [];
  const legs = legsOf(steps);
  for (const [i, leg] of legs.entries()) {
    const amount = i === 0 ? amountIn : CONTRACT_BALANCE;
    const least = i === legs.length - 1 ? minOut : 0n;
    const first = leg[0] as Step;
    if (first.kind === "uniswap-v3") {
      const path = concatBytes(
        bytes(first.tokenIn),
        ...leg.map((s) => concatBytes(bytesOf(BigInt(s.fee), 3), bytes(s.tokenOut))),
      );
      // one dynamic tuple: its offset, then (offset of path, recipient, amountIn, minOut, path)
      const tuple = concatBytes(
        word(0x80n),
        addressWord(ADDRESS_THIS),
        word(amount),
        word(least),
        tail(path),
      );
      calls.push(concatBytes(EXACT_INPUT, word(0x20n), tuple));
    } else {
      const tokens = [first.tokenIn, ...leg.map((s) => s.tokenOut)];
      calls.push(concatBytes(
        SWAP_EXACT_TOKENS_FOR_TOKENS,
        word(amount),
        word(least),
        word(0x80n),
        addressWord(ADDRESS_THIS),
        word(BigInt(tokens.length)),
        ...tokens.map(addressWord),
      ));
    }
  }
  calls.push(concatBytes(UNWRAP_WETH9, word(minOut), addressWord(recipient)));
  // bytes[]: its offset, its length, each element's offset from after the length, the elements
  const offsets: Uint8Array[] = [];
  let at = 32n * BigInt(calls.length);
  for (const call of calls) {
    offsets.push(word(at));
    at += BigInt(tail(call).length);
  }
  return concatBytes(
    MULTICALL,
    word(0x20n),
    word(BigInt(calls.length)),
    ...offsets,
    ...calls.map(tail),
  );
}

/** `steps` in runs of one pool kind. */
function legsOf(steps: readonly Step[]): Step[][] {
  const legs: Step[][] = [];
  for (const step of steps) {
    const last = legs[legs.length - 1];
    if (last !== undefined && (last[0] as Step).kind === step.kind) {
      last.push(step);
    } else {
      legs.push([step]);
    }
  }
  return legs;
}

/** ERC-20 approve(spender, amount). */
export function approveCalldata(spender: Address, amount: bigint): Uint8Array {
  return concatBytes(APPROVE, addressWord(spender), word(amount));
}

/** WETH's withdraw(amount): `amount` of the caller's WETH as ETH. */
export function withdrawCalldata(amount: bigint): Uint8Array {
  return concatBytes(WITHDRAW, word(amount));
}

function selector(signature: string): Uint8Array {
  return keccak(utf8(signature)).slice(0, 4);
}

function word(value: bigint): Uint8Array {
  return bytesOf(value, 32);
}

function addressWord(address: Address): Uint8Array {
  return concatBytes(new Uint8Array(12), bytes(address));
}

/** Dynamic bytes as ABI-encoded: their length, then the bytes padded to a word. */
function tail(data: Uint8Array): Uint8Array {
  const padded = new Uint8Array(Math.ceil(data.length / 32) * 32);
  padded.set(data);
  return concatBytes(word(BigInt(data.length)), padded);
}
