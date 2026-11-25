"""Sends a token from an account holding no ETH, as the account's own transaction, sponsored by
a Bruno relay over its REST API, with no client library. The relay sends the account the ETH
its transactions need, sends them as signed, and is paid in the same token by one more transfer
the account signs, all in one bundle.

    pip install httpx eth-account
    python sponsor.py

Set BRUNO_URL to the relay (https://bruno.mailam.app), CHAIN to the chain's id (11155111 for
Sepolia), PRIVATE_KEY to the account's key, TOKEN to the address of a token the relay takes,
RECIPIENT, AMOUNT in the token's smallest unit, and MAX, the most the account pays the relay,
in the token's smallest unit.
"""

import json
import os
import sys
import time

import httpx
from eth_account import Account

TRANSFER = "0xa9059cbb"  # ERC-20's transfer(address,uint256)
WAIT_SECONDS = 600  # how long the status is waited for


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"{name} isn't set")
    return value


def transfer(to: str, amount: int) -> str:
    """The calldata of transfer(to, amount)."""
    return TRANSFER + to[2:].lower().rjust(64, "0") + hex(amount)[2:].rjust(64, "0")


def check(holds: bool, why: str) -> None:
    """Stop before anything is signed if the quote isn't what was asked for."""
    if not holds:
        sys.exit(f"the quote isn't what was asked for: {why}")


def signed(account, transaction: dict[str, str]) -> str:
    """A transaction the quote filled in, signed without being sent: its raw bytes, as hex."""
    fields = {
        "type": 2,
        "chainId": int(transaction["chainId"], 16),
        "nonce": int(transaction["nonce"], 16),
        "to": transaction["to"],
        "data": transaction["data"],
        "value": int(transaction["value"], 16),
        "gas": int(transaction["gas"], 16),
        "maxFeePerGas": int(transaction["maxFeePerGas"], 16),
        "maxPriorityFeePerGas": int(transaction["maxPriorityFeePerGas"], 16),
    }
    return "0x" + account.sign_transaction(fields).raw_transaction.hex()


account = Account.from_key(env("PRIVATE_KEY"))
chain_id = int(env("CHAIN"))
token = env("TOKEN")
sending = transfer(env("RECIPIENT"), int(env("AMOUNT")))

with httpx.Client(base_url=f"{env('BRUNO_URL')}/{chain_id}/v1", timeout=60) as relay:
    # where a sponsorship's payment goes, read on its own so the quote can be checked against it
    forwarder = relay.get("/config").raise_for_status().json()["forwarder"]

    # 1. what the transfer costs, and what to sign: it, filled in, and the payment after it
    asked = relay.post(
        "/sponsorships/quote",
        json={
            "tokens": [token],
            "account": account.address,
            "transactions": [{"to": token, "data": sending}],
        },
    )
    if asked.status_code != 200:
        sys.exit(f"not quoted: {asked.text}")
    quote = asked.json()
    (ours,) = quote["transactions"]
    (priced,) = quote["quotes"]
    payment = priced["payment"]
    price = int(priced["amount"], 16)
    if price > int(env("MAX")):
        sys.exit(f"the price, {price}, is over MAX")

    # 2. check before signing: our transfer as we asked for it, then a transfer of the price to
    # the relay's forwarder, at the nonce after it, both for our chain, and nothing else. A
    # signed transaction stays valid, so it's signed only for the chain we mean
    check(quote["forwarder"].lower() == forwarder.lower(), "another forwarder")
    check(ours["to"].lower() == token.lower() and ours["data"] == sending, "not our transfer")
    check(int(ours["value"], 16) == 0, "our transfer sends ETH")
    check(payment["to"].lower() == token.lower(), "the payment isn't in our token")
    check(payment["data"] == transfer(forwarder, price), "the payment isn't the price")
    check(int(payment["value"], 16) == 0, "the payment sends ETH")
    check(int(payment["nonce"], 16) == int(ours["nonce"], 16) + 1, "the nonces don't follow")
    check(int(ours["chainId"], 16) == chain_id, "our transfer is for another chain")
    check(int(payment["chainId"], 16) == chain_id, "the payment is for another chain")

    # 3. submit: the transactions as signed, the payment last
    taken = relay.post(
        "/sponsorships",
        json={
            "account": account.address,
            "transactions": [signed(account, ours), signed(account, payment)],
            "payment": {"token": token, "payee": forwarder, "amount": priced["amount"]},
            "maxBlockNumber": quote["maxBlockNumber"],
        },
    )
    if taken.status_code not in (200, 202):
        sys.exit(f"not taken: {taken.text}")
    submission = taken.json()["id"]

    # its status is a submission's: wait until it's in a block, or has failed
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        status = relay.get(f"/submissions/{submission}").raise_for_status().json()
        if status["stage"] in ("confirming", "done", "failed"):
            break
        if time.monotonic() > deadline:
            sys.exit(f"{submission} is still {status['stage']} after {WAIT_SECONDS} seconds")
        time.sleep(1)

# included: the relay's first transaction, the account's two, and the relay's last
print(
    json.dumps(
        {
            "account": account.address,
            "status": status["status"],
            "reason": status.get("reason"),
            "transactions": status.get("transactionHashes", []),
            "paid": str(price),
        }
    )
)
if status["status"] != "included":
    sys.exit(1)
