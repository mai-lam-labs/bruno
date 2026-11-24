"""A sponsorship's quote: what an account is to sign, filled in (each transaction's nonce, fees
and gas), the payment the relay offers in each token, and what it's charged
(docs/development/plans/sponsorship.md). One simulation a token, of the whole sequence with that
token's payment in it; priced as a submission of what's quoted would be."""

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil

from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress

from .abi import address_word, word
from .chain import Chain, Latest, market_tip, risen_base_fee
from .config import ChainConfig
from .evm import NATIVE_ASSET, EncodedCall
from .forks import NONZERO_BYTE_GAS, ZERO_BYTE_GAS
from .forwarder import TRANSFER
from .measurements import Measurements, schedule_of
from .plan import account_state, funding_overhead, sandwich, signed_json
from .pricing import ESTIMATE_HEADROOM, Pricing
from .refusals import Refusal, refused
from .simulation import gas_used
from .sponsorship import Sponsorship
from .submission import InvalidSubmissionError
from .transactions import Decoded, SignedTransaction, authorization_json

# an amount's word in a payment's calldata, at its dearest over a unit's: every byte nonzero.
# A payment's gas is estimated paying a unit, the amount not known until it's priced
_AMOUNT_WORD_GAS = 32 * (NONZERO_BYTE_GAS - ZERO_BYTE_GAS)
# the largest amount: a quote's stand-in pays it, its charge the same whatever's paid
_MOST = 2**256 - 1


@dataclass(frozen=True, slots=True)
class Unsigned:
    """A transaction an account is to sign, as a quote is asked about it: its gas estimated if
    it's None; `to` None for a contract creation."""

    sender: ChecksumAddress
    to: ChecksumAddress | None
    data: bytes
    value: int
    gas: int | None


@dataclass(frozen=True, slots=True)
class Asked:
    """What a sponsorship's quote is asked: the account's authorizations, signed; the call the
    relay's first transaction makes into it, and its gas, estimated if None; its own
    transactions, each signed already or to be; and its deadline, the furthest one if None."""

    authorizations: tuple[SignedSetCodeAuthorization, ...]
    call: EncodedCall | None
    gas: int | None
    transactions: tuple[Decoded | Unsigned, ...]
    max_block_number: int | None

    @property
    def account(self) -> ChecksumAddress:
        """The account sponsored: its first transaction's sender, or its call's target with
        none. InvalidSubmissionError with neither."""
        if self.transactions:
            first = self.transactions[0]
            return first.sender if isinstance(first, Unsigned) else first.signed.sender
        if self.call is not None:
            return self.call.to
        raise InvalidSubmissionError("sponsored", refused(Refusal.SPONSORED_NOTHING))


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
    pays, the gas its call is given (None with no call or authorization), each of its
    transactions to sign (None for one signed already), and its quote in each token."""

    valid_until_block: int
    max_block_number: int
    forwarder: ChecksumAddress
    gas: int | None
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
    """`asked`'s quote in each of `tokens`. Each transaction not signed yet is given the
    account's next nonce (after its authorizations, which the relay's first transaction
    spends), the max fee a block may ask by the deadline, and the market's tip. Then, a token
    at a time: with transactions of the account's, the payment in that token is added after
    them, paying a unit (with none, the account's code pays, in the relay's call, and no
    payment is offered); the sequence is simulated once, the relay's first transaction then
    the account's, for the gas each uses, a transaction's limit that and ESTIMATE_HEADROOM
    where none was given (the payment's with what its amount's word may add); and the whole is
    planned and charged as a sponsorship of it would be. A transaction that fails is the
    quote's simulation failing."""
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
    data = b"" if asked.call is None else asked.call.data
    runs_code = asked.call is not None or bool(asked.authorizations)
    sending = bool(asked.transactions)
    values = sum(t.signed.value if isinstance(t, Decoded) else t.value for t in own)
    first: dict[str, object] = {
        "from": sender,
        "to": account,
        "data": "0x" + data.hex(),
        "value": hex(values + 1 if sending else 0),
        "gas": hex(config.max_gas),
    }
    if asked.authorizations:
        first["authorizationList"] = [authorization_json(a) for a in asked.authorizations]
    overhead = funding_overhead(schedule, data, len(asked.authorizations), sending, state.empty)
    call_gas = asked.gas
    filled: list[ToSign | None] = []
    quotes: list[SponsoredTokenQuote] = []
    for token in tokens:

        def paying(amount: int, gas: int, token: ChecksumAddress = token) -> ToSign:
            """The payment of `amount` in `token`, to sign, given `gas`."""
            call = payment_call(forwarder, token, amount)
            return ToSign(chain.chain_id, nonce, call.to, call.data, call.value, gas, max_fee, tip)

        calls = [
            first,
            *(
                signed_json(t) if isinstance(t, Decoded) else _call_json(account, t, t.gas)
                for t in own
            ),
        ]
        if sending:  # the payment offered, paying a unit, after the account's own
            calls.append(_call_json(account, paying(1, config.max_gas), config.max_gas))
        used = await gas_used(chain, calls, {sender: values + 1} if sending else {})
        if call_gas is None:
            call_gas = ceil(max(0, used[0] - overhead) * ESTIMATE_HEADROOM) if runs_code else 0
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
            for t, gas, estimate in zip(own, used[1 : 1 + len(own)], estimated, strict=True)
        ]
        word_gas = 0 if token == NATIVE_ASSET else _AMOUNT_WORD_GAS
        payment_gas = ceil(used[-1] * ESTIMATE_HEADROOM) + word_gas if sending else 0
        drafts = [t if isinstance(t, Decoded) else _drafted(t, account) for t in signing]
        if sending:
            drafts.append(_drafted(paying(1, payment_gas), account))
        stand_in = Sponsorship(
            account=account,
            authorizations=asked.authorizations,
            call=asked.call,
            gas=call_gas,
            transactions=tuple(drafts),
            token=token,
            payee=forwarder,
            amount=_MOST,
            max_block_number=deadline,
        )
        plan = sandwich(
            chain.chain_id,
            stand_in,
            schedule,
            measured.tokens[token],
            account_empty=state.empty,
            payee_holds=token in measured.payee_holds,
            payee_new=measured.payee_new,
        )
        pricing.fits(plan)
        amount = await pricing.charge(measured, latest, token, plan)
        payment = paying(amount, payment_gas) if sending else None
        quotes.append(SponsoredTokenQuote(token, amount, payment))
        if not filled:
            filled = [None if isinstance(t, Decoded) else t for t in signing]
    return SponsoredQuotes(
        valid_until_block=latest.number + config.quote_blocks,
        max_block_number=deadline,
        forwarder=forwarder,
        gas=call_gas if runs_code else None,
        transactions=tuple(filled),
        quotes=tuple(quotes),
    )
