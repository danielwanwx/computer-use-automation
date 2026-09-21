"""Independent test oracle for results from the ParaBank UI runtime.

This module is testbed-only. The application runtime must never import it.
ParaBank's pinned overview.jsp and activity.jsp render available balance as
max(account.balance, 0), independently of the runtime's UI parser.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any, Mapping


_CENT = Decimal("0.01")
_ZERO = Decimal("0")
_AVAILABLE_MONEY = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]{2}$")


class EvaluationError(AssertionError):
    """A result failed the independent backend comparison."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _decimal(value: Any, reason_code: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise EvaluationError(reason_code)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise EvaluationError(reason_code) from None
    if not parsed.is_finite():
        raise EvaluationError(reason_code)
    return parsed


def expected_available_balance(ledger_balance: Any) -> Decimal:
    """Apply ParaBank's pinned UI rule, then format to cents."""
    balance = _decimal(ledger_balance, "BACKEND_BALANCE_INVALID")
    available = max(balance, _ZERO)
    try:
        return available.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise EvaluationError("BACKEND_BALANCE_INVALID") from None


def assert_result_matches_backend(
    result: Mapping[str, Any],
    *,
    requested_account_id: str,
    backend_account: Mapping[str, Any],
) -> None:
    """Raise EvaluationError unless SUCCESS matches the independent account truth."""
    if not isinstance(result, Mapping) or result.get("status") != "SUCCESS":
        raise EvaluationError("RESULT_NOT_SUCCESS")
    outputs = result.get("outputs")
    if not isinstance(outputs, Mapping):
        raise EvaluationError("OUTPUTS_INVALID")

    backend_id = backend_account.get("id")
    if backend_id is None or str(backend_id) != str(requested_account_id):
        raise EvaluationError("BACKEND_ACCOUNT_MISMATCH")
    if backend_account.get("type") != "SAVINGS":
        raise EvaluationError("ACCOUNT_TYPE_MISMATCH")
    if outputs.get("currency") != "USD":
        raise EvaluationError("CURRENCY_MISMATCH")

    actual_text = outputs.get("available_balance")
    if not isinstance(actual_text, str) or not _AVAILABLE_MONEY.fullmatch(actual_text):
        raise EvaluationError("OUTPUT_BALANCE_INVALID")
    actual = _decimal(actual_text, "OUTPUT_BALANCE_INVALID")
    expected = expected_available_balance(backend_account.get("balance"))
    if actual != expected:
        raise EvaluationError("BALANCE_MISMATCH")
