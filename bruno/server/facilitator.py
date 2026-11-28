"""The hosted x402 facilitator: a merchant's URL, `/x402/facilitator/<payout>/`, with x402
v2's `/supported`, `/verify` and `/settle` under it. What a URL's path says: the payout
address, the merchant's terms, each a `name=value` segment, and the endpoint asked for."""

import base64
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import msgspec
from eth_typing import ChecksumAddress, Hash32
from eth_utils.address import to_checksum_address

from .. import jsonrpc
from ..accounts import derive
from ..chain import has_code, word_of
from ..common import BrunoError
from ..eip3009 import (
    ECDSA_BYTES,
    TransferAuthorization,
    digest,
    quoted_calldata,
    transfer_digest,
)
from ..erc6492 import WrapperError
from ..evm import EncodedCall
from ..pricing import Payer, Quoted, QuotedCall
from ..refusals import Refusal, Refused, refused
from ..relay import NoCapacityError, Relay, UnderpaidError
from ..reverts import SimulationFailedError
from ..submission import InvalidSubmissionError, Submission, SubmittedCall
from ..targets import boundable
from ..tokens import balance_of_call
from ..tracked import Finished, Status
from .wire import (
    FacilitatorInfo,
    FacilitatorRequest,
    FeeSplit,
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
    hex_data,
)
from .x402 import (
    ASSET_TRANSFER_METHOD,
    EXPIRED,
    INVALID_NETWORK,
    INVALID_PAYLOAD,
    INVALID_REQUIREMENTS,
    INVALID_SCHEME,
    INVALID_SIGNATURE,
    INVALID_VERSION,
    NOT_YET_VALID,
    PAYMENT_FLOW,
    RECIPIENT_MISMATCH,
    SCHEME,
    VALUE_MISMATCH,
    X402_VERSION,
    network,
    transfer_refusal,
)

# the endpoints under a facilitator URL; none is the payout address's own page
ENDPOINTS = ("supported", "verify", "settle")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_AMOUNT = re.compile(r"[0-9]{1,78}")


class InvalidRouteError(BrunoError):
    """A facilitator URL that isn't one: which part (`field`: `payout`, a term's name, or the
    segment itself), and why."""

    def __init__(self, field_: str, why: str) -> None:
        self.field = field_
        self.why = why
        self.refused: Refused = refused(Refusal.MALFORMED, why=why)
        super().__init__(f"{field_}: {self.refused}")


@dataclass(frozen=True, slots=True)
class Terms:
    """A merchant's limits on what Bruno does for it, read from its facilitator URL: the
    `tokens` taken (None: every token the relay takes by EIP-3009), and the most the fee may
    be in a token, in its base units (`max_fee`: a token without one settles at whatever the
    fee is)."""

    tokens: frozenset[ChecksumAddress] | None = None
    max_fee: Mapping[ChecksumAddress, int] = field(default_factory=dict)

    def takes(self, token: ChecksumAddress) -> bool:
        """Whether a payment in `token` is within the terms."""
        return self.tokens is None or token in self.tokens

    def allows(self, token: ChecksumAddress, fee: int) -> bool:
        """Whether a `fee` in `token` is within the terms: at most its cap, if it has one."""
        most = self.max_fee.get(token)
        return most is None or fee <= most


@dataclass(frozen=True, slots=True)
class Route:
    """What a facilitator URL's path names, after its prefix: the `payout` address, the
    merchant's `terms`, and the `endpoint` asked for (one of ENDPOINTS; empty for the payout
    address's own page)."""

    payout: ChecksumAddress
    terms: Terms
    endpoint: str


def route_of(segments: Sequence[str]) -> Route:
    """The route a facilitator URL's path `segments` name, each percent-decoded already: the
    payout address, then the terms, each `name=value` and each at most once, then the
    endpoint, if any. `tokens=<a>,<b>` and `max-fee=<a>:<n>,<b>:<m>`, an address in any case
    and an amount in decimal. InvalidRouteError for anything else: a term unknown or given
    twice, a token twice in one, a cap for a token the terms don't take."""
    if not segments:
        raise InvalidRouteError("payout", "missing: the payout address")
    payout = _address(segments[0], "payout")
    rest = list(segments[1:])
    endpoint = ""
    if rest and "=" not in rest[-1]:
        endpoint = rest.pop()
        if endpoint not in ENDPOINTS:
            raise InvalidRouteError(endpoint, "an unknown endpoint")
    given: dict[str, str] = {}
    for segment in rest:
        name, found, value = segment.partition("=")
        if not found:
            raise InvalidRouteError(segment, "not a term: name=value")
        if name in given:
            raise InvalidRouteError(name, "given twice")
        given[name] = value
    tokens: frozenset[ChecksumAddress] | None = None
    max_fee: dict[ChecksumAddress, int] = {}
    for name, value in given.items():
        if name == "tokens":
            taken = [_address(each, name) for each in value.split(",")]
            if len(set(taken)) != len(taken):
                raise InvalidRouteError(name, "a token twice")
            tokens = frozenset(taken)
        elif name == "max-fee":
            for each in value.split(","):
                token, _, amount = each.partition(":")
                capped = _address(token, name)
                if capped in max_fee:
                    raise InvalidRouteError(name, "a token twice")
                if not _AMOUNT.fullmatch(amount) or int(amount) >= 2**256:
                    raise InvalidRouteError(name, "an amount in the token's base units, decimal")
                max_fee[capped] = int(amount)
        else:
            raise InvalidRouteError(name, "an unknown term")
    if tokens is not None and not max_fee.keys() <= tokens:
        raise InvalidRouteError("max-fee", "a token the terms don't take")
    return Route(payout, Terms(tokens, max_fee), endpoint)


def _address(text: str, field_: str) -> ChecksumAddress:
    """The address `text` is, in any case: InvalidRouteError naming `field_` if it isn't one."""
    if not _ADDRESS.fullmatch(text):
        raise InvalidRouteError(field_, "an address")
    return to_checksum_address(text)


# x402's extension this facilitator answers the merchant by (EXTENSION-RESPONSES): the fee
EXTENSION = "bruno-fee"
# how far ahead a settlement's deadline is, at most: /settle is held until it's included or
# this passes, under the 90 seconds x402's reference client gives a facilitator's answer
WAIT_BLOCKS = 5
# how many slots past the deadline's time the receipt wallet's own authorizations stay valid:
# slots missed before the deadline's block make its time later than expected
_LATE_SLOTS = 2
# why a payment isn't settled, where x402's own codes say nothing: the token isn't taken (by
# the relay, by EIP-3009 at a gas it bounds, or by the merchant's terms); the fee is over the
# terms' cap; the payment doesn't exceed the fee; it wasn't included by its deadline, the
# payment unused, to be tried again
ASSET_NOT_ACCEPTED = "bruno_asset_not_accepted"
FEE_OVER_MAX = "bruno_fee_over_max"
PAYMENT_UNDER_FEE = "bruno_payment_under_fee"
NOT_INCLUDED = "bruno_not_included"
# the reference facilitator's code for a payer that's a smart wallet not deployed yet
UNDEPLOYED_SMART_WALLET = "invalid_exact_evm_payload_undeployed_smart_wallet"
_MAX_UINT256 = 2**256 - 1


class RefusedError(BrunoError):
    """A payment the facilitator won't settle: x402's code for why, or one of Bruno's own."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Split:
    """A settled payment in parts, in `token`'s base units: what the buyer `paid`, the `fee`
    Bruno keeps, what the receipt wallet keeps (`kept`: one unit, the first time it holds the
    token, so its balance's slot stays set), and the merchant's `payout`, the rest."""

    token: ChecksumAddress
    paid: int
    fee: int
    kept: int

    @property
    def payout(self) -> int:
        return self.paid - self.fee - self.kept

    def wire(self) -> FeeSplit:
        return FeeSplit(self.token, str(self.paid), str(self.fee), str(self.kept), str(self.payout))


@dataclass(frozen=True, slots=True)
class Settlement:
    """A payment as Bruno settles it: the `submission` that does (the buyer's transfer to the
    receipt wallet, then the wallet's to the merchant, and as its payment the wallet's of the
    fee to Bruno), the buyer's `transfer` and its `digest`, and the payment's `split`."""

    submission: Submission
    transfer: TransferAuthorization
    digest: bytes
    split: Split


def supported(relays: Mapping[int, Relay]) -> SupportedResponse:
    """What the facilitator settles: `exact` on each chain that takes x402, its extension, and
    each such chain's sending accounts."""
    taking = {chain_id: relay for chain_id, relay in relays.items() if relay.domains}
    return SupportedResponse(
        kinds=tuple(SupportedKind(X402_VERSION, SCHEME, network(c)) for c in taking),
        extensions=(EXTENSION,),
        signers={
            network(chain_id): tuple(
                derive(relay.float_account, i).address for i in range(relay.config.accounts)
            )
            for chain_id, relay in taking.items()
        },
    )


def info(relays: Mapping[int, Relay], route: Route) -> FacilitatorInfo:
    """Where `route`'s payout address is paid, on each chain that takes x402."""
    return FacilitatorInfo(
        payout=route.payout,
        pay_to={
            network(chain_id): relay.receipt_account(route.payout).address
            for chain_id, relay in relays.items()
            if relay.domains
        },
    )


def relay_of(relays: Mapping[int, Relay], request: FacilitatorRequest) -> Relay:
    """The relay of the chain `request`'s payment is on: RefusedError if it's none served."""
    named = request.payment_requirements.network
    for chain_id, relay in relays.items():
        if relay.domains and network(chain_id) == named:
            return relay
    raise RefusedError(INVALID_NETWORK)


def split_headers(split: Split | None) -> dict[str, str]:
    """The headers that tell the merchant of `split`, if there's one: EXTENSION-RESPONSES,
    base64 of a JSON object keyed by the extension's name."""
    if split is None:
        return {}
    value = base64.b64encode(msgspec.json.encode({EXTENSION: split.wire()})).decode()
    return {"EXTENSION-RESPONSES": value}


async def settlement(relay: Relay, route: Route, request: FacilitatorRequest) -> Settlement:
    """`request`'s payment as Bruno would settle it for `route`'s merchant now, checked against
    what was required of it and against the terms, its fee the relay's quote for the three
    transfers: RefusedError why not. Its signature and the buyer's balance aren't checked
    here: the settlement's simulation finds those."""
    payload, required = request.payment_payload, request.payment_requirements
    accepted = payload.accepted
    if request.x402_version != X402_VERSION or payload.x402_version != X402_VERSION:
        raise RefusedError(INVALID_VERSION)
    if required.scheme != SCHEME or accepted.scheme != SCHEME:
        raise RefusedError(INVALID_SCHEME)
    if accepted.network != required.network:
        raise RefusedError(INVALID_NETWORK)
    token = to_checksum_address(required.asset)
    chain_id = relay.chain.chain_id
    # taken by the relay over x402, at a gas it bounds, and by the merchant's terms
    key_signed = EncodedCall(token, quoted_calldata(None))
    if (
        token not in relay.domains
        or to_checksum_address(accepted.asset) != token
        or not boundable(chain_id, key_signed)
        or not route.terms.takes(token)
    ):
        raise RefusedError(ASSET_NOT_ACCEPTED)
    extra = required.extra
    method = None if extra is None else extra.asset_transfer_method
    flow = None if extra is None else extra.payment_flow
    if method not in (None, ASSET_TRANSFER_METHOD) or flow not in (None, PAYMENT_FLOW):
        raise RefusedError(INVALID_REQUIREMENTS)
    receipt = relay.receipt_account(route.payout)
    authorization = payload.payload.authorization
    paid_to = (to_checksum_address(authorization.to), to_checksum_address(required.pay_to))
    if paid_to != (receipt.address, receipt.address):
        raise RefusedError(RECIPIENT_MISMATCH)
    # exact: the transfer is of the amount required, no more and no less
    amount = required.amount
    if not (amount.isascii() and amount.isdecimal()) or int(amount) != int(authorization.value):
        raise RefusedError(VALUE_MISMATCH)
    if any(
        int(each) > _MAX_UINT256
        for each in (authorization.value, authorization.valid_after, authorization.valid_before)
    ):
        raise RefusedError(INVALID_PAYLOAD)
    transfer = payload.transfer()
    try:
        deadline = await relay.x402_deadline(
            transfer.valid_after, transfer.valid_before, WAIT_BLOCKS
        )
    except InvalidSubmissionError as e:
        raise RefusedError(NOT_YET_VALID if e.field == "validAfter" else EXPIRED) from e
    at = relay.chain.seen_tag
    code, held = await relay.chain.batch(
        [
            jsonrpc.Call("eth_getCode", [transfer.payer, at]),
            balance_of_call(token, receipt.address, at),
        ],
        atomic=True,
    )
    try:
        wrapped = transfer.deployment is not None
    except WrapperError as e:
        raise RefusedError(INVALID_SIGNATURE) from e
    if wrapped and not has_code(jsonrpc.result_of(code)):
        raise RefusedError(UNDEPLOYED_SMART_WALLET)
    kept = 0 if word_of(jsonrpc.result_of(held)) else 1
    # priced with the wallet's own transfers at their dearest, no byte of theirs zero, so the
    # fee covers them whatever they come to hold
    paying = EncodedCall(token, transfer.calldata())
    dearest = TransferAuthorization(
        receipt.address,
        route.payout,
        _MAX_UINT256,
        _MAX_UINT256,
        _MAX_UINT256,
        b"\xff" * 32,
        b"\xff" * ECDSA_BYTES,
    )
    paying_out = EncodedCall(token, dearest.calldata())
    quoted = Quoted(tuple(QuotedCall(call, len(call.data)) for call in (paying, paying_out)))
    quotes = await relay.quote(quoted, [token], Payer(address=receipt.address))
    (quote,) = quotes.quotes
    split = Split(token, transfer.value, quote.amount, kept)
    if split.payout <= 0:
        raise RefusedError(PAYMENT_UNDER_FEE)
    if not route.terms.allows(token, split.fee):
        raise RefusedError(FEE_OVER_MAX)
    latest = await relay.measuring.latest()
    lasting = (deadline - latest.number + _LATE_SLOTS) * relay.chain.slot
    valid_before = latest.timestamp + lasting

    def signed(to: ChecksumAddress, value: int) -> TransferAuthorization:
        """The receipt wallet's authorization of `value` of the token to `to`, signed."""
        nonce = os.urandom(32)
        domain = relay.domains[token]
        hash_ = digest(domain, chain_id, token, receipt.address, to, value, 0, valid_before, nonce)
        signature = bytes(receipt.unsafe_sign_hash(Hash32(hash_)).signature)
        return TransferAuthorization(receipt.address, to, value, 0, valid_before, nonce, signature)

    out = EncodedCall(token, signed(route.payout, split.payout).calldata())
    gas = quotes.gas or (None, None)
    submission = Submission(
        calls=tuple(
            SubmittedCall(call, given) for call, given in zip((paying, out), gas, strict=True)
        ),
        authorizations=(),
        token=token,
        payee=relay.payee,
        amount=split.fee,
        max_block_number=deadline,
        transfer=signed(relay.payee, split.fee),
    )
    return Settlement(
        submission,
        transfer,
        transfer_digest(relay.domains[token], chain_id, token, transfer),
        split,
    )


async def verify(
    relay: Relay, route: Route, request: FacilitatorRequest
) -> tuple[VerifyResponse, Split | None]:
    """x402's /verify: whether `request`'s payment would be settled for `route`'s merchant
    now, its settlement made and simulated and nothing sent; and its split, as a quote."""
    payer = to_checksum_address(request.payment_payload.payload.authorization.from_)
    try:
        made = await settled_as(relay, route, request)
    except RefusedError as e:
        return VerifyResponse(is_valid=False, invalid_reason=e.reason, payer=payer), None
    try:
        await relay.would_take(made.submission)
    except SimulationFailedError:
        why = await transfer_refusal(relay, made.split.token, made.transfer)
        return VerifyResponse(is_valid=False, invalid_reason=why, payer=payer), None
    return VerifyResponse(is_valid=True, payer=payer), made.split


async def settled_as(relay: Relay, route: Route, request: FacilitatorRequest) -> Settlement:
    """`settlement`, a payment whose own transfer fails its quote's estimate refused for why
    (a contract payer's, which the relay doesn't bound)."""
    try:
        return await settlement(relay, route, request)
    except SimulationFailedError as e:
        transfer = request.payment_payload.transfer()
        token = to_checksum_address(request.payment_requirements.asset)
        raise RefusedError(await transfer_refusal(relay, token, transfer)) from e


async def settle(
    relay: Relay, route: Route, request: FacilitatorRequest
) -> tuple[SettleResponse, Split | None]:
    """x402's /settle: `request`'s payment settled for `route`'s merchant, held until its
    submission is included or its deadline passes. One being settled already, or settled, is
    answered by that submission; one that wasn't included is tried again. Its split is
    answered by the call that made the settlement."""
    payer = to_checksum_address(request.payment_payload.payload.authorization.from_)
    named = network(relay.chain.chain_id)

    def failed(reason: str) -> SettleResponse:
        return SettleResponse(
            success=False, transaction="", network=named, error_reason=reason, payer=payer
        )

    token = to_checksum_address(request.payment_requirements.asset)
    if token not in relay.domains:
        return failed(ASSET_NOT_ACCEPTED), None
    buying = request.payment_payload.transfer()
    paid = transfer_digest(relay.domains[token], relay.chain.chain_id, token, buying)
    split: Split | None = None
    id_ = relay.queue.settlement(paid)
    if id_ is not None:
        earlier = relay.queue.status(id_)
        if isinstance(earlier, Finished) and earlier.status is not Status.INCLUDED:
            id_ = None  # not included: another try
    if id_ is None:
        try:
            made = await settled_as(relay, route, request)
            id_ = (await relay.submit(made.submission, settlement=made.digest)).id
            split = made.split
        except RefusedError as e:
            return failed(e.reason), None
        except SimulationFailedError:
            return failed(await transfer_refusal(relay, token, buying)), None
        except UnderpaidError, NoCapacityError:
            # the fee moved, or the queue is full: nothing sent, to be tried again
            return failed(NOT_INCLUDED), None
    tracked = await relay.queue.outcome(id_)
    if tracked.status is Status.INCLUDED and tracked.transaction_hash is not None:
        return (
            SettleResponse(
                success=True,
                transaction=hex_data(tracked.transaction_hash),
                network=named,
                payer=payer,
                amount=str(buying.value),
            ),
            split,
        )
    if tracked.revert_data is not None:  # it stopped simulating: why, if it's known
        return failed(await transfer_refusal(relay, token, buying)), None
    return failed(NOT_INCLUDED), None
