"""A sponsorship's quote: what an account is to sign, filled in (each transaction's nonce, fees
and gas), the payment the relay offers in each token, and what it's charged
(docs/development/plans/sponsorship.md). Its calls are priced as a submission's are, and it's
planned by the one planner (plan.py). One simulation a token, of the whole sequence with that
token's payment in it, fills in its transactions' gas; priced as a sponsorship of what's quoted
would be."""

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil

from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress

from .abi import address_word, word
from .accountreads import account_state, call_accounts
from .chain import Chain, Latest, market_tip, risen_base_fee
from .config import ChainConfig
from .evm import NATIVE_ASSET, EncodedCall
from .forks import NONZERO_BYTE_GAS, ZERO_BYTE_GAS
from .forwarder import TRANSFER
from .measurements import Measurements, schedule_of
from .plan import (
    CheckedByForwarder,
    Draft,
    Step,
    plan_drafts,
    plan_of,
    signed_json,
    sponsored,
    transaction_json,
)
from .pricing import ESTIMATE_HEADROOM, NO_PAYER, Pricing, Quoted, QuotedCall
from .simulation import gas_used
from .sponsorship import Sponsorship, owned
from .submission import SubmittedCall, authorities
from .transactions import Decoded, SignedTransaction

# an amount's word in a payment's calldata, at its dearest over a unit's: every byte nonzero.
# A payment's gas is estimated paying a unit, the amount not known until it's priced
_AMOUNT_WORD_GAS = 32 * (NONZERO_BYTE_GAS - ZERO_BYTE_GAS)
# the largest amount: a quote's stand-in pays it, its charge the same whatever's paid
_MOST = 2**256 - 1


@dataclass(frozen=True, slots=True)
class Unsigned:
    """A transaction the sponsored account is to sign, as a quote is asked about it: its gas
    estimated if it's None; `to` None for a contract creation."""

    to: ChecksumAddress | None
    data: bytes
    value: int
    gas: int | None


@dataclass(frozen=True, slots=True)
class Asked:
    """What a sponsorship's quote is asked: the `account` sponsored; its authorizations,
    signed; the calls the relay makes first, each with its gas, one's estimated if None; its
    own transactions, each signed already or to be; and its deadline, the furthest one if
    None. What's given is the account's (sponsorship.owned): InvalidSubmissionError
    otherwise."""

    account: ChecksumAddress
    authorizations: tuple[SignedSetCodeAuthorization, ...]
    calls: tuple[SubmittedCall, ...]
    transactions: tuple[Decoded | Unsigned, ...]
    max_block_number: int | None

    def __post_init__(self) -> None:
        owned(
            self.account,
            len(self.calls),
            self.authorizations,
            [None if isinstance(t, Unsigned) else t.signed.sender for t in self.transactions],
        )


@dataclass(frozen=True, slots=True)
class ToSign:
    """A transaction filled in for its account to sign, EIP-1559's: `to` None for a contract
    creation."""

    chain_id: int
    nonce: int
    to: ChecksumAddress | None
    data: bytes
    value: int
    gas: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


@dataclass(frozen=True, slots=True)
class SponsoredTokenQuote:
    """What a sponsorship is charged in `token`, and the payment of it the relay offers, for
    the account to sign after its own: None with no transaction of the account's, when it's
    its code, run by the relay's call, that pays."""

    token: ChecksumAddress
    amount: int
    payment: ToSign | None


@dataclass(frozen=True, slots=True)
class SponsoredQuotes:
    """A sponsorship's quote, valid until `valid_until_block`: its deadline, the forwarder it
    pays, the gas each of its calls is priced for, in order, each of its transactions to sign
    (None for one signed already), and its quote in each token."""

    valid_until_block: int
    max_block_number: int
    forwarder: ChecksumAddress
    gas: tuple[int, ...]
    transactions: tuple[ToSign | None, ...]
    quotes: tuple[SponsoredTokenQuote, ...]


def _drafted(to_sign: ToSign, sender: ChecksumAddress) -> Decoded:
    """`to_sign` as a quote's stand-in for the transaction it will be once signed: priced and
    planned by, never sent."""
    signed = SignedTransaction(
        raw=b"",
        hash=b"",
        sender=sender,
        nonce=to_sign.nonce,
        gas=to_sign.gas,
        max_fee_per_gas=to_sign.max_fee_per_gas,
        value=to_sign.value,
    )
    return Decoded(
        signed,
        to_sign.chain_id,
        to_sign.max_priority_fee_per_gas,
        to_sign.to,
        to_sign.data,
        (),
        (),
    )


def _call_json(sender: ChecksumAddress, to_sign: ToSign, gas: int) -> dict[str, object]:
    """`to_sign` as eth_simulateV1 takes a call, given `gas`: no nonce or fees, nothing
    validated."""
    call: dict[str, object] = {
        "from": sender,
        "data": "0x" + to_sign.data.hex(),
        "value": hex(to_sign.value),
        "gas": hex(gas),
    }
    if to_sign.to is not None:
        call["to"] = to_sign.to
    return call


def payment_call(forwarder: ChecksumAddress, token: ChecksumAddress, amount: int) -> EncodedCall:
    """The payment a quote offers: `amount` of `token` transferred to the forwarder, or of ETH
    sent it."""
    if token == NATIVE_ASSET:
        return EncodedCall(forwarder, b"", amount)
    return EncodedCall(token, TRANSFER + address_word(forwarder) + word(amount))


async def sponsored_quotes(
    chain: Chain,
    config: ChainConfig,
    pricing: Pricing,
    measured: Measurements,
    latest: Latest,
    asked: Asked,
    tokens: Sequence[ChecksumAddress],
    sender: ChecksumAddress,
    forwarder: ChecksumAddress,
) -> SponsoredQuotes:
    """`asked`'s quote in each of `tokens`. Its calls are priced as a submission's are
    (Pricing.priced): each one's accounts and bound read, one's gas estimated if not given.
    Each transaction not signed yet is given the account's next nonce (after its
    authorizations, which the relay's first transaction spends), the max fee a block may ask
    by the deadline, and the market's tip. Then, a token at a time: with transactions of the
    account's, the payment in that token is added after them, paying a unit (with none, the
    relay's calls pay, and no payment is offered); the sequence is simulated once, the relay's
    own transactions as it's planned and then the account's, for the gas each of the
    account's uses, a transaction's limit that and ESTIMATE_HEADROOM where none was given (the
    payment's with what its amount's word may add); and the whole is planned and charged as a
    sponsorship of it would be. A transaction that fails is the quote's simulation failing."""
    account = asked.account
    schedule = schedule_of(measured)
    deadline = asked.max_block_number or latest.number + config.max_deadline_blocks
    max_fee = risen_base_fee(latest.next_base_fee, deadline - latest.number)
    tip = min(market_tip(latest.tips), max_fee)
    state = await account_state(chain, account)
    nonce = state.nonce + len(asked.authorizations)
    # each of its transactions as it is, or as it will be signed, its gas the cap until estimated
    own: list[Decoded | ToSign] = []
    for transaction in asked.transactions:
        if isinstance(transaction, Unsigned):
            gas = config.max_gas if transaction.gas is None else transaction.gas
            own.append(
                ToSign(
                    chain.chain_id,
                    nonce,
                    transaction.to,
                    transaction.data,
                    transaction.value,
                    gas,
                    max_fee,
                    tip,
                )
            )
            nonce += 1
        else:
            own.append(transaction)
            nonce = transaction.signed.nonce + 1
    estimated = [isinstance(t, Unsigned) and t.gas is None for t in asked.transactions]
    sending = bool(asked.transactions)
    # its calls as a submission's are priced, and their accounts with the account's own
    quoted = Quoted(
        tuple(QuotedCall(each.call, len(each.call.data), each.gas) for each in asked.calls),
        asked.authorizations,
    )
    unpaid = await pricing.signing(measured, NO_PAYER, transferring=False)
    priced = await pricing.priced(measured, quoted, tokens, unpaid, sender)
    signers = authorities(asked.authorizations)
    targets = [*(planned.call.to for planned in priced.calls), account]
    accounts = await call_accounts(chain, targets, signers)
    calls = tuple(SubmittedCall(planned.call, planned.gas) for planned in priced.calls)
    proof = CheckedByForwarder()
    filled: list[ToSign | None] = []
    quotes: list[SponsoredTokenQuote] = []
    for token in tokens:

        def paying(amount: int, gas: int, token: ChecksumAddress = token) -> ToSign:
            """The payment of `amount` in `token`, to sign, given `gas`."""
            call = payment_call(forwarder, token, amount)
            return ToSign(chain.chain_id, nonce, call.to, call.data, call.value, gas, max_fee, tip)

        def drafting(
            transactions: Sequence[Decoded | ToSign], token: ChecksumAddress = token
        ) -> Draft[CheckedByForwarder]:
            """The sponsorship of `transactions`, paying the most in `token`, as it's
            drafted."""
            stand_in = Sponsorship(
                account=account,
                authorizations=asked.authorizations,
                calls=calls,
                transactions=tuple(
                    t if isinstance(t, Decoded) else _drafted(t, account) for t in transactions
                ),
                token=token,
                payee=forwarder,
                amount=_MOST,
                max_block_number=deadline,
            )
            return sponsored(
                stand_in,
                proof,
                signers,
                accounts,
                measured.tokens[token],
                schedule,
                priced.calls,
                account_new=state.empty,
                account_has_code=state.has_code,
                payee_holds=token in measured.payee_holds,
                payee_new=measured.payee_new,
            )

        # the sequence as it will be sent, each transaction given all the gas it may have: the
        # relay's own first, then the account's, then the payment offered, paying a unit
        offered = [*own, paying(1, config.max_gas)] if sending else own
        provisional = proof.plan(chain.chain_id, drafting(offered), schedule)
        first = [step for step in provisional.steps if isinstance(step, Step)][:-1]
        sequence = [
            *({**transaction_json(step, sender), "gas": hex(config.max_gas)} for step in first),
            *(
                signed_json(t) if isinstance(t, Decoded) else _call_json(account, t, t.gas)
                for t in offered
            ),
        ]
        holding = sum(step.transaction.value for step in first)
        used = await gas_used(chain, sequence, {sender: holding} if holding else {})
        theirs = used[len(first) :]
        signing = [
            ToSign(
                t.chain_id,
                t.nonce,
                t.to,
                t.data,
                t.value,
                ceil(gas * ESTIMATE_HEADROOM),
                max_fee,
                tip,
            )
            if isinstance(t, ToSign) and estimate
            else t
            for t, gas, estimate in zip(own, theirs, estimated, strict=False)
        ]
        word_gas = 0 if token == NATIVE_ASSET else _AMOUNT_WORD_GAS
        payment_gas = ceil(theirs[-1] * ESTIMATE_HEADROOM) + word_gas if sending else 0
        final = [*signing, paying(1, payment_gas)] if sending else signing
        # planned as a sponsorship taken is: a transaction of the relay's given the gas left
        # over is estimated here
        (planned,) = await plan_drafts(chain, [drafting(final)], schedule, sender)
        plan = plan_of(planned)
        pricing.fits(plan)
        pricing.within_cost(plan, latest, deadline - latest.number, field="transactions")
        amount = await pricing.charge(measured, latest, token, plan)
        payment = paying(amount, payment_gas) if sending else None
        quotes.append(SponsoredTokenQuote(token, amount, payment))
        if not filled:
            filled = [None if isinstance(t, Decoded) else t for t in signing]
    return SponsoredQuotes(
        valid_until_block=latest.number + config.quote_blocks,
        max_block_number=deadline,
        forwarder=forwarder,
        gas=tuple(planned.gas for planned in priced.calls),
        transactions=tuple(filled),
        quotes=tuple(quotes),
    )
