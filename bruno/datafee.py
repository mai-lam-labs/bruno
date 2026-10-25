"""What an OP-stack transaction pays beyond its gas: the L1 data fee and the operator fee, taken
from its sender's balance on top of gas x fee (and a node's pool requires the balance to cover
them). Ethereum has neither: no `DataFee` at all.

The formulas are GasPriceOracle's (ethereum-optimism/optimism,
packages/contracts-bedrock/src/L2/GasPriceOracle.sol, v1.6.0): Fjord's L1 fee upper bound for
an unsigned transaction's size (getL1FeeUpperBound), and Jovian's operator fee (getOperatorFee).
They're computed here, from the inputs read with each block, never assumed to hold: a chain is
measured (measurements.measure) by asking its oracle for both at sample sizes and comparing,
and one whose oracle answers otherwise isn't served."""

from dataclasses import dataclass, replace

from eth_utils.crypto import keccak

from .net.jsonrpc import Answer, Call

# the OP stack's predeploys: the fee oracle, and the L1 attributes it reads
GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"
L1_BLOCK = "0x4200000000000000000000000000000000000015"

# Fjord's regression from a FastLZ size to an estimated Brotli size, scaled by 1e6, and the
# smallest size it's taken as
_COST_INTERCEPT = -42_585_600
_COST_FASTLZ_COEF = 836_500
_MIN_TRANSACTION_SIZE = 100
_SCALE = 10**6
# getL1FeeUpperBound: the signature's bytes added to an unsigned transaction's size, and
# FastLZ's practical bound on a size (n + n/255 + 16, which covers 99.99% of transactions)
_SIGNATURE_BYTES = 68
_FASTLZ_SLACK_DIVISOR = 255
_FASTLZ_SLACK_BYTES = 16
# Jovian's operator fee: gas x scalar x 100, and the constant
_OPERATOR_FEE_MULTIPLIER = 100
# what the L1 fees can rise to a block: EIP-1559's base fee by an eighth; a blob base fee by
# less (EIP-4844's most, at any schedule's max over target, is under an eighth). Risen for as
# many blocks as the L2's, which are never fewer than the L1's in the same time
_MAX_L1_FEE_RISE_NUMERATOR = 9
_MAX_L1_FEE_RISE_DENOMINATOR = 8
# the sizes and gas the oracle is asked for, to check the formulas against (measured): a size
# under the regression's floor and one over it; no gas (the constant) and some
CHECKED_SIZES = (0, 40_000)
CHECKED_GAS = (0, 1_000_000)


def _selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def _call(to: str, data: str, block: str) -> Call:
    return Call("eth_call", [{"to": to, "data": data}, block])


def _word(n: int) -> str:
    return n.to_bytes(32, "big").hex()


# each input, from the predeploy that answers it, in DataFee's order
_INPUTS = (
    (GAS_PRICE_ORACLE, "l1BaseFee()"),
    (GAS_PRICE_ORACLE, "blobBaseFee()"),
    (GAS_PRICE_ORACLE, "baseFeeScalar()"),
    (GAS_PRICE_ORACLE, "blobBaseFeeScalar()"),
    (L1_BLOCK, "operatorFeeScalar()"),
    (L1_BLOCK, "operatorFeeConstant()"),
)


@dataclass(frozen=True, slots=True)
class DataFee:
    """An OP-stack chain's fee inputs as of a block: the L1 base fee and blob base fee, their
    scalars, and the operator fee's scalar and constant."""

    l1_base_fee: int
    blob_base_fee: int
    base_fee_scalar: int
    blob_base_fee_scalar: int
    operator_fee_scalar: int
    operator_fee_constant: int

    def l1_fee(self, unsigned_size: int) -> int:
        """getL1FeeUpperBound(unsigned_size): the most an unsigned transaction of
        `unsigned_size` bytes pays for its data, signed."""
        size = unsigned_size + _SIGNATURE_BYTES
        fastlz = size + size // _FASTLZ_SLACK_DIVISOR + _FASTLZ_SLACK_BYTES
        estimated = max(
            _COST_INTERCEPT + _COST_FASTLZ_COEF * fastlz, _MIN_TRANSACTION_SIZE * _SCALE
        )
        scaled = (
            self.base_fee_scalar * 16 * self.l1_base_fee
            + self.blob_base_fee_scalar * self.blob_base_fee
        )
        return estimated * scaled // _SCALE**2

    def operator_fee(self, gas: int) -> int:
        """getOperatorFee(gas): what `gas` used pays the operator."""
        return gas * self.operator_fee_scalar * _OPERATOR_FEE_MULTIPLIER + (
            self.operator_fee_constant
        )

    def fee(self, unsigned_size: int, gas: int) -> int:
        """The most a transaction of `unsigned_size` bytes using up to `gas` pays beyond gas."""
        return self.l1_fee(unsigned_size) + self.operator_fee(gas)

    def risen(self, blocks: int) -> DataFee:
        """These inputs, the L1 fees risen as far as they can in `blocks` blocks."""
        num, den = _MAX_L1_FEE_RISE_NUMERATOR**blocks, _MAX_L1_FEE_RISE_DENOMINATOR**blocks
        return replace(
            self,
            l1_base_fee=-(-self.l1_base_fee * num // den),
            blob_base_fee=-(-self.blob_base_fee * num // den),
        )


def reading(block: str = "latest") -> list[Call]:
    """The inputs' reads at `block`, in DataFee's order: one batch's calls."""
    return [_call(to, _selector(signature), block) for to, signature in _INPUTS]


def read(answers: list[Answer]) -> DataFee:
    """The inputs from their reads' answers (reading's order)."""
    return DataFee(*(int(a.value(), 16) for a in answers))


def checking(block: str) -> list[Call]:
    """The oracle's own answers at `block` to check the formulas against: getL1FeeUpperBound
    at each of CHECKED_SIZES, then getOperatorFee at each of CHECKED_GAS. Read at the same
    block as the inputs (reading): a new block between them would change the inputs."""
    upper = _selector("getL1FeeUpperBound(uint256)")
    operator = _selector("getOperatorFee(uint256)")
    return [_call(GAS_PRICE_ORACLE, upper + _word(n), block) for n in CHECKED_SIZES] + [
        _call(GAS_PRICE_ORACLE, operator + _word(g), block) for g in CHECKED_GAS
    ]


def agrees(fee: DataFee, answers: list[Answer]) -> bool:
    """Whether the oracle's answers (checking's order, read with `fee`'s inputs) are what the
    formulas give."""
    expected = [fee.l1_fee(n) for n in CHECKED_SIZES] + [fee.operator_fee(g) for g in CHECKED_GAS]
    return [int(a.value(), 16) for a in answers] == expected
