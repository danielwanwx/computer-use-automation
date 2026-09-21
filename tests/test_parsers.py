from decimal import Decimal

import pytest

from cua.conditions.parsers import AmountParseError, USD_DECIMAL_V1, parse_unique_usd_amount


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,234.50", Decimal("1234.50")),
        ("-$250.00", Decimal("-250.00")),
        ("$-0.75", Decimal("-0.75")),
        ("$0.00", Decimal("0.00")),
    ],
)
def test_usd_decimal_parser_returns_exact_decimal(raw, expected):
    assert USD_DECIMAL_V1.parse(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "$1,23.00",
        "$1.0",
        "$NaN",
        "$Infinity",
        "USD 10.00",
        "€10.00",
        "$10.00 and $11.00",
        "--$1.00",
        "$--1.00",
        "",
    ],
)
def test_usd_decimal_parser_rejects_invalid_or_ambiguous_text(raw):
    with pytest.raises(AmountParseError):
        USD_DECIMAL_V1.parse(raw)


def test_parser_rejects_multiple_visible_amount_candidates():
    with pytest.raises(AmountParseError) as exc_info:
        parse_unique_usd_amount(("$10.00", "$20.00"))

    assert exc_info.value.code == "AMBIGUOUS_AMOUNT"
