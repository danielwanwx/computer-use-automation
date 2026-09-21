import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from pydantic import SecretStr, ValidationError

from cua.conditions.parsers import AmountParseError, USDDecimalParser
from cua.models.bundles import CapabilityContract
from cua.models.verification import CompletionContext, CompletionView


class VerificationStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    reason_code: str
    outputs: Mapping[str, SecretStr] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))


class CompletionVerifier:
    """Runtime-owned final verifier for the native read-only balance capability."""

    _account_id_pattern = re.compile(r"^[0-9]+$", re.ASCII)

    def __init__(self) -> None:
        self._usd_parser = USDDecimalParser()

    def verify(
        self,
        contract: CapabilityContract,
        view: CompletionView,
        context: CompletionContext,
    ) -> VerificationResult:
        """Return outputs only when a fresh view and current-run membership proof agree."""
        try:
            contract = CapabilityContract.model_validate(contract.model_dump(mode="python"))
            view = CompletionView.model_validate(view.model_dump(mode="python"))
            context = CompletionContext.model_validate(context.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            return _result(VerificationStatus.FAILED, "INVALID_EVIDENCE")

        if not _supports_balance_contract(contract):
            return _result(VerificationStatus.FAILED, "UNSUPPORTED_CONTRACT")

        proof = context.membership_proof
        if (
            proof.run_ref != context.run_ref
            or proof.session_ref != context.session_ref
            or proof.authentication_generation != context.authentication_generation
            or proof.account_binding_ref != context.account_binding_ref
            or proof.overview_complete is not True
            or proof.account_present is not True
            or proof.account_binding_value.get_secret_value()
            != context.requested_account_id.get_secret_value()
        ):
            return _result(VerificationStatus.FAILED, "MEMBERSHIP_PROOF_INVALID")

        if view.session_ref != context.session_ref:
            return _result(VerificationStatus.FAILED, "SESSION_MISMATCH")
        if view.run_ref != context.run_ref:
            return _result(VerificationStatus.FAILED, "RUN_MISMATCH")
        if view.authentication_generation != context.authentication_generation:
            return _result(VerificationStatus.FAILED, "AUTHENTICATION_CHANGED")
        if view.origin != context.target_origin:
            return _result(VerificationStatus.FAILED, "ORIGIN_MISMATCH")
        if view.profile_id != context.approved_profile:
            return _result(VerificationStatus.FAILED, "PROFILE_MISMATCH")
        if view.page_state != "DETAIL_READY":
            return _result(VerificationStatus.UNKNOWN, "DETAIL_NOT_READY")
        if view.principal_matches is None:
            return _result(VerificationStatus.UNKNOWN, "PRINCIPAL_UNKNOWN")
        if not view.principal_matches:
            return _result(VerificationStatus.FAILED, "SUBJECT_MISMATCH")

        requested_account_id = context.requested_account_id.get_secret_value()
        if not self._account_id_pattern.fullmatch(requested_account_id):
            return _result(VerificationStatus.FAILED, "INPUT_INVALID")

        account_number = _single_value(view.account_number_values)
        if isinstance(account_number, VerificationResult):
            return account_number
        if account_number.get_secret_value() != requested_account_id:
            return _result(VerificationStatus.FAILED, "SUBJECT_MISMATCH")

        account_type = _single_value(view.account_type_values)
        if isinstance(account_type, VerificationResult):
            return account_type
        if account_type.get_secret_value().strip() != "SAVINGS":
            return _result(VerificationStatus.FAILED, "ACCOUNT_TYPE_MISMATCH")

        balance_text = _single_value(view.available_balance_values)
        if isinstance(balance_text, VerificationResult):
            return balance_text
        try:
            balance = self._usd_parser.parse(balance_text.get_secret_value())
        except AmountParseError as error:
            return _result(VerificationStatus.FAILED, error.code)

        return VerificationResult(
            status=VerificationStatus.SUCCESS,
            reason_code="VERIFIED",
            outputs={
                "available_balance": SecretStr(self._usd_parser.format(balance)),
                "currency": SecretStr("USD"),
            },
        )


def _supports_balance_contract(contract: CapabilityContract) -> bool:
    account_inputs = tuple(item for item in contract.inputs if item.name == "account_id")
    outputs = {item.name: item for item in contract.outputs}
    return (
        len(account_inputs) == 1
        and account_inputs[0].value_type == "string"
        and account_inputs[0].pattern == r"^[0-9]+$"
        and account_inputs[0].sensitive
        and set(outputs) == {"available_balance", "currency"}
        and outputs["available_balance"].value_type == "decimal_string"
        and outputs["available_balance"].sensitive
        and outputs["currency"].value_type == "string"
        and outputs["currency"].enum == ("USD",)
    )


def _single_value(values: tuple[SecretStr, ...]) -> SecretStr | VerificationResult:
    if not values:
        return _result(VerificationStatus.UNKNOWN, "FIELD_UNKNOWN")
    if len(values) != 1:
        return _result(VerificationStatus.FAILED, "TARGET_AMBIGUOUS")
    return values[0]


def _result(status: VerificationStatus, reason_code: str) -> VerificationResult:
    return VerificationResult(status=status, reason_code=reason_code)
