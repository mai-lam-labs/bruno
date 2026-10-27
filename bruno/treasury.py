"""The treasury on one chain: the payee's revenue converted to ETH for the float, so the relay
keeps paying its own gas (docs/development/design/treasury.md).

Each run reads the float, its concurrency accounts and the payee in one batch, and measures
what the float and accounts spent since the last run (what they held then, and what the last
run forwarded if it landed, less what they hold now). Their target is `runway_days` of that
spend, at least enough to fund every account once for the largest transaction at the fee
ceiling. What they lack comes from the payee: its ETH above its reserve, then its WETH
unwrapped and its other tokens swapped, in the configuration's order, through Uniswap's
router, each paying the payee, which forwards the lot to the float. The payee keeps a reserve:
one run's gas at its max fee, x `funding_multiplier`; conversions refill it.

Every transaction's gas is measured (eth_estimateGas), with headroom: forks price gas
differently. A swap is measured once the router may spend its token, so each token is approved
ahead of its first swap, in a run of its own; the next run follows soon after.

A conversion, or the forward alone, goes ahead only if it costs (gas, and the pools' cut, taken
as the token's margin) at most its amount x (`max_cost_multiplier` - 1), unless the float would
run dry before the next run. The run's transactions go to every builder as one bundle, all or
none, for each of the next BUNDLE_BLOCKS blocks; nothing is waited for: the next run reads the
forward's receipt."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor

from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .accounts import derive
from .builders import send_bundle
from .bundle import atomic_params
from .chain import Chain, ChainError, lasting_fees, market_tip
from .common import BrunoError
from .config import ChainConfig, Hop, PoolKind, TreasuryConfig
from .deployer import DEPLOYER
from .evm import NATIVE_ASSET
from .maintenance import AccountsStatus, AccountState, Holding
from .net.jsonrpc import Call
from .relay import Relay, UnavailableError, fee_ceiling
from .router import Step, approve_calldata, swap_calldata, withdraw_calldata
from .transactions import Fees, SignedTransaction, sign, unsigned_size

logger = logging.getLogger(__name__)

# a run's bundle is offered for each of the next this many blocks: one builder or another
# builds one of them
BUNDLE_BLOCKS = 5
# how soon a run that failed (the nodes not answering, say) is tried again
RETRY_SECONDS = 300.0
SECONDS_A_DAY = 86_400
# how soon the run after one that approved tokens follows, the approvals landed
APPROVED_RETRY_SECONDS = 120.0
# a transaction's gas limit over its measured gas: the state may move before it lands
GAS_HEADROOM = Fraction(5, 4)
# how many times a conversion is sized, its gas measured again each time it's more
SIZINGS = 3
# what's left with the payee of a token taken over x402: its balance's slot stays set
KEPT_UNITS = 1
_UNLIMITED = 2**256 - 1
# an allowance the router spends from without approving again
_APPROVED = 2**128
_BALANCE_OF = keccak(text="balanceOf(address)")[:4]
_ALLOWANCE = keccak(text="allowance(address,address)")[:4]
_TOKEN0 = keccak(text="token0()")[:4]
_TOKEN1 = keccak(text="token1()")[:4]
_FEE = keccak(text="fee()")[:4]


class TreasuryError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A treasury run, at block `block`: what the float and its accounts held then (the next
    run measures spend from it); the float's spend, a day, as measured since the last run (None
    on the first); the float's target and the payee's reserve; how many transactions of
    `max_gas` the float and its accounts cover at the next block's fee; and, if the run
    forwarded ETH to the float, how much and in which transaction, looked for by the next run.
    The balances themselves are shown as last read (AccountsStatus), not from here."""

    at: int
    block: int
    float_balance: int
    accounts: tuple[int, ...]
    spend_a_day: int | None
    target: int
    reserve: int
    covers: int
    outcome: str
    forwarded: int = 0
    forward_tx: bytes | None = None
    # whether the run approved tokens: the next follows soon, to swap them
    approving: bool = False

    @property
    def held(self) -> int:
        """What the float and its accounts hold."""
        return self.float_balance + sum(self.accounts)


@dataclass(frozen=True, slots=True)
class _Conversion:
    """One token converted to ETH at the payee: its transaction's call and gas, and the least
    ETH it pays the payee."""

    token: ChecksumAddress
    amount: int
    to: ChecksumAddress
    data: bytes
    gas: int
    min_out: int


class Treasury:
    def __init__(self, relay: Relay, payee: LocalAccount, settings: TreasuryConfig) -> None:
        """The treasury of `relay`'s chain, `payee` its payee's key."""
        if payee.address != relay.payee:
            raise TreasuryError(f"the payee key is {payee.address}'s, not {relay.payee}'s")
        if payee.address == relay.float_account.address:
            raise TreasuryError("the payee is the float: their nonces would collide")
        self._relay = relay
        self._payee = payee
        self._settings = settings
        self._routes: dict[ChecksumAddress, tuple[Step, ...]] = {}
        # each token's conversion gas and its transaction's most bytes unsigned, as last
        # measured: the reserve counts a token the payee holds none of now, for when it does
        self._gas: dict[ChecksumAddress, int] = {}
        self._sizes: dict[ChecksumAddress, int] = {}
        self.snapshot: Snapshot | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def _chain(self) -> Chain:
        return self._relay.chain

    @property
    def _config(self) -> ChainConfig:
        return self._relay.config

    def start(self) -> None:
        """A run now, and every `interval_seconds`."""
        self._task = asyncio.create_task(self._run_every())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run_every(self) -> None:
        while True:
            try:
                snapshot = await self.run()
                delay = (
                    min(APPROVED_RETRY_SECONDS, self._settings.interval_seconds)
                    if snapshot.approving
                    else self._settings.interval_seconds
                )
            except Exception:
                logger.exception("chain %d: a treasury run", self._chain.chain_id)
                delay = min(RETRY_SECONDS, self._settings.interval_seconds)
            await asyncio.sleep(delay)

    async def run(self) -> Snapshot:
        """One run: read, measure, convert and forward what's needed, sent as a bundle. The
        run's snapshot, kept as `snapshot`. UnavailableError on a chain that's unavailable:
        nothing is done until it's available."""
        known = await self._relay.available()
        schedule = known.schedule
        if schedule is None:
            raise UnavailableError("no schedule measured")
        latest = await self._chain.latest(known.op_stack)
        # valid for as long as the bundle is offered; and as the next block's are, for what a
        # transaction costs now
        fees = lasting_fees(latest, market_tip(latest.tips), BUNDLE_BLOCKS)
        fees_now = lasting_fees(latest, fees.max_priority_fee_per_gas, 0)
        float_address = self._relay.float_account.address
        accounts = [
            derive(self._relay.float_account, i).address for i in range(self._config.accounts)
        ]
        # the tokens measured: a suspended one waits until it's measured again
        tokens = [
            to_checksum_address(t.address)
            for t in self._config.tokens
            if to_checksum_address(t.address) != NATIVE_ASSET
            and to_checksum_address(t.address) in known.tokens
        ]
        payee = self._payee.address
        router = to_checksum_address(self._settings.router)
        last = self.snapshot
        calls = [Call("eth_getBalance", [a, "latest"]) for a in [float_address, *accounts, payee]]
        calls.append(Call("eth_getTransactionCount", [payee, "latest"]))
        calls += [
            Call("eth_getTransactionCount", [a, "latest"]) for a in [float_address, *accounts]
        ]
        for token in tokens:
            calls.append(_eth_call(token, _BALANCE_OF + _word(payee)))
            calls.append(_eth_call(token, _ALLOWANCE + _word(payee) + _word(router)))
        if last is not None and last.forward_tx is not None:
            calls.append(Call("eth_getTransactionReceipt", ["0x" + last.forward_tx.hex()]))
        answers = [a.value() for a in await self._chain.batch(calls)]
        float_balance = int(answers[0], 16)
        account_balances = tuple(int(a, 16) for a in answers[1 : 1 + len(accounts)])
        payee_eth = int(answers[1 + len(accounts)], 16)
        nonce = int(answers[2 + len(accounts)], 16)
        # a float with no balance and no nonce isn't in the state: the forward creates it
        float_nonce = int(answers[3 + len(accounts)], 16)
        account_nonces = tuple(
            int(a, 16) for a in answers[4 + len(accounts) : 4 + 2 * len(accounts)]
        )
        float_exists = float_balance > 0 or float_nonce > 0
        transfer_gas = schedule.transfer(new_recipient=not float_exists)
        at = 4 + 2 * len(accounts)
        balances = {t: int(answers[at + 2 * i], 16) for i, t in enumerate(tokens)}
        allowances = {t: int(answers[at + 2 * i + 1], 16) for i, t in enumerate(tokens)}
        self._relay.record_accounts(
            AccountsStatus(
                latest.number,
                float_balance,
                float_nonce,
                tuple(
                    AccountState(i, a, account_balances[i], account_nonces[i])
                    for i, a in enumerate(accounts)
                ),
                payee_eth,
                nonce,
                tuple(Holding(t, balances[t], known.decimals[t], known.symbols[t]) for t in tokens),
            )
        )
        self._relay.record_payee(balances)
        # what may be converted: all of a token but a base unit of one taken over x402, so the
        # payee's balance never returns to zero, and a transfer to it never sets a new slot
        spare = {
            t: max(0, b - (KEPT_UNITS if t in self._relay.domains else 0))
            for t, b in balances.items()
        }
        held = float_balance + sum(account_balances)
        now = int(time.time())

        # measured: what the float and accounts spent since the last run, a day
        spend_a_day: int | None = None
        if last is not None and now > last.at:
            receipt = answers[-1] if last.forward_tx is not None else None
            landed = last.forwarded if receipt is not None and receipt["status"] == "0x1" else 0
            spent = max(0, last.held + landed - held)
            spend_a_day = spent * SECONDS_A_DAY // (now - last.at)
        ceiling = fee_ceiling(latest, self._config.quote_blocks)
        # the largest call's own gas and bytes, the measure the float is sized by
        largest = self._size(DEPLOYER, bytes(self._config.max_call_bytes))
        transaction = ceiling.cost(self._config.max_gas, largest)
        minimum = ceil(self._config.accounts * Fraction(self._config.funding_multiplier)) * (
            transaction
        )
        target = max(
            minimum,
            0 if spend_a_day is None else ceil(Fraction(self._settings.runway_days) * spend_a_day),
        )
        need = max(0, target - held)
        # running dry: the float and accounts can't cover a transaction, or won't last until
        # the next run
        emergency = held < transaction or (
            spend_a_day is not None
            and held * SECONDS_A_DAY < spend_a_day * self._settings.interval_seconds
        )
        weth = self._weth
        # each token the router may not spend yet is approved, once, ahead of its swaps
        unapproved = [t for t in tokens if t != weth and allowances[t] < _APPROVED]
        approvals = [(t, approve_calldata(router, _UNLIMITED)) for t in unapproved]
        # each conversion's gas, measured on all the payee holds of its token
        convertible = [t for t in tokens if spare[t] > 0 and t not in unapproved]
        measured = await self._measure(
            [await self._call(t, spare[t], 0) for t in convertible] + approvals
        )
        gas_of = dict(zip(convertible, measured[: len(convertible)], strict=True))
        approval_gas = measured[len(convertible) :]
        notes = [f"{t}: no conversion would succeed" for t, g in gas_of.items() if g is None]
        self._gas.update({t: g for t, g in gas_of.items() if g is not None})
        for token, gas in gas_of.items():
            if gas is not None:
                self._sizes[token] = self._size(*await self._call(token, spare[token], 0))
        forward_size = self._size(float_address, b"", value=True)
        reserve = ceil(
            (
                fees.cost(transfer_gas, forward_size)
                + sum(fees.cost(g, self._sizes[t]) for t, g in self._gas.items())
            )
            * Fraction(self._config.funding_multiplier)
        )

        approved: list[tuple[ChecksumAddress, bytes, int]] = []
        conversions: list[_Conversion] = []
        # the payee's ETH as its transactions go, each at its max cost, the conversions paying
        # it their least out
        working = payee_eth
        for (token, data), gas in zip(approvals, approval_gas, strict=True):
            if gas is None or working < fees.cost(gas, self._size(token, data)):
                notes.append(f"{token}: can't approve the router")
                continue
            approved.append((token, data, gas))
            working -= fees.cost(gas, self._size(token, data))
        # ETH the payee must gain for the float's need, its own reserve and the forward's cost,
        # each transaction at its most
        shortfall = need + reserve + fees.cost(transfer_gas, forward_size) - working
        # a run that approves does only that: what its tokens' swaps cost is measured next run
        for token in [] if approved else convertible:
            measured_gas = gas_of[token]
            if shortfall <= 0:
                break
            if measured_gas is None:
                continue
            conversion = await self._conversion(
                token,
                spare[token],
                measured_gas,
                shortfall,
                fees,
                None if emergency else fees_now,
            )
            if conversion is None:
                continue
            size = self._size(conversion.to, conversion.data)
            cost = fees_now.cost(conversion.gas, size) + await self._cut(conversion)
            value = conversion.amount * await self._relay.prices.wei(token)
            if not emergency and cost > value * (Fraction(self._settings.max_cost_multiplier) - 1):
                notes.append(f"{token}: converting {conversion.amount} costs too much")
                continue
            most = fees.cost(conversion.gas, size)
            if working < most:
                # another, cheaper, may go ahead: an unwrap refills the payee's ETH
                notes.append(f"{token}: the payee holds too little ETH for its gas")
                continue
            working += conversion.min_out - most
            shortfall -= conversion.min_out - most
            conversions.append(conversion)

        forward = (
            0 if approved else min(need, working - fees.cost(transfer_gas, forward_size) - reserve)
        )
        if forward > 0 and not emergency:
            cost = fees_now.cost(transfer_gas, forward_size)
            if not conversions and cost > forward * (
                Fraction(self._settings.max_cost_multiplier) - 1
            ):
                notes.append(f"forwarding {forward} wei costs too much")
                forward = 0
        forward = max(forward, 0)

        txs: list[SignedTransaction] = []
        calls = approved + [(c.to, c.data, c.gas) for c in conversions]
        for to, data, gas in calls:
            txs.append(sign(self._payee, self._chain.chain_id, nonce, fees, gas, to, data))
            nonce += 1
        forward_tx: SignedTransaction | None = None
        if forward > 0:
            forward_tx = sign(
                self._payee,
                self._chain.chain_id,
                nonce,
                fees,
                transfer_gas,
                float_address,
                b"",
                forward,
            )
            txs.append(forward_tx)
        if txs:
            await self._send(latest.number + 1, txs)
            done = [f"approving {t}" for t, _, _ in approved]
            done += [f"converting {c.amount} of {c.token}" for c in conversions]
            if forward > 0:
                done.append(f"forwarding {forward} wei to the float")
            outcome = "sent: " + "; ".join(done)
        elif need == 0 and payee_eth >= reserve:
            outcome = "nothing to do"
        else:
            outcome = "nothing sent"
        if notes:
            outcome += " (" + "; ".join(notes) + ")"
        self.snapshot = Snapshot(
            at=now,
            block=latest.number,
            float_balance=float_balance,
            accounts=account_balances,
            spend_a_day=spend_a_day,
            target=target,
            reserve=reserve,
            covers=held // now_cost
            if (now_cost := fees_now.cost(self._config.max_gas, largest))
            else 0,
            outcome=outcome,
            forwarded=forward,
            forward_tx=None if forward_tx is None else forward_tx.hash,
            approving=bool(approved),
        )
        logger.info("chain %d: treasury: %s", self._chain.chain_id, outcome)
        return self.snapshot

    @property
    def _weth(self) -> ChecksumAddress:
        return to_checksum_address(self._config.weth)

    async def _call(
        self, token: ChecksumAddress, amount: int, min_out: int
    ) -> tuple[ChecksumAddress, bytes]:
        """The payee's call converting `amount` of `token` to ETH, at least `min_out` wei: WETH
        withdrawn, any other token swapped through the router."""
        if token == self._weth:
            return token, withdraw_calldata(amount)
        steps = await self._route(token)
        router = to_checksum_address(self._settings.router)
        return router, swap_calldata(steps, amount, min_out, self._payee.address)

    async def _measure(self, calls: Sequence[tuple[ChecksumAddress, bytes]]) -> list[int | None]:
        """Each of `calls`' gas from the payee, measured in one batch, with headroom: None for
        one that would fail."""
        answers = await self._chain.batch(
            [
                Call(
                    "eth_estimateGas",
                    [{"from": self._payee.address, "to": to, "data": "0x" + data.hex()}, "latest"],
                )
                for to, data in calls
            ]
        )
        return [
            None if a.error is not None else ceil(int(a.value(), 16) * GAS_HEADROOM)
            for a in answers
        ]

    async def _conversion(
        self,
        token: ChecksumAddress,
        balance: int,
        gas: int,
        shortfall: int,
        fees: Fees,
        fees_now: Fees | None,
    ) -> _Conversion | None:
        """What converts enough of `token` (holding `balance`, its conversion `gas`) to cover
        `shortfall` and its own cost at `fees`; all of it, if it falls short. Given `fees_now`,
        at least enough that its cost at them is within the cost allowed: a conversion worth
        making is made in one. Its gas measured again as it will be sent, and the amount sized
        again if that's more; None if nothing would come of it, or it would fail."""
        if token == self._weth:
            wei, margin, slippage = Fraction(1), Fraction(1), Fraction(1)
        else:
            wei = await self._relay.prices.wei(token)
            margin = self._margin(token)
            slippage = Fraction(self._settings.slippage_multiplier)
        size = self._sizes[token]
        for _ in range(SIZINGS):
            cost = fees.cost(gas, size)
            now = None if fees_now is None else fees_now.cost(gas, size)
            wanted = ceil((shortfall + cost) * margin * slippage / wei)
            amount = min(balance, max(wanted, self._economic(now, margin, wei)))
            min_out = floor(amount * wei / margin / slippage)
            if min_out <= cost:
                return None
            to, data = await self._call(token, amount, min_out)
            (sent_gas,) = await self._measure([(to, data)])
            if sent_gas is None:
                return None
            if sent_gas <= gas:
                return _Conversion(token, amount, to, data, sent_gas, min_out)
            gas, size = sent_gas, self._size(to, data)
        return None

    def _economic(self, cost_now: int | None, margin: Fraction, wei: Fraction) -> int:
        """The least amount of a token (`wei` a unit, `margin` its pools' cut) whose
        conversion, costing `cost_now`, is within the cost allowed; 0 without `cost_now`, or if
        none is."""
        allowed = Fraction(self._settings.max_cost_multiplier) - margin
        if cost_now is None or allowed <= 0:
            return 0
        return ceil(cost_now / allowed / wei)

    def _size(self, to: ChecksumAddress, data: bytes, value: bool = False) -> int:
        """The most bytes a payee's (or the float's) transaction to `to` takes unsigned."""
        return unsigned_size(self._chain.chain_id, to, data, value=int(value))

    async def _cut(self, conversion: _Conversion) -> int:
        """What the pools take of `conversion`, in wei: its amount's worth x (the token's
        margin - 1), the margin being what converting it costs."""
        wei = await self._relay.prices.wei(conversion.token)
        return ceil(conversion.amount * wei * (self._margin(conversion.token) - 1))

    def _margin(self, token: ChecksumAddress) -> Fraction:
        for t in self._config.tokens:
            if to_checksum_address(t.address) == token:
                return Fraction(t.margin_multiplier)
        return Fraction(1)

    async def _route(self, token: ChecksumAddress) -> tuple[Step, ...]:
        """`token`'s path to WETH as swaps: each pool's tokens and fee tier, read once, with
        every other token's not yet read, in one batch."""
        if token not in self._routes:
            unread = [
                to_checksum_address(t.address)
                for t in self._config.tokens
                if t.price and to_checksum_address(t.address) not in self._routes
            ]
            calls: list[Call] = []
            for unread_token in unread:
                for hop in self._hops(unread_token):
                    calls += [_eth_call(hop.pool, _TOKEN0), _eth_call(hop.pool, _TOKEN1)]
                    if hop.kind is PoolKind.UNISWAP_V3:
                        calls.append(_eth_call(hop.pool, _FEE))
            answers = iter(a.value() for a in await self._chain.batch(calls))
            for unread_token in unread:
                self._routes[unread_token] = self._steps(unread_token, answers)
        return self._routes[token]

    def _hops(self, token: ChecksumAddress) -> tuple[Hop, ...]:
        return next(t.price for t in self._config.tokens if to_checksum_address(t.address) == token)

    def _steps(self, token: ChecksumAddress, answers: Iterator[str]) -> tuple[Step, ...]:
        """`token`'s path as swaps, from `answers`: each hop's token0, token1 and (v3) fee."""
        steps: list[Step] = []
        at = token
        for hop in self._hops(token):
            token0 = to_checksum_address("0x" + next(answers)[-40:])
            token1 = to_checksum_address("0x" + next(answers)[-40:])
            fee = int(next(answers), 16) if hop.kind is PoolKind.UNISWAP_V3 else 0
            if at not in (token0, token1):
                raise TreasuryError(f"{token}'s path: pool {hop.pool} doesn't trade {at}")
            out = token1 if at == token0 else token0
            steps.append(Step(hop.kind, at, out, fee))
            at = out
        if at != self._weth:
            raise TreasuryError(f"{token}'s path ends at {at}, not WETH")
        return tuple(steps)

    async def _send(self, block: int, txs: Sequence[SignedTransaction]) -> None:
        """Every builder the bundle of `txs`, for each of BUNDLE_BLOCKS blocks from `block`."""
        sends = [
            (builder, atomic_params(b, txs))
            for builder in self._relay.builders
            for b in range(block, block + BUNDLE_BLOCKS)
        ]
        results = await asyncio.gather(
            *(send_bundle(self._relay.client, b.url, p, b.identity) for b, p in sends),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, BaseException)]
        for (builder, _), result in zip(sends, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("treasury: %s refused a bundle: %s", builder.url, result)
        if len(refused) == len(sends):
            raise ChainError("every builder refused the treasury's bundle")


def _word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def _eth_call(to: str, data: bytes) -> Call:
    return Call("eth_call", [{"to": to, "data": "0x" + data.hex()}, "latest"])


def treasuries(relays: Mapping[int, Relay], payee: LocalAccount) -> dict[int, Treasury]:
    """A treasury for each chain that configures one and whose payee `payee` is."""
    return {
        chain_id: Treasury(relay, payee, relay.config.treasury)
        for chain_id, relay in relays.items()
        if relay.config.treasury is not None and relay.payee == payee.address
    }
