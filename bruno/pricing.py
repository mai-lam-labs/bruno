"""What a call costs on one chain, in each token it's paid in, and whether it fits: the fees a
transaction can pay by a quote's last block, our fee, and the margins; the limits on a call and
its transaction; and the tokens taken."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from math import ceil

from eth_typing import ChecksumAddress, HexStr

from .chain import Chain, Latest, has_code, market_tip, quantity, risen_base_fee
from .common import BrunoError
from .config import ChainConfig
from .eip3009 import quoted_calldata, transfer_gas
from .erc6492 import Deployment
from .evm import EncodedCall
from .jsonrpc import Call
from .measurements import Category, Measurements, Unavailability, UnavailableError, schedule_of
from .plan import Plan, empty, runner_plan
from .prices import Prices
from .runner import Payment, Transfer, in_call_runner
from .submission import Bound, InvalidSubmissionError
from .tracked import Tracked
from .transactions import Fees
from .wallets import verification

# how far the tip may rise over a quote's blocks, as a multiple of today's: a quote allows for
# it, and an account is funded for it
TIP_ALLOWANCE = 2
# the largest amount: a quote's runner pushes it, so its initcode is never longer than the
# one paid for
MAX_AMOUNT = 2**256 - 1


def fee_ceiling(latest: Latest, quote_blocks: int) -> Fees:
    """The fees a transaction can pay by a quote's last block, as of `latest`: the next base
    fee risen for `quote_blocks` blocks, and the tip doubled; what's paid beyond gas, risen for
    them too. Accounts are funded to it."""
    tip = market_tip(latest.tips)
    data_fee = None if latest.data_fee is None else latest.data_fee.risen(quote_blocks)
    return Fees(
        risen_base_fee(latest.next_base_fee, quote_blocks) + TIP_ALLOWANCE * tip, tip, data_fee
    )


class Unsupported(StrEnum):
    """Why a token or payee isn't taken (Unsupported): its category the reason's."""

    NOT_ACCEPTED = "not-accepted"
    NO_EIP3009 = "no-eip3009"
    WRONG_PAYEE = "wrong-payee"
    ETH_NOT_TAKEN = "eth-not-taken"
    SUSPENDED = "suspended"

    @property
    def category(self) -> Category:
        """A suspended token is measured again, and may be taken then: retried. The rest are
        asked for otherwise."""
        return Category.RETRY if self is Unsupported.SUSPENDED else Category.REQUEST


class UnsupportedError(BrunoError):
    """A token or payee the relay doesn't take: why (`reason`), and its details for people."""

    def __init__(self, reason: Unsupported, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TokenQuote:
    token: ChecksumAddress
    amount: int


@dataclass(frozen=True, slots=True)
class Quotes:
    valid_until_block: int
    quotes: tuple[TokenQuote, ...]


@dataclass(frozen=True, slots=True)
class Payer:
    """What a quote is told of its payment's signer, each if known: its `address` (the payment
    then a transfer, EIP-3009), an account to deploy first (`deployment`), and the gas its
    signature check takes (`verify_gas`)."""

    address: ChecksumAddress | None = None
    deployment: Deployment | None = None
    verify_gas: int | None = None


# a quote told nothing of its payment's signer: the call pays, or (over x402) a key's transfer
NO_PAYER = Payer()


@dataclass(frozen=True, slots=True)
class _Signing:
    """How a quote prices its payment's transfer: whether there is one, and if its signer is a
    contract, its check's gas (`verify_gas`) and its signature's length (`signature_bytes`); and
    its deployment, if it declares one, the runner checking it, with the gas its factory call
    takes if it may be made."""

    transferring: bool
    verify_gas: int = 0
    signature_bytes: int | None = None
    deployment: Deployment | None = None
    deploy_gas: int = 0


class Pricing:
    def __init__(
        self, chain: Chain, config: ChainConfig, prices: Prices, payee: ChecksumAddress
    ) -> None:
        self._chain = chain
        self._config = config
        self._prices = prices
        self._payee = payee
        self._margins = {
            t.address: Fraction(config.margin_multiplier) * Fraction(t.margin_multiplier)
            for t in config.tokens
        }

    async def signing(
        self, measured: Measurements, payer: Payer, *, transferring: bool
    ) -> _Signing:
        """How `payer`'s transfer is priced, if `transferring`. Its signer is `payer.address`,
        else the account its deployment deploys. A contract (one with `verify_gas` given, one
        deployed, or one with code) has its check priced at `verify_gas`, else at the wallet gas
        schedule's figure for its deployment's factory, else at the schedule's largest. A
        deployment is priced as the runner checks it, and its factory call as estimated: for an
        account with no code, made (InvalidSubmissionError if it reverts); for one with code,
        made only to prepare it (ERC-6492), which a transfer's wrapped signature may ask, so
        priced if it can be. InvalidSubmissionError too for `verify_gas` with no transfer to
        check, or over the most gas a call may have. One read batch, if any."""
        if payer.verify_gas is not None and not transferring:
            raise InvalidSubmissionError("verifyGas", "only with a payer")
        if payer.verify_gas is not None and payer.verify_gas > self._config.max_gas:
            most = self._config.max_gas
            raise InvalidSubmissionError("verifyGas", f"over {most}", Bound(most))
        deployment = payer.deployment
        signer = payer.address
        if signer is None and transferring and deployment is not None:
            signer = deployment.account
        calls: list[Call] = []
        if deployment is not None:
            calls += [
                Call("eth_getCode", [deployment.account, "latest"]),
                Call(
                    "eth_estimateGas",
                    [{"to": deployment.factory, "data": "0x" + deployment.factory_data.hex()}],
                ),
            ]
        # the signer's own code, unless it's the account deployed
        read_signer = signer is not None and (deployment is None or deployment.account != signer)
        if read_signer:
            calls.append(Call("eth_getCode", [signer, "latest"]))
        answers = await self._chain.batch(calls) if calls else []
        schedule = schedule_of(measured)
        deploy_gas = 0
        if deployment is not None:
            deploys = not has_code(answers[0].value())
            estimate = answers[1]
            if estimate.error is not None:
                if deploys:
                    raise InvalidSubmissionError(
                        "deployment", f"its factory call fails: {estimate.error.message}"
                    )
            elif deploys or transferring:
                intrinsic = schedule.intrinsic(deployment.factory_data).standard
                deploy_gas = quantity(estimate.value()) - intrinsic
        signer_has_code = read_signer and has_code(answers[-1].value())
        contract = transferring and (
            payer.verify_gas is not None
            or (deployment is not None and deployment.account == signer)
            or signer_has_code
        )
        if not contract:
            return _Signing(transferring, deployment=deployment, deploy_gas=deploy_gas)
        known = verification(deployment, schedule)
        return _Signing(
            transferring,
            verify_gas=known.gas if payer.verify_gas is None else payer.verify_gas,
            signature_bytes=known.signature_bytes,
            deployment=deployment,
            deploy_gas=deploy_gas,
        )

    async def quote_at(
        self,
        measured: Measurements,
        latest: Latest,
        gas: int,
        call_bytes: int,
        authority: ChecksumAddress | None,
        tokens: Sequence[ChecksumAddress],
        signing: _Signing,
        *,
        existing: bool = False,
    ) -> Quotes:
        schedule = schedule_of(measured)
        target = authority if authority is not None else self._payee
        call = EncodedCall(target, HexStr("0x" + "ff" * call_bytes))
        authorities = () if authority is None else (authority,)
        empties = 0 if existing else await empty(self._chain, authorities)
        # the base fee risen and the tip doubled, as they can be by the quote's last block
        fees = fee_ceiling(latest, self._config.quote_blocks - 1)
        quotes: list[TokenQuote] = []
        for token in tokens:
            erc20 = measured.tokens[token]
            transfer = None
            if signing.transferring and erc20 is not None and erc20.transfer_gas is not None:
                bound = transfer_gas(erc20.transfer_gas, schedule, token in measured.payee_holds)
                transfer = Transfer(
                    quoted_calldata(signing.signature_bytes), bound + signing.verify_gas
                )
            payment = Payment(erc20, self._payee, MAX_AMOUNT, transfer)
            runner = in_call_runner(
                call,
                payment,
                schedule,
                delegated=set(authorities),
                client_gas=gas,
                deployment=signing.deployment,
            )
            plan = runner_plan(
                self._chain.chain_id,
                runner,
                gas,
                schedule,
                len(authorities),
                empties,
                extra_gas=signing.deploy_gas,
            )
            self.fits(plan)
            quotes.append(TokenQuote(token, await self.charge(measured, latest, token, plan, fees)))
        return Quotes(
            valid_until_block=latest.number + self._config.quote_blocks, quotes=tuple(quotes)
        )

    def fits(self, plan: Plan) -> None:
        """The plan's transaction within the most gas a transaction may use, everything in it:
        the runner, a deployment, a transfer and its check, and the call."""
        most = self._config.max_gas
        if plan.gas_limit > most:
            raise InvalidSubmissionError(
                "gas",
                f"the transaction needs {plan.gas_limit}, over {most}",
                Bound(most, plan.gas_limit),
            )

    def within_limits(self, gas: int, call_bytes: int) -> None:
        """The call's own gas, and its length, within the limits: a call given more than a whole
        transaction may use can never fit in one."""
        most_gas, most_bytes = self._config.max_gas, self._config.max_call_bytes
        if gas > most_gas:
            raise InvalidSubmissionError("gas", f"over {most_gas}", Bound(most_gas))
        if call_bytes > most_bytes:
            raise InvalidSubmissionError("call", f"over {most_bytes} bytes", Bound(most_bytes))

    def supported(self, measured: Measurements, token: ChecksumAddress) -> None:
        if token not in self._margins:
            raise UnsupportedError(Unsupported.NOT_ACCEPTED, f"{token} isn't accepted")
        if token in measured.suspended:
            why = f"{token} is suspended: {measured.suspended[token]}"
            raise UnsupportedError(Unsupported.SUSPENDED, why)

    def tip(self, latest: Latest) -> int:
        """The tip each transaction offers: the market's (market_tip)."""
        return market_tip(latest.tips)

    def fees(self, latest: Latest) -> Fees:
        """The fees of the block after `latest`: exactly its base fee, and the tip; what's paid
        beyond gas risen for the one block (its L1 inputs may be a new L1 block's)."""
        tip = self.tip(latest)
        data_fee = None if latest.data_fee is None else latest.data_fee.risen(1)
        return Fees(latest.next_base_fee + tip, tip, data_fee)

    async def _our_fee(self, measured: Measurements, latest: Latest, gas_limit: int) -> int:
        """Our fee on a transaction of `gas_limit`, in wei: the tip a gas, at least
        `min_fee_per_gas`; and at least `min_fee`, by its token's measured decimals."""
        fee = max(self.tip(latest), self._config.min_fee_per_gas) * gas_limit
        min_fee = self._config.min_fee
        if min_fee is None:
            return fee
        token = min_fee.token
        decimals = measured.decimals.get(token)
        if decimals is None:
            why = f"min_fee's token {token} is suspended"
            raise UnavailableError(Unavailability.MIN_FEE_SUSPENDED, why)
        units = ceil(Fraction(min_fee.amount) * 10**decimals)
        return max(fee, ceil(units * await self._prices.wei(token)))

    async def charge(
        self,
        measured: Measurements,
        latest: Latest,
        token: ChecksumAddress,
        plan: Plan,
        fees: Fees | None = None,
    ) -> int:
        """What `plan`'s transaction is charged in `token`: its cost at `fees` (the fees of the
        block after `latest`, if not given), and our fee, margins included."""
        cost = (self.fees(latest) if fees is None else fees).cost(plan.gas_limit, plan.size)
        fee = await self._our_fee(measured, latest, plan.gas_limit)
        return await self.price(token, cost + fee)

    async def price(self, token: ChecksumAddress, wei: int) -> int:
        """`wei` in `token`'s base units, margins included, rounded up."""
        return ceil(Fraction(wei) / await self._prices.wei(token) * self._margins[token])

    async def profit(self, tracked: Tracked, fees: Fees, base_fee: int) -> Fraction:
        """What the submission pays in a block of `base_fee`, in wei, less its transaction's
        cost at `fees`."""
        wei = await self._prices.wei(tracked.submission.token)
        paying = tracked.paying(base_fee)
        return paying * wei - fees.cost(tracked.gas_limit, tracked.size)
