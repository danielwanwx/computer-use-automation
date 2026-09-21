import re
from decimal import Decimal
from enum import StrEnum
from typing import Sequence


class AmountParseCode(StrEnum):
    INVALID_AMOUNT = "INVALID_AMOUNT"
    AMBIGUOUS_AMOUNT = "AMBIGUOUS_AMOUNT"


class AmountParseError(ValueError):
    def __init__(self, code: AmountParseCode) -> None:
        self.code = code.value
        super().__init__(self.code)


_AMOUNT = re.compile(
    r"^(?P<lead_sign>-)?\$(?P<inner_sign>-)?"
    r"(?P<integer>(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+))"
    r"\.(?P<fraction>[0-9]{2})$",
    re.ASCII,
)


class USDDecimalParser:
    parser_id = "USD_DECIMAL_V1"
    version = "1"

    def parse(self, raw: str) -> Decimal:
        if not isinstance(raw, str) or len(raw) > 32:
            raise AmountParseError(AmountParseCode.INVALID_AMOUNT)
        match = _AMOUNT.fullmatch(raw.strip())
        if match is None or (match.group("lead_sign") and match.group("inner_sign")):
            raise AmountParseError(AmountParseCode.INVALID_AMOUNT)

        integer = match.group("integer").replace(",", "")
        fraction = match.group("fraction")
        sign = "-" if match.group("lead_sign") or match.group("inner_sign") else ""
        return Decimal(f"{sign}{integer}.{fraction}")

    def format(self, amount: Decimal) -> str:
        if not amount.is_finite():
            raise AmountParseError(AmountParseCode.INVALID_AMOUNT)
        return format(amount, ".2f")


USD_DECIMAL_V1 = USDDecimalParser()


def parse_unique_usd_amount(values: Sequence[str]) -> Decimal:
    if len(values) != 1:
        raise AmountParseError(AmountParseCode.AMBIGUOUS_AMOUNT)
    return USD_DECIMAL_V1.parse(values[0])
