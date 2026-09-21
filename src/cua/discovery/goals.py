"""Bind the supported natural-language intent locally before UI/model access."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Mapping
import unicodedata

from pydantic import SecretStr


_ACCOUNT_ID = re.compile(r"(?<![A-Za-z0-9_])([0-9]{1,20})(?![A-Za-z0-9_])", re.ASCII)
_ACCOUNT_ID_ONLY = re.compile(r"^[0-9]{1,20}$", re.ASCII)
_WRITE_INTENT = re.compile(
    r"\b(?:transfer|pay|payment|withdraw|deposit|send|move|close|change|update)\b",
    re.IGNORECASE | re.ASCII,
)
_CHINESE_WRITE_INTENT = re.compile(
    r"(?:转账|付款|支付|存款|取款|提款|转出|转入|汇款|缴费|修改|更改|关闭|删除|开户)",
)


class GoalBindError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BoundIntent:
    """A canonical safe intent plus one local-only input binding."""

    code: str
    safe_goal: str
    requested_account_id: SecretStr = field(repr=False)

    @property
    def input_bindings(self) -> Mapping[str, SecretStr]:
        return MappingProxyType({"inputs.account_id": self.requested_account_id})

    def __repr__(self) -> str:
        return "BoundIntent(code='get_savings_balance', requested_account='<requested_account>')"


class GoalBinder:
    """Accept only one unambiguous savings-balance intent and account ID."""

    def bind(
        self,
        goal: str,
        supplied_inputs: Mapping[str, SecretStr | str] | None = None,
    ) -> BoundIntent:
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2_000:
            raise GoalBindError("INPUT_INVALID")
        normalized = unicodedata.normalize("NFKC", goal).casefold()
        words = set(re.findall(r"[a-z]+", normalized, re.ASCII))
        english_savings_balance = (
            ("savings" in words or "saving" in words)
            and ("balance" in words or "available" in words)
        )
        chinese_savings_balance = "储蓄" in normalized and "余额" in normalized
        if (
            not (english_savings_balance or chinese_savings_balance)
            or _WRITE_INTENT.search(normalized)
            or _CHINESE_WRITE_INTENT.search(normalized)
        ):
            raise GoalBindError("UNSUPPORTED_GOAL")

        textual_ids = _ACCOUNT_ID.findall(normalized)
        if len(textual_ids) > 1:
            raise GoalBindError("INPUT_CONFLICT")
        textual_id = textual_ids[0] if textual_ids else None
        supplied = _normalize_supplied_inputs(supplied_inputs)
        if len(supplied) > 1:
            raise GoalBindError("INPUT_CONFLICT")
        supplied_id = next(iter(supplied), None)
        if textual_id is not None and supplied_id is not None and textual_id != supplied_id:
            raise GoalBindError("INPUT_CONFLICT")
        account_id = supplied_id or textual_id
        if account_id is None or not _ACCOUNT_ID_ONLY.fullmatch(account_id):
            raise GoalBindError("INPUT_INVALID")

        return BoundIntent(
            code="get_savings_balance",
            safe_goal="Get the available balance for the requested savings account.",
            requested_account_id=SecretStr(account_id),
        )


def _normalize_supplied_inputs(
    supplied: Mapping[str, SecretStr | str] | None,
) -> set[str]:
    if supplied is None:
        return set()
    if not isinstance(supplied, Mapping) or any(
        key not in {"account_id", "inputs.account_id"} for key in supplied
    ):
        raise GoalBindError("INPUT_INVALID")
    values: set[str] = set()
    for value in supplied.values():
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or not _ACCOUNT_ID_ONLY.fullmatch(raw):
            raise GoalBindError("INPUT_INVALID")
        values.add(raw)
    return values
