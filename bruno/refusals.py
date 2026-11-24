"""What a refusal is (docs/development/design/refusals.md): its name, the `reason` every error
carries, who can act on it, and one template a refusal for what it says, the same on both
relays. The Worker reads these as exported (make vectors): neither relay writes a refusal's
text inline, so they can't drift apart.

A template's `{name}`s are its values, each given as text or a number; a value it doesn't name
is an error, as one it names and isn't given."""

import string
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import override


class Category(StrEnum):
    """Who can act on a refusal: the client, trying again later (`retry`) or asking for
    something else (`request`); or only the relay's operator (`operator`)."""

    RETRY = "retry"
    REQUEST = "request"
    OPERATOR = "operator"


class Refusal(StrEnum):
    """Each refusal, by name: an error's `reason`."""

    # what doesn't decode as the request's types: the decoder's own words
    MALFORMED = "malformed"
    # the request's own fields, after `field: `
    TOKENS_NONE = "tokens-none"
    TOKENS_TOO_MANY = "tokens-too-many"
    QUOTE_NO_CALL = "quote-no-call"
    QUOTE_NO_GAS = "quote-no-gas"
    AUTHORITY_UNDELEGATED = "authority-undelegated"
    AUTHORITY_AND_LIST = "authority-and-list"
    CALL_LENGTH_MISMATCH = "call-length-mismatch"
    GAS_OVER = "gas-over"
    CALL_TOO_LONG = "call-too-long"
    RUNNER_UNBUILT = "runner-unbuilt"
    SIGNATURE_WRAPPER = "signature-wrapper"
    TRANSACTION_OVER = "transaction-over"
    VERIFY_GAS_NO_PAYER = "verify-gas-no-payer"
    FACTORY_FAILS = "factory-fails"
    DEADLINE_PASSED = "deadline-passed"
    DEADLINE_FAR = "deadline-far"
    VALID_AFTER = "valid-after"
    VALID_BEFORE = "valid-before"
    TRANSFER_NOT_PAYMENT = "transfer-not-payment"
    TOKEN_SUSPENDED = "token-suspended"
    NO_EIP3009 = "no-eip3009"
    DEPLOYMENT_NOT_SIGNATURES = "deployment-not-signatures"
    AUTHORIZATION_UNRECOVERABLE = "authorization-unrecoverable"
    AUTHORIZATION_Y_PARITY = "authorization-y-parity"
    AUTHORIZATION_R_S = "authorization-r-s"
    AUTHORIZATION_NONCE = "authorization-nonce"
    AUTHORIZATIONS_OVER = "authorizations-over"
    AUTHORIZATION_CHAIN = "authorization-chain"
    # a sponsorship's fields
    SPONSORED_NOTHING = "sponsored-nothing"
    SPONSORED_NOT_ACCOUNTS = "sponsored-not-accounts"
    SPONSORED_NONCE = "sponsored-nonce"
    NOT_FORWARDED = "not-forwarded"
    # a user operation's fields
    ENTRY_POINT = "entry-point"
    EIP7702_FACTORY = "eip7702-factory"
    PAYMASTER_FIELDS = "paymaster-fields"
    PAYMASTER_PAYS_BY_FEES = "paymaster-pays-by-fees"
    FEES_PAY_NOTHING = "fees-pay-nothing"
    FEES_ESTIMATED = "fees-estimated"
    LIMITS_GIVEN = "limits-given"
    OPERATION_PAYS_NOTHING = "operation-pays-nothing"
    OPERATION_OVER = "operation-over"
    EIP7702_UNSIGNED = "eip7702-unsigned"
    EIP7702_NOT_SENDER = "eip7702-not-sender"
    SIGNATURE_REFUSED = "signature-refused"
    # what's paid, and with what
    UNDERPAID = "underpaid"
    NO_CAPACITY = "no-capacity"
    NOT_ACCEPTED = "not-accepted"
    ETH_NOT_ACCEPTED = "eth-not-accepted"
    SUSPENDED = "suspended"
    WRONG_PAYEE = "wrong-payee"
    UNKNOWN_SUBMISSION = "unknown-submission"
    # the chain, unavailable: why, then what to do by its category
    NOT_MEASURED = "not-measured"
    WRONG_CHAIN = "wrong-chain"
    UNKNOWN_FORK = "unknown-fork"
    NO_SCHEDULE = "no-schedule"
    NO_DEPLOYER = "no-deployer"
    NO_SIMULATION = "no-simulation"
    UNKNOWN_FEE_FORMULA = "unknown-fee-formula"
    MIN_FEE_SUSPENDED = "min-fee-suspended"
    BLOCK_UNSEEN = "block-unseen"
    NODE_FAILED = "node-failed"
    # the path
    NOT_SERVED = "not-served"
    NOT_FOUND = "not-found"
    METHOD_NOT_ALLOWED = "method-not-allowed"
    NO_X402 = "no-x402"
    # a simulation's revert
    REVERT_UNDERPAID = "revert-underpaid"
    REVERT_GAS_EXHAUSTED = "revert-gas-exhausted"
    REVERT_BALANCE_UNREADABLE = "revert-balance-unreadable"
    REVERT_FAILED_OP = "revert-failed-op"
    REVERT_FAILED_OP_WITH_REVERT = "revert-failed-op-with-revert"
    REVERT_ERROR = "revert-error"
    REVERT_PANIC = "revert-panic"
    REVERT_EMPTY = "revert-empty"
    REVERT_OTHER = "revert-other"

    @property
    def category(self) -> Category:
        """Who can act on it: what passes by itself is retried, what's in the relay's
        configuration or its chain is its operator's, and the rest is the client's to ask
        otherwise."""
        if self in _RETRIED:
            return Category.RETRY
        if self in _OPERATORS:
            return Category.OPERATOR
        return Category.REQUEST


class Wording(StrEnum):
    """Message text that's never an error's reason, each a template as a refusal's is: a
    decoder's wording of what's malformed, a category's next step, a failed simulation's frame
    around its revert's refusal, and x402's own descriptions."""

    # what's malformed, in the decoder's words
    HASH_MALFORMED = "hash-malformed"
    PARAMS_ONE = "params-one"
    PARAMS_NONE = "params-none"
    PARAMS_OPERATION = "params-operation"
    # what a client can do, by a refusal's category
    NEXT_RETRY = "next-retry"
    NEXT_REQUEST = "next-request"
    NEXT_OPERATOR = "next-operator"
    # a failed simulation, around its revert's refusal
    SIMULATION_FAILED = "simulation-failed"
    # x402's payment
    X402_NO_CALL = "x402-no-call"
    X402_NO_SIGNATURE = "x402-no-signature"
    X402_VERSION = "x402-version"
    X402_SCHEME = "x402-scheme"
    X402_NETWORK = "x402-network"
    X402_TOKEN = "x402-token"
    X402_PAYEE = "x402-payee"
    X402_METHOD = "x402-method"
    X402_VALUE = "x402-value"
    X402_UINT256 = "x402-uint256"
    X402_FINISHED = "x402-finished"
    X402_DEADLINE = "x402-deadline"


_RETRIED = frozenset(
    {
        Refusal.SUSPENDED,
        Refusal.NOT_MEASURED,
        Refusal.MIN_FEE_SUSPENDED,
        Refusal.BLOCK_UNSEEN,
        Refusal.NODE_FAILED,
    }
)
_OPERATORS = frozenset(
    {
        Refusal.WRONG_CHAIN,
        Refusal.UNKNOWN_FORK,
        Refusal.NO_SCHEDULE,
        Refusal.NO_DEPLOYER,
        Refusal.NO_SIMULATION,
        Refusal.UNKNOWN_FEE_FORMULA,
    }
)


@dataclass(frozen=True, slots=True)
class Refused:
    """A refusal as an error carries it: which, and the values its template names. Its text
    is the template with them (str)."""

    refusal: Refusal
    values: Mapping[str, str | int]

    @override
    def __str__(self) -> str:
        return message(self.refusal, **self.values)


def refused(refusal: Refusal, **values: str | int) -> Refused:
    """`refusal` with `values`: ValueError, as message raises it, if they aren't its
    template's."""
    message(refusal, **values)
    return Refused(refusal, values)


TEMPLATES: dict[Refusal | Wording, str] = {
    Refusal.MALFORMED: "{why}",
    Refusal.TOKENS_NONE: "at least one: name a token postpaid_config lists",
    Refusal.TOKENS_TOO_MANY: "more than are accepted: name each once, from postpaid_config's list",
    Wording.HASH_MALFORMED: "32 bytes, as hex",
    Wording.PARAMS_ONE: "one parameter, by position",
    Wording.PARAMS_NONE: "none",
    Wording.PARAMS_OPERATION: "two parameters, by position: the user operation and its EntryPoint",
    Refusal.QUOTE_NO_CALL: "missing: the call, or its data's length",
    Refusal.QUOTE_NO_GAS: "missing: needed with only the call's length; or give the call whole",
    Refusal.AUTHORITY_UNDELEGATED: (
        "needed: {authority} isn't delegated yet, so the call can't be run to estimate it; give "
        "gas, or sign the authorization and quote with authorizationList"
    ),
    Refusal.AUTHORITY_AND_LIST: "with authorizationList: give one or the other",
    Refusal.CALL_LENGTH_MISMATCH: (
        "{given}, but the call's data is {length}. Leave callDataLength out with the call"
    ),
    Refusal.GAS_OVER: "over {most}, the most a call may be given",
    Refusal.CALL_TOO_LONG: "over {most} bytes, the most a call's data may be",
    Refusal.RUNNER_UNBUILT: "{why}",
    Refusal.SIGNATURE_WRAPPER: "{why}",
    Refusal.TRANSACTION_OVER: (
        "the transaction needs {needs}, over {most}: give the call less gas, or split it into "
        "smaller calls"
    ),
    Refusal.VERIFY_GAS_NO_PAYER: "only with a payer: name the payer, or leave verifyGas out",
    Refusal.FACTORY_FAILS: (
        "its factory call fails: {why}. Check the factory and its data, or leave the deployment "
        "out if the account exists"
    ),
    Refusal.DEADLINE_PASSED: "passed: name a later block",
    Refusal.DEADLINE_FAR: "over {ahead} blocks ahead: name an earlier block",
    Refusal.VALID_AFTER: "not valid by the next block: sign it valid from now",
    Refusal.VALID_BEFORE: "passed: sign the transfer valid for longer",
    Refusal.TRANSFER_NOT_PAYMENT: (
        "the transfer isn't the payment: its to must be the payee, and its value the amount"
    ),
    Refusal.TOKEN_SUSPENDED: (
        "{token} is suspended. Pay in another token postpaid_config lists, or try again later"
    ),
    Refusal.NO_EIP3009: (
        "{token} takes no EIP-3009 transfer: pay in a token x402 takes, or have the call pay "
        "for itself"
    ),
    Refusal.DEPLOYMENT_NOT_SIGNATURES: (
        "not the one the payment's signature deploys: name that one, or none"
    ),
    Refusal.AUTHORIZATION_UNRECOVERABLE: "unrecoverable: {why}. Sign it again",
    Refusal.AUTHORIZATION_Y_PARITY: "yParity is 0 or 1",
    Refusal.AUTHORIZATION_R_S: "r or s out of range",
    Refusal.AUTHORIZATION_NONCE: "nonce out of range",
    Refusal.AUTHORIZATIONS_OVER: "over {most}: send at most that many",
    Refusal.AUTHORIZATION_CHAIN: "chain id {chain_id}: sign it for this chain, or for any (0)",
    Refusal.SPONSORED_NOTHING: "neither a call nor a transaction: give at least one",
    Refusal.SPONSORED_NOT_ACCOUNTS: "{given}'s, not the sponsored account's, {account}",
    Refusal.SPONSORED_NONCE: "nonce {nonce}, where {following} follows the transaction before it",
    Refusal.NOT_FORWARDED: "{token}'s transfer by the forwarder didn't measure: pay in another",
    Refusal.ENTRY_POINT: "only {taken}",
    Refusal.EIP7702_FACTORY: "0x7702 is EntryPoint v0.8's",
    Refusal.PAYMASTER_FIELDS: (
        "none, but its other fields given: name the paymaster, or leave its fields out"
    ),
    Refusal.PAYMASTER_PAYS_BY_FEES: "zero: an op with a paymaster pays the relay by its fees",
    Refusal.FEES_PAY_NOTHING: "pays nothing a gas as the base fee may fall",
    Refusal.FEES_ESTIMATED: "above zero: an op paying by its fees is priced by its estimate",
    Refusal.LIMITS_GIVEN: "none: a signed op gives its limits",
    Refusal.OPERATION_PAYS_NOTHING: (
        "pays the relay's payee nothing, in any token it takes. End its calls with a payment to "
        "the payee of what postpaid_quoteUserOperation quotes"
    ),
    Refusal.OPERATION_OVER: "the operation needs {needs}, over {most}",
    Refusal.EIP7702_UNSIGNED: (
        "none, and the sender isn't delegated: add the sender's signed authorization"
    ),
    Refusal.EIP7702_NOT_SENDER: "not signed by the sender",
    Refusal.SIGNATURE_REFUSED: (
        "{whose} doesn't take its signature: sign the op's hash for this EntryPoint and chain"
    ),
    Refusal.UNDERPAID: (
        "underpaid: {required} of {token} is required. Sign the payment again for at least "
        "that, and send it again"
    ),
    Refusal.NO_CAPACITY: (
        "no capacity: the queue is full. A payment of at least {required} of {token} would be "
        "taken: pay that, or try again in a few blocks"
    ),
    Refusal.NOT_ACCEPTED: "{token} isn't accepted: postpaid_config lists the tokens taken",
    Refusal.ETH_NOT_ACCEPTED: (
        "ETH isn't accepted here, and an op paying by its fees pays in it. Pay by the op's "
        "calls instead (maxFeePerGas 0)"
    ),
    Refusal.SUSPENDED: (
        "{token} is suspended: {why}. Pay in another token postpaid_config lists, or try again "
        "later"
    ),
    Refusal.WRONG_PAYEE: "the payee is {payee}, not {given}. Pay the payee postpaid_config names",
    Refusal.UNKNOWN_SUBMISSION: (
        "no submission {id}: unknown here, or finished long enough ago to be forgotten. Check "
        "the id, or submit it again"
    ),
    Refusal.NOT_MEASURED: "not measured yet: its nodes didn't answer",
    Refusal.WRONG_CHAIN: "the nodes are chain {nodes}, not {chain}",
    Refusal.UNKNOWN_FORK: "a fork no schedule knows: a plain call costs {gas}",
    Refusal.NO_SCHEDULE: "no schedule measured",
    Refusal.NO_DEPLOYER: "the deployer isn't at {deployer}",
    Refusal.NO_SIMULATION: "the nodes don't answer eth_simulateV1, which every simulation uses",
    Refusal.UNKNOWN_FEE_FORMULA: (
        "an OP-stack fee formula Bruno doesn't know: its oracle answers otherwise"
    ),
    Refusal.MIN_FEE_SUSPENDED: "min_fee's token {token} is suspended",
    Refusal.BLOCK_UNSEEN: "the nodes aren't at block {block} yet",
    Refusal.NODE_FAILED: "a node failed. Try again in a few seconds",
    Wording.NEXT_RETRY: "Try again in a few seconds",
    Wording.NEXT_REQUEST: "Ask for something else",
    Wording.NEXT_OPERATOR: "Only the relay's operator can fix this: use another relay meanwhile",
    Refusal.NOT_SERVED: "chain {chain} isn't served here: see /",
    Refusal.NOT_FOUND: "not found: see /",
    Refusal.METHOD_NOT_ALLOWED: "method not allowed: see /",
    Refusal.NO_X402: (
        "chain {chain} takes no x402 payments: pay through postpaid_sendTransaction, or on a "
        "chain that does: see /"
    ),
    Wording.SIMULATION_FAILED: "simulation failed: {reason}",
    Refusal.REVERT_UNDERPAID: (
        "the payee gained less than the payment's amount during the call. Make the call pay "
        "it, or pay by a signed transfer"
    ),
    Refusal.REVERT_GAS_EXHAUSTED: (
        "the call couldn't be given its gas. Quote the call again for its gas"
    ),
    Refusal.REVERT_BALANCE_UNREADABLE: (
        "the payment token's balance couldn't be read. Pay in another token"
    ),
    Refusal.REVERT_FAILED_OP: (
        'the EntryPoint refused the op: "{text}". Check its signature and nonce, and that its '
        "account, or its paymaster, can pay for it"
    ),
    Refusal.REVERT_FAILED_OP_WITH_REVERT: (
        'the EntryPoint refused the op: "{text}" ({inner}). Check its signature and nonce, and '
        "that its account, or its paymaster, can pay for it"
    ),
    Refusal.REVERT_ERROR: 'the call reverted: "{text}"',
    Refusal.REVERT_PANIC: "the call panicked (0x{code})",
    Refusal.REVERT_EMPTY: "the call reverted, saying nothing",
    Refusal.REVERT_OTHER: "the call reverted with error {selector}",
    Wording.X402_NO_CALL: (
        "the body isn't a call: post the call (to, data) and, if you know it, its gas, for its "
        "price"
    ),
    Wording.X402_NO_SIGNATURE: (
        "PAYMENT-SIGNATURE is required: sign a transfer for one of these and send it with the "
        "same body"
    ),
    Wording.X402_VERSION: "x402Version {version}: only 2 is taken",
    Wording.X402_SCHEME: "scheme {scheme}: only exact is taken",
    Wording.X402_NETWORK: "network {network}: this endpoint is {expected}",
    Wording.X402_TOKEN: "{token} isn't accepted over x402: pay in one the 402 lists",
    Wording.X402_PAYEE: "the payee is {payee}: pay it",
    Wording.X402_METHOD: "only {method}, {flow} is taken",
    Wording.X402_VALUE: (
        "value isn't the amount {amount}: sign the transfer for exactly accepted.amount"
    ),
    Wording.X402_UINT256: "{name} isn't a uint256",
    Wording.X402_FINISHED: "{status}: {why}. Pay again, at the price this 402 asks",
    Wording.X402_DEADLINE: "its deadline passed",
}


def message(refusal: Refusal | Wording, **values: str | int) -> str:
    """`refusal`'s template (or a wording's) with `values`: ValueError if one it names isn't
    given, or one given isn't named."""
    named = {name for _, name, _, _ in string.Formatter().parse(TEMPLATES[refusal]) if name}
    if named != set(values):
        raise ValueError(f"{refusal}: names {sorted(named)}, given {sorted(values)}")
    return TEMPLATES[refusal].format_map(values)


def exported() -> dict[str, dict[str, dict[str, str]]]:
    """Each refusal by name, its template and its category, and each wording's template, as
    the Worker reads them (worker/src/refusals.json)."""
    return {
        "refusals": {
            str(t): {"template": template, "category": str(t.category)}
            for t, template in TEMPLATES.items()
            if isinstance(t, Refusal)
        },
        "wordings": {
            str(t): {"template": template}
            for t, template in TEMPLATES.items()
            if isinstance(t, Wording)
        },
    }
