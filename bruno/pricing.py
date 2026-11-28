"""What a call costs on one chain, in each token it's paid in, and whether it fits: the fees a
transaction can pay by a quote's last block, our fee, and the margins; the limits on a call and
its transactions, and on what a sponsorship may cost; and the tokens taken."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from math import ceil, floor

from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress

from .accountreads import CallAccounts, call_accounts
from .chain import Chain, Latest, has_code, market_tip, quantity, risen_base_fee
from .common import BrunoError
from .config import ChainConfig
from .eip3009 import UnsignedTransfer, quoted_calldata, transfer_gas
from .erc6492 import Deployment
from .evm import NATIVE_ASSET, EncodedCall
from .forks import GasSchedule
from .jsonrpc import Call, result_of
from .measurements import Measurements, UnavailableError, schedule_of
from .plan import (
    CostBasis,
    Draft,
    Plan,
    QuotedProof,
    bounds_of,
    call_gas,
    plan_drafts,
    plan_of,
    proof_of,
)
from .prices import Prices
from .refusals import Refusal, Refused, refused
from .runner import Erc20, PlannedCall, Transfer, call_need
from .submission import Bound, InvalidSubmissionError, Submission, SubmittedCall, authorities
from .tracked import Tracked
from .transactions import Fees
from .wallets import verification

# how far the tip may rise over a quote's blocks, as a multiple of today's: a quote allows for
# it, and an account is funded for it
TIP_ALLOWANCE = 2
# the largest amount: a quote's runner pushes it, so its initcode is never longer than the
# one paid for
MAX_AMOUNT = 2**256 - 1
# what a call whose gas a quote estimates is given over the estimate: state can change before
# it's included (a user op's limits likewise, userops._GAS_HEADROOM)
ESTIMATE_HEADROOM = Fraction(11, 10)


def fee_ceiling(latest: Latest, quote_blocks: int) -> Fees:
    """The fees a transaction can pay by a quote's last block, as of `latest`: the next base
    fee risen for `quote_blocks` blocks, and the tip doubled; what's paid beyond gas, risen for
    them too. Accounts are funded to it."""
    tip = market_tip(latest.tips)
    data_fee = None if latest.data_fee is None else latest.data_fee.risen(quote_blocks)
    return Fees(
        risen_base_fee(latest.next_base_fee, quote_blocks) + TIP_ALLOWANCE * tip, tip, data_fee
    )


def cost_within(basis: Sequence[CostBasis], ceiling: Fees, max_gas: int, field: str) -> None:
    """Transactions of `basis` cost no more at `ceiling`, together, than `max_gas` does at its
    max fee: InvalidSubmissionError otherwise, naming `field` (a submission's `calls`, a
    sponsorship's `transactions`), with the most and what they need, in wei."""
    most = max_gas * ceiling.max_fee_per_gas
    needs = sum(each.cost(ceiling) for each in basis)
    if needs > most:
        raise InvalidSubmissionError(
            field, refused(Refusal.COST_OVER, needs=needs, most=most), Bound(most, needs)
        )


def _data_gas(data: bytes, schedule: GasSchedule) -> int:
    """What `data` adds to a transaction's intrinsic gas, carried in it."""
    return schedule.intrinsic(data).standard - schedule.intrinsic(b"").standard


class UnsupportedError(BrunoError):
    """A token or payee the relay doesn't take: why (`refused`)."""

    def __init__(self, refused: Refused) -> None:
        super().__init__(str(refused))
        self.refused = refused


@dataclass(frozen=True, slots=True)
class TokenQuote:
    """A quote in `token`: its `amount`, and for a call's, that amount broken down; and the
    transfer its payer signs, if it pays by one (Relay.quote fills it in)."""

    token: ChecksumAddress
    amount: int
    breakdown: Breakdown | None = None
    transfer: UnsignedTransfer | None = None


@dataclass(frozen=True, slots=True)
class Part:
    """A part of a quote: its gas, and its amount in the quote's token."""

    gas: int
    amount: int


@dataclass(frozen=True, slots=True)
class Breakdown:
    """A quote in parts, for people to read: setting up its account (its deployment and
    authorizations), the calls themselves, the ETH they send (`value`, which takes no gas),
    the payment's transfer and signature check, and the relay's own (its runner and deployer,
    the intrinsic base, our fee, the margins: the rest of the amount). Each part's gas at the
    quote's fees and the token's price, rounded down, so the relay's is never below zero and
    the parts sum to the amount."""

    setup: Part
    call: Part
    value: Part
    payment: Part
    relay: Part


@dataclass(frozen=True, slots=True)
class Quotes:
    """A quote in each token, valid until `valid_until_block`; its calls', the gas each is
    priced for, in order (none for a user op's, its limits its own)."""

    valid_until_block: int
    quotes: tuple[TokenQuote, ...]
    gas: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class QuotedCall:
    """One of the calls a quote prices: the call, or only its data's length (`call` None:
    priced at its worst, every byte nonzero, to the authority or the payee); and its gas, None
    to estimate it."""

    call: EncodedCall | None
    call_data_length: int
    gas: int | None = None


@dataclass(frozen=True, slots=True)
class Quoted:
    """What a quote prices: its `calls`, and their EIP-7702 authorizations, signed, or the
    signer of one not signed yet (`authority`)."""

    calls: tuple[QuotedCall, ...]
    authorizations: tuple[SignedSetCodeAuthorization, ...] = ()
    authority: ChecksumAddress | None = None

    @property
    def length_fields(self) -> tuple[str, ...]:
        """The field each call's length is given by: the call, or its length alone."""
        return tuple(
            f"calls[{index}]" if each.call is not None else f"calls[{index}].callDataLength"
            for index, each in enumerate(self.calls)
        )

    @property
    def authorities(self) -> tuple[ChecksumAddress, ...]:
        """Each authorization's signer: InvalidSubmissionError if one can't be recovered."""
        if self.authority is not None:
            return (self.authority,)
        return authorities(self.authorizations)


@dataclass(frozen=True, slots=True)
class Priced:
    """Quoted calls as they're priced: each as it's planned (a stand-in of its length, if only
    that was given; its gas given, estimated, or the relay's own bound of it), and what's read
    of their accounts."""

    calls: tuple[PlannedCall, ...]
    accounts: CallAccounts


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
    deploys: bool = False


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
        self,
        measured: Measurements,
        payer: Payer,
        authorities: Collection[ChecksumAddress] = (),
        *,
        transferring: bool,
    ) -> _Signing:
        """How `payer`'s transfer is priced, if `transferring`. Its signer is `payer.address`,
        else the account its deployment deploys. A contract (one with `verify_gas` given, one
        deployed, one with code, or one of the request's `authorities`, which its
        authorizations give code) has its check priced at `verify_gas`, else at the wallet gas
        schedule's figure for its deployment's factory, else at the schedule's largest. A
        deployment is priced as the runner checks it, and its factory call as estimated: for an
        account with no code, made (InvalidSubmissionError if it reverts); for one with code,
        made only to prepare it (ERC-6492), which a transfer's wrapped signature may ask, so
        priced if it can be. InvalidSubmissionError too for `verify_gas` with no transfer to
        check, or over the most gas a call may have. One read batch, if any."""
        if payer.verify_gas is not None and not transferring:
            raise InvalidSubmissionError("verifyGas", refused(Refusal.VERIFY_GAS_NO_PAYER))
        if payer.verify_gas is not None and payer.verify_gas > self._config.max_gas:
            most = self._config.max_gas
            raise InvalidSubmissionError(
                "verifyGas", refused(Refusal.GAS_OVER, most=most), Bound(most)
            )
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
        deploys = False
        if deployment is not None:
            deploys = not has_code(result_of(answers[0]))
            estimate = answers[1]
            if estimate.error is not None:
                if deploys:
                    raise InvalidSubmissionError(
                        "deployment",
                        refused(Refusal.FACTORY_FAILS, why=estimate.error.node_message),
                    )
            elif deploys or transferring:
                intrinsic = schedule.intrinsic(deployment.factory_data).standard
                deploy_gas = quantity(result_of(estimate)) - intrinsic
        signer_has_code = read_signer and has_code(result_of(answers[-1]))
        contract = transferring and (
            payer.verify_gas is not None
            or (deployment is not None and deployment.account == signer)
            or signer_has_code
            or signer in authorities
        )
        if not contract:
            return _Signing(
                transferring, deployment=deployment, deploy_gas=deploy_gas, deploys=deploys
            )
        known = verification(deployment, schedule)
        return _Signing(
            transferring,
            verify_gas=known.gas if payer.verify_gas is None else payer.verify_gas,
            signature_bytes=known.signature_bytes,
            deployment=deployment,
            deploy_gas=deploy_gas,
            deploys=deploys,
        )

    async def priced(
        self,
        measured: Measurements,
        quoted: Quoted,
        tokens: Sequence[ChecksumAddress],
        signing: _Signing,
        sender: ChecksumAddress,
        *,
        existing: bool = False,
    ) -> Priced:
        """`quoted` as it's priced: each call (its length's stand-in, if only that's given) with
        its gas, and what's read of their accounts in one batch. A call the relay bounds is
        given its bound, whatever gas it states, and isn't estimated. One that states no gas is
        estimated in another batch (call_gas), sent by `sender`, the others at their gas:
        InvalidSubmissionError if a second one states none. `existing`: its authorities are
        taken to exist and each call to have its gas, nothing read."""
        schedule = schedule_of(measured)
        authorities = quoted.authorities
        target = authorities[0] if authorities else self._payee
        calls = [
            each.call
            if each.call is not None
            else EncodedCall(target, b"\xff" * each.call_data_length)
            for each in quoted.calls
        ]
        bounds: list[int | None] = [None] * len(calls)
        if existing:
            accounts = CallAccounts(frozenset(authorities), 0, frozenset(authorities))
        else:
            accounts = await call_accounts(self._chain, [call.to for call in calls], authorities)
            bounds = await bounds_of(self._chain, calls, measured.tokens, schedule, authorities)
        planned: list[PlannedCall] = []
        unstated: list[int] = []
        for index, (each, call, bound) in enumerate(zip(quoted.calls, calls, bounds, strict=True)):
            if bound is not None:
                planned.append(PlannedCall(call, call_need(bound), bounded=True))
            elif each.gas is not None:
                planned.append(PlannedCall(call, each.gas))
            else:
                planned.append(PlannedCall(call, 0))
                unstated.append(index)
        if len(unstated) > 1:
            raise InvalidSubmissionError(f"calls[{unstated[1]}].gas", refused(Refusal.CALL_NO_GAS))
        for index in unstated:
            # as paid in the first token (ETH if none): in another, the call finds that token's
            # balance cold, its estimate the higher
            token = tokens[0] if tokens else NATIVE_ASSET
            erc20 = measured.tokens[tokens[0]] if tokens else None
            draft = self._draft(measured, quoted, tuple(planned), accounts, token, erc20, signing)
            # its deployment estimated only if it's made: its account has no code
            deployment = signing.deployment if signing.deploys else None
            estimating = replace(
                draft,
                deployment=deployment,
                deploy_gas=None if deployment is None else signing.deploy_gas,
            )
            gas = await self._estimated_gas(quoted, index, estimating, accounts, schedule, sender)
            planned[index] = PlannedCall(calls[index], gas)
        return Priced(tuple(planned), accounts)

    async def quote_at(
        self,
        measured: Measurements,
        latest: Latest,
        quoted: Quoted,
        tokens: Sequence[ChecksumAddress],
        signing: _Signing,
        sender: ChecksumAddress,
        *,
        existing: bool = False,
    ) -> Quotes:
        """`quoted` in each of `tokens`, as it's priced (priced), sent by `sender`: in each, the
        transaction it would be sent as, drafted as a submission paying the most it can, and
        planned as a submission is (plan_drafts)."""
        schedule = schedule_of(measured)
        priced = await self.priced(measured, quoted, tokens, signing, sender, existing=existing)
        calls, accounts = priced.calls, priced.accounts
        authorities = quoted.authorities
        self.within_limits(
            [planned.gas for planned in calls],
            [each.call_data_length for each in quoted.calls],
            quoted.length_fields,
        )
        # the base fee risen and the tip doubled, as they can be by the quote's last block
        fees = fee_ceiling(latest, self._config.quote_blocks - 1)
        drafts = [
            self._draft(measured, quoted, calls, accounts, t, measured.tokens[t], signing)
            for t in tokens
        ]
        planned = await plan_drafts(self._chain, drafts, schedule, sender)
        quotes: list[TokenQuote] = []
        for token, draft, each in zip(tokens, drafts, planned, strict=True):
            plan = plan_of(each)
            self.fits(plan)
            self.within_cost(plan, latest, self._config.quote_blocks)
            amount = await self.charge(measured, latest, token, plan, fees)
            setup_gas = (
                signing.deploy_gas
                + (
                    0
                    if signing.deployment is None
                    else _data_gas(signing.deployment.factory_data, schedule)
                )
                + schedule.authorization * len(authorities)
                + schedule.authorizations_gas(accounts.empty, len(authorities))
            )
            call_gas = sum(
                draft.proof.call_part(planned.gas) + _data_gas(planned.call.data, schedule)
                for planned in calls
            )
            transfer = draft.transfer
            payment_gas = (
                0
                if transfer is None
                else (transfer.gas or 0) + _data_gas(transfer.calldata, schedule)
            )
            sent = sum(planned.call.value for planned in calls)
            breakdown = await self._breakdown(
                token, amount, plan.gas_limit, fees, setup_gas, call_gas, payment_gas, sent
            )
            quotes.append(TokenQuote(token, amount, breakdown))
        return Quotes(
            valid_until_block=latest.number + self._config.quote_blocks,
            quotes=tuple(quotes),
            gas=tuple(planned.gas for planned in calls),
        )

    def _draft(
        self,
        measured: Measurements,
        quoted: Quoted,
        planned: tuple[PlannedCall, ...],
        accounts: CallAccounts,
        token: ChecksumAddress,
        erc20: Erc20 | None,
        signing: _Signing,
    ) -> Draft[QuotedProof]:
        """The transaction `quoted` would be sent as, paid in `token` (`erc20` as measured, None
        for ETH), drafted as a submission paying the most it can (MAX_AMOUNT), its calls as
        `planned`: its transfer's calldata a stand-in of its length, its gas bounded, its signature's
        check at `signing`'s figure; its deployment's factory call at the gas `signing` found;
        its proof chosen as a submission's is (proof_of)."""
        transfer = None
        if signing.transferring and erc20 is not None and erc20.transfer_gas is not None:
            schedule = schedule_of(measured)
            bound = transfer_gas(erc20.transfer_gas, schedule, token in measured.payee_holds)
            transfer = Transfer(
                quoted_calldata(signing.signature_bytes), bound + signing.verify_gas
            )
        deployment = signing.deployment
        submission = Submission(
            calls=tuple(SubmittedCall(each.call, each.gas) for each in planned),
            authorizations=quoted.authorizations,
            token=token,
            payee=self._payee,
            amount=MAX_AMOUNT,
            max_block_number=0,
            deployment=deployment,
        )
        proof = proof_of(
            self._chain.chain_id,
            [each.call for each in planned],
            transfer=None if transfer is None else EncodedCall(token, transfer.calldata),
            deployment=deployment,
        )
        return Draft(
            submission,
            proof,
            quoted.authorities,
            accounts,
            erc20,
            planned,
            transfer,
            deployment,
            deploy_gas=signing.deploy_gas,
        )

    async def _breakdown(
        self,
        token: ChecksumAddress,
        amount: int,
        gas_limit: int,
        fees: Fees,
        setup_gas: int,
        call_gas: int,
        payment_gas: int,
        sent: int,
    ) -> Breakdown:
        """`amount` in parts (Breakdown): each part's gas at `fees`' max fee and the token's
        price, rounded down; the wei the calls send (`sent`) at that price, no gas of its own;
        the relay's, the rest of the gas limit and of the amount."""
        wei = await self._prices.wei(token)

        def part(part_gas: int) -> Part:
            return Part(part_gas, floor(Fraction(part_gas * fees.max_fee_per_gas) / wei))

        setup, call, payment = part(setup_gas), part(call_gas), part(payment_gas)
        value = Part(0, floor(Fraction(sent) / wei))
        relay = Part(
            gas_limit - setup.gas - call.gas - payment.gas,
            amount - setup.amount - call.amount - value.amount - payment.amount,
        )
        return Breakdown(setup, call, value, payment, relay)

    async def _estimated_gas(
        self,
        quoted: Quoted,
        index: int,
        draft: Draft[QuotedProof],
        accounts: CallAccounts,
        schedule: GasSchedule,
        sender: ChecksumAddress,
    ) -> int:
        """The gas `quoted`'s `index`th call is given, unstated: its estimate (call_gas) as
        `draft` sends it, with ESTIMATE_HEADROOM. Only for a call given whole, and for an authority's account
        already delegated, an unsigned authorization's delegate being unknown
        (InvalidSubmissionError, `gas`)."""
        field = f"calls[{index}].gas"
        if quoted.calls[index].call is None:
            raise InvalidSubmissionError(field, refused(Refusal.QUOTE_NO_GAS))
        authority = quoted.authority
        if authority is not None and authority not in accounts.delegating:
            raise InvalidSubmissionError(
                field,
                refused(Refusal.AUTHORITY_UNDELEGATED, authority=authority),
            )
        estimate = await call_gas(self._chain, draft, index, schedule, sender)
        return ceil(estimate * ESTIMATE_HEADROOM)

    def fits(self, plan: Plan) -> None:
        """The plan's transactions, together, within the most gas a transaction may use,
        everything in them: the runner, a deployment, a transfer and its check, and the call."""
        most = self._config.max_gas
        if plan.gas_limit > most:
            raise InvalidSubmissionError(
                "calls",
                refused(Refusal.TRANSACTION_OVER, needs=plan.gas_limit, most=most),
                Bound(most, plan.gas_limit),
            )

    def within_cost(self, plan: Plan, latest: Latest, blocks: int, field: str = "calls") -> None:
        """What the client chooses of a plan's cost is bounded when it's quoted and taken, its
        deadline `blocks` on, a submission's as a sponsorship's, one cap and one refusal naming
        `field`: for a submission, its transactions' gas and the ETH its calls send. Of a
        sponsorship: the relay's own transactions' gas at the fee ceiling then, and
        the ETH they send (what the account is sent: its transactions' gas at the max fees it
        signed, and their value), within `max_gas` at that ceiling. A value, or a max fee over
        what the deadline needs, takes the room gas would. What the account is sent is fixed
        from then on. The relay's own gas isn't: it's signed each block at that block's fees,
        as a call's is, and only where the payment covers its charge there, so a tip risen past
        this ceiling may take a sponsorship's cost over `most`, never over what a call of
        `max_gas` costs in the same block."""
        ceiling = fee_ceiling(latest, blocks)
        cost_within(plan.basis, ceiling, self._config.max_gas, field)

    def within_limits(
        self, gas: Sequence[int], call_bytes: Sequence[int], length_fields: Sequence[str] = ()
    ) -> None:
        """The calls' own gas, and their lengths, within the limits, each call's and all of
        them together: calls given more than a whole transaction may use can never fit in one.
        A refusal names the call over a limit on its own, or `calls` where they're over it
        together. `length_fields`: what gave each call's length, the call itself if not said."""
        most_gas, most_bytes = self._config.max_gas, self._config.max_call_bytes
        over = next((f"calls[{i}].gas" for i, each in enumerate(gas) if each > most_gas), None)
        if over is not None or sum(gas) > most_gas:
            raise InvalidSubmissionError(
                over or "calls", refused(Refusal.GAS_OVER, most=most_gas), Bound(most_gas)
            )
        fields = length_fields or [f"calls[{i}]" for i in range(len(call_bytes))]
        long = next(
            (field for field, each in zip(fields, call_bytes, strict=True) if each > most_bytes),
            None,
        )
        if long is not None or sum(call_bytes) > most_bytes:
            raise InvalidSubmissionError(
                long or "calls", refused(Refusal.CALL_TOO_LONG, most=most_bytes), Bound(most_bytes)
            )

    def supported(self, measured: Measurements, token: ChecksumAddress) -> None:
        if token not in self._margins:
            raise UnsupportedError(refused(Refusal.NOT_ACCEPTED, token=token))
        if token in measured.suspended:
            why = measured.suspended[token]
            raise UnsupportedError(refused(Refusal.SUSPENDED, token=token, why=why))

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
            raise UnavailableError(refused(Refusal.MIN_FEE_SUSPENDED, token=token))
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
        cost = plan.cost(self.fees(latest) if fees is None else fees)
        fee = await self._our_fee(measured, latest, plan.gas_limit)
        return await self.price(token, cost + fee)

    async def price(self, token: ChecksumAddress, wei: int) -> int:
        """`wei` in `token`'s base units, margins included, rounded up."""
        return ceil(Fraction(wei) / await self._prices.wei(token) * self._margins[token])

    async def profit(self, tracked: Tracked, fees: Fees, base_fee: int) -> Fraction:
        """What the submission pays in a block of `base_fee`, in wei, less what the relay's own
        transactions cost at `fees`."""
        wei = await self._prices.wei(tracked.submission.token)
        paying = tracked.paying(base_fee)
        return paying * wei - tracked.cost(fees)
