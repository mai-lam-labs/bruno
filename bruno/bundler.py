"""The relay as an ERC-4337 bundler (ERC-7769): a UserOp estimated, priced, sent as a
submission whose call is the EntryPoint's handleOps of it, and its receipt read. A Bundler
holds its relay and keeps nothing of its own: an op it takes is the relay's submission, tracked
there by its hash. What an op is, and how it's simulated and read, is userops'."""

from dataclasses import replace
from fractions import Fraction
from math import ceil

from eth_typing import HexStr
from eth_utils.address import to_checksum_address

from .chain import fallen
from .evm import NATIVE_ASSET, EncodedCall
from .net.jsonrpc import REVERTED
from .plan import SimulationFailedError, runner_plan
from .relay import MAX_AMOUNT, Relay, UnsupportedError, fee_ceiling, schedule_of
from .runner import Payment, in_call_runner
from .submission import InvalidSubmissionError, Submission, authorities, check_signed
from .tracked import Finished, Status
from .userops import (
    FeePayment,
    GasEstimate,
    InvalidUserOperationError,
    UserOperation,
    UserOperationReceipt,
    delegate_of,
    estimated,
    estimating,
    fees_paid,
    gas_estimate,
    handle_ops,
    paid,
    receipt_of,
    refusal,
    sending,
    simulation_calldata,
    used,
    user_operation_hash,
    with_overhead,
)


class Bundler:
    """`relay`, answering ERC-7769's methods and the gas price ones."""

    def __init__(self, relay: Relay) -> None:
        self._relay = relay

    async def estimate_user_operation(self, op: UserOperation) -> GasEstimate:
        """`op`'s gas limits, as ERC-7769's eth_estimateUserOperationGas answers them. Refused,
        reading nothing, as _user_operation_limits has it; an authorization it brings is taken
        as its account's delegation, its signature unchecked (ERC-7769). One read: its
        simulation, and with a paymaster its simulation without it (estimating: its
        paymaster's limits measured unless given); SimulationFailedError if one reverts (its
        validation failing for more than its stub signature, its account not deployed by its
        factory, or, paying by its fees, not paying its prefund). Refused if its limits are
        over the most gas a transaction may use. An op paying by its fees is charged, at the
        price its fees give at the least the base fee can fall to by the quote's last block, as
        much gas as a quote of its transaction (planned as any call's) comes to: the rest,
        beyond what it uses, is its preVerificationGas, whatever its fees (gas it's charged, not
        gas its transaction uses). Fees paying nothing a gas then are invalid params."""
        self._user_operation_limits(op)
        limit = self._relay.config.max_gas
        calls = estimating(op, limit)
        answers = await self._relay.chain.batch(calls)
        for answer in answers:
            if answer.error is not None:
                if answer.error.code != REVERTED:
                    raise answer.error
                revert = answer.error.data if isinstance(answer.error.data, str) else "0x"
                raise SimulationFailedError(bytes.fromhex(revert.removeprefix("0x")))
        results = [bytes.fromhex(answer.value().removeprefix("0x")) for answer in answers]
        gas = used(results, op, limit)
        estimate = estimated(gas, op)
        needs = estimate.gas
        if needs > limit:
            raise InvalidUserOperationError("gas", f"the operation needs {needs}, over {limit}")
        if not op.pays_by_fees:
            return estimate
        measured = await self._relay.available()
        self._relay.supported(measured, NATIVE_ASSET)
        paymaster = op.paymaster
        if paymaster is not None:
            paymaster = replace(
                paymaster,
                verification_gas_limit=estimate.paymaster_verification_gas_limit,
                post_op_gas_limit=estimate.paymaster_post_op_gas_limit,
            )
        sized = replace(
            op,
            verification_gas_limit=estimate.verification_gas_limit,
            call_gas_limit=estimate.call_gas_limit,
            paymaster=paymaster,
        )
        data = HexStr("0x" + handle_ops(sized, self._relay.payee).hex())
        call = EncodedCall(op.entry_point.address, data)
        schedule = schedule_of(measured)
        # the call's gas as it's sent: a node's estimate of the op's simulation, the
        # EntryPoint's own work in it, less what its transaction's intrinsic gas is; at least
        # its limits
        (sized_gas,) = await self._relay.chain.batch([gas_estimate(sized)])
        if sized_gas.error is not None:
            if sized_gas.error.code != REVERTED:
                raise sized_gas.error
            revert = sized_gas.error.data if isinstance(sized_gas.error.data, str) else "0x"
            raise SimulationFailedError(bytes.fromhex(revert.removeprefix("0x")))
        intrinsic, _ = schedule.intrinsic(simulation_calldata(sized, None))
        call_gas = max(needs, int(sized_gas.value(), 16) - intrinsic)
        payment = Payment(None, self._relay.payee, MAX_AMOUNT)
        runner = in_call_runner(call, payment, schedule, client_gas=call_gas)
        # an authorization's account, holding ETH to pay the op's fees, isn't empty
        authorizations = 0 if op.authorization is None else 1
        plan = runner_plan(
            self._relay.chain.chain_id, runner, call_gas, schedule, authorizations, 0
        )
        self._relay.fits(plan)
        latest = await self._relay.latest()
        fees = fee_ceiling(latest, self._relay.config.quote_blocks - 1)
        quoted = await self._relay.charge(measured, latest, NATIVE_ASSET, plan, fees)
        # its price at the least the base fee can be by the quote's last block: what it pays
        # by its fees can't then fall short of the quote, however the base fee falls before
        # it's sent (a fee that isn't flat pays by it)
        price = op.gas_price(fallen(latest.next_base_fee, self._relay.config.quote_blocks - 1))
        if price == 0:
            raise InvalidUserOperationError(
                "maxFeePerGas", "pays nothing a gas as the base fee may fall"
            )
        charged = ceil(Fraction(quoted, price))
        return with_overhead(estimate, gas, charged)

    async def user_operation_gas_price(self) -> int:
        """The gas price an op paying by its fees is asked, both its fees (EntryPoint v0.7 takes
        a max fee that's its priority fee too as its price, whatever the base fee): a gas as a
        quote charges it, its base fee risen and its tip doubled as by the quote's last block,
        and our fee a gas, in wei, ETH's margin included. UnsupportedError if the relay doesn't
        take ETH."""
        measured = await self._relay.available()
        self._relay.supported(measured, NATIVE_ASSET)
        latest = await self._relay.latest()
        fees = fee_ceiling(latest, self._relay.config.quote_blocks - 1)
        ours = max(self._relay.tip(latest), self._relay.config.min_fee_per_gas)
        return await self._relay.price(NATIVE_ASSET, fees.max_fee_per_gas + ours)

    async def send_user_operation(self, op: UserOperation) -> bytes:
        """Take `op` (signed) as a submission whose call is the EntryPoint's handleOps of it, the
        relay's payee its beneficiary: its hash (ERC-7769's eth_sendUserOperation). What it pays
        is found by simulating it, in the one batch that also estimates handleOps' gas: paying
        by its fees, the ETH its gas costs at its price in the next block; else the payee's
        balance in each token the relay takes, read before and after. Refused, reading nothing,
        as an estimate is, or if an authorization it brings isn't its account's for this chain;
        refused without its paymaster's limits, if it has one; refused if the account doesn't
        take its signature (or its paymaster its paymaster data's), or it pays nothing in a token the
        relay takes; then checked and taken as any submission (UnderpaidError:
        what's required), one paying by its fees for what it pays in the next block, its amount
        (what the runner checks) what it pays in any. The same op sent again is taken once."""
        self._user_operation_limits(op)
        self._authorized(op)
        if op.paymaster is not None:
            for field, value in (
                ("paymasterVerificationGasLimit", op.paymaster.verification_gas_limit),
                ("paymasterPostOpGasLimit", op.paymaster.post_op_gas_limit),
            ):
                if value is None:
                    raise InvalidUserOperationError(field, "none: a signed op gives its limits")
        op_hash = user_operation_hash(op, self._relay.chain.chain_id, await self._delegate(op))
        if self._relay.operation(op_hash) is not None:
            return op_hash
        tokens = list(self._relay.terms().tokens)
        before, ran, priced, estimate = await self._relay.chain.batch(
            sending(op, self._relay.payee, tokens)
        )
        for answer in (ran, priced, estimate):
            if answer.error is not None:
                if answer.error.code != REVERTED:
                    raise answer.error
                data = answer.error.data if isinstance(answer.error.data, str) else "0x"
                revert = bytes.fromhex(data.removeprefix("0x"))
                raise refusal(revert) or SimulationFailedError(revert)
        start = bytes.fromhex(before.value().removeprefix("0x"))
        after = bytes.fromhex(ran.value().removeprefix("0x"))
        payments = paid(start, after, tokens)
        latest = await self._relay.latest()
        fee_payment: FeePayment | None = None
        if op.pays_by_fees:
            if NATIVE_ASSET not in tokens:
                raise UnsupportedError("ETH isn't taken: an op paying by its fees pays in it")
            after_priced = bytes.fromhex(priced.value().removeprefix("0x"))
            runs = [
                (payments[NATIVE_ASSET], after),
                (paid(start, after_priced, tokens)[NATIVE_ASSET], after_priced),
            ]
            fee_payment = fees_paid(op, runs, len(tokens))
            token, amount = NATIVE_ASSET, fee_payment.at(0)
        else:
            paying = [(token, amount) for token, amount in payments.items() if amount]
            if not paying:
                raise InvalidUserOperationError(
                    "callData", "pays the relay's payee nothing, in any token it takes"
                )
            token, amount = paying[0]
        data = handle_ops(op, self._relay.payee)
        intrinsic, _ = schedule_of(await self._relay.available()).intrinsic(data)
        head = latest.number
        submission = Submission(
            call=EncodedCall(op.entry_point.address, HexStr("0x" + data.hex())),
            authorizations=() if op.authorization is None else (op.authorization,),
            gas=int(estimate.value(), 16) - intrinsic,
            token=to_checksum_address(token),
            payee=self._relay.payee,
            amount=amount,
            max_block_number=head + self._relay.config.max_deadline_blocks,
        )
        await self._relay.submit(submission, op_hash, fee_payment)
        return op_hash

    async def user_operation_receipt(self, op_hash: bytes) -> UserOperationReceipt | None:
        """ERC-7769's receipt of the UserOp `op_hash` this relay sent: None until it's included
        (or if it never is, or was forgotten); once it is, read from its transaction's receipt,
        nothing stored of it."""
        id_ = self._relay.operation(op_hash)
        if id_ is None:
            return None
        finished = self._relay.status(id_)
        if not isinstance(finished, Finished) or finished.transaction_hash is None:
            return None
        if finished.status is not Status.INCLUDED:
            return None
        receipt = await self._relay.chain.receipt(finished.transaction_hash)
        return None if receipt is None else receipt_of(op_hash, receipt)

    def _user_operation_limits(self, op: UserOperation) -> None:
        """What's refused of an op reading nothing: an EIP-7702 account's for EntryPoint v0.7,
        one with a paymaster paying nothing by its fees (what its paymaster pays the relay), or
        call or factory data longer than a call's may be."""
        if op.eip7702 and not op.entry_point.eip712:
            raise InvalidUserOperationError("factory", "0x7702 is EntryPoint v0.8's")
        if op.paymaster is not None and not op.pays_by_fees:
            raise InvalidUserOperationError(
                "maxFeePerGas", "zero: an op with a paymaster pays the relay by its fees"
            )
        most = self._relay.config.max_call_bytes
        for field, value in (("callData", op.call_data), ("factoryData", op.factory_data)):
            if len(value) > most:
                raise InvalidUserOperationError(field, f"over {most} bytes")

    def _authorized(self, op: UserOperation) -> None:
        """An authorization `op` brings signed by its account, for this chain or any: else
        InvalidUserOperationError."""
        auth = op.authorization
        if auth is None:
            return
        if auth.chain_id not in (0, self._relay.chain.chain_id):
            raise InvalidUserOperationError("eip7702Auth", f"chain id {auth.chain_id}")
        try:
            check_signed(auth.y_parity, auth.r, auth.s, auth.nonce)
            (authority,) = authorities([auth])
        except InvalidSubmissionError as e:
            raise InvalidUserOperationError("eip7702Auth", e.reason) from e
        if authority != op.sender:
            raise InvalidUserOperationError("eip7702Auth", "not signed by the sender")

    async def _delegate(self, op: UserOperation) -> bytes | None:
        """What an EIP-7702 account's `op` is hashed with: its authorization's address, else what
        its account's code delegates to (one read). InvalidUserOperationError if it delegates to
        nothing; None for any other account's op."""
        if not op.eip7702:
            return None
        if op.authorization is not None:
            return op.authorization.address
        code: str = await self._relay.chain.rpc("eth_getCode", [op.sender, "latest"])
        delegate = delegate_of(bytes.fromhex(code.removeprefix("0x")))
        if delegate is None:
            raise InvalidUserOperationError("eip7702Auth", "none, and the sender isn't delegated")
        return delegate
