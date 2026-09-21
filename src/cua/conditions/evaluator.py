from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from pydantic import SecretStr

from cua.conditions.parsers import AmountParseError, USDDecimalParser
from cua.models.conditions import (
    AccountPresentCondition,
    AllCondition,
    AnyCondition,
    ConditionExpr,
    ConditionReference,
    ConditionExpr,
    ExplicitDenialCondition,
    FieldEqualsCondition,
    MembershipValidCondition,
    NotCondition,
    OutputValidCondition,
    OverviewCompleteCondition,
    PageStateCondition,
    ParseableCondition,
    PrincipalMatchesCondition,
)


class TriState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ConditionResult:
    status: TriState
    reason_code: str


@dataclass(frozen=True, slots=True)
class ConditionContext:
    # Ephemeral page values and bindings stay in memory and never enter the registry.
    page_state: str | None = None
    principal_matches: bool | None = None
    overview_complete: bool | None = None
    account_presence: Mapping[str, bool | None] = field(default_factory=dict)
    membership_validity: Mapping[str, bool | None] = field(default_factory=dict)
    denial_signals: Mapping[str, bool | None] = field(default_factory=dict)
    field_values: Mapping[str, tuple[SecretStr, ...]] = field(default_factory=dict, repr=False)
    input_values: Mapping[str, SecretStr] = field(default_factory=dict, repr=False)
    output_values: Mapping[str, SecretStr] = field(default_factory=dict, repr=False)
    valid_output_refs: frozenset[str] = frozenset()


class ConditionEvaluator:
    def __init__(
        self,
        parsers: Mapping[tuple[str, str], USDDecimalParser] | None = None,
        condition_definitions: Mapping[str, ConditionExpr] | None = None,
    ) -> None:
        self._parsers = dict(parsers or {(USDDecimalParser.parser_id, USDDecimalParser.version): USDDecimalParser()})
        self._conditions = dict(condition_definitions or {})

    def evaluate(self, condition: ConditionExpr, context: ConditionContext) -> ConditionResult:
        return self._evaluate(condition, context, ())

    def _evaluate(
        self,
        condition: ConditionExpr,
        context: ConditionContext,
        condition_stack: tuple[str, ...],
    ) -> ConditionResult:
        if isinstance(condition, NotCondition):
            result = self._evaluate(condition.child, context, condition_stack)
            if result.status is TriState.UNKNOWN:
                return result
            status = TriState.FAIL if result.status is TriState.PASS else TriState.PASS
            return ConditionResult(status, _reason(status))

        if isinstance(condition, AllCondition):
            return _combine_all(
                self._evaluate(child, context, condition_stack)
                for child in condition.conditions
            )

        if isinstance(condition, AnyCondition):
            return _combine_any(
                self._evaluate(child, context, condition_stack)
                for child in condition.conditions
            )

        if isinstance(condition, ConditionReference):
            if condition.name in condition_stack:
                return ConditionResult(TriState.UNKNOWN, "CONDITION_REFERENCE_CYCLE")
            expression = self._conditions.get(condition.name)
            if expression is None:
                return ConditionResult(TriState.UNKNOWN, "UNRESOLVED_CONDITION")
            return self._evaluate(
                expression,
                context,
                (*condition_stack, condition.name),
            )

        if isinstance(condition, PageStateCondition):
            if context.page_state is None:
                return ConditionResult(TriState.UNKNOWN, "PAGE_STATE_UNKNOWN")
            status = TriState.PASS if context.page_state == condition.value else TriState.FAIL
            return ConditionResult(status, _reason(status))

        if isinstance(condition, PrincipalMatchesCondition):
            return _boolean_result(context.principal_matches, "PRINCIPAL_UNKNOWN")

        if isinstance(condition, OverviewCompleteCondition):
            return _boolean_result(context.overview_complete, "OVERVIEW_UNKNOWN")

        if isinstance(condition, AccountPresentCondition):
            if context.overview_complete is not True:
                reason = (
                    "OVERVIEW_INCOMPLETE"
                    if context.overview_complete is False
                    else "OVERVIEW_UNKNOWN"
                )
                return ConditionResult(TriState.UNKNOWN, reason)
            return _mapping_result(
                context.account_presence.get(condition.input_ref),
                "ACCOUNT_PRESENCE_UNKNOWN",
            )

        if isinstance(condition, MembershipValidCondition):
            return _mapping_result(context.membership_validity.get(condition.input_ref), "MEMBERSHIP_UNKNOWN")

        if isinstance(condition, FieldEqualsCondition):
            values = context.field_values.get(condition.target_ref)
            if values is None or len(values) == 0:
                return ConditionResult(TriState.UNKNOWN, "FIELD_UNKNOWN")
            if len(values) != 1:
                return ConditionResult(TriState.FAIL, "TARGET_AMBIGUOUS")
            if condition.expected.kind == "input_ref":
                expected = context.input_values.get(condition.expected.name)
                if expected is None:
                    return ConditionResult(TriState.UNKNOWN, "INPUT_UNKNOWN")
                expected_value = expected.get_secret_value()
            else:
                expected_value = condition.expected.value
            status = TriState.PASS if values[0].get_secret_value() == expected_value else TriState.FAIL
            return ConditionResult(status, _reason(status))

        if isinstance(condition, ParseableCondition):
            values = context.field_values.get(condition.target_ref)
            if values is None or len(values) == 0:
                return ConditionResult(TriState.UNKNOWN, "FIELD_UNKNOWN")
            if len(values) != 1:
                return ConditionResult(TriState.FAIL, "TARGET_AMBIGUOUS")
            parser = self._parsers.get((condition.parser_id, condition.parser_version))
            if parser is None:
                return ConditionResult(TriState.FAIL, "UNKNOWN_PARSER")
            try:
                parser.parse(values[0].get_secret_value())
            except AmountParseError as error:
                return ConditionResult(TriState.FAIL, error.code)
            return ConditionResult(TriState.PASS, "PARSEABLE")

        if isinstance(condition, OutputValidCondition):
            if condition.output_ref not in context.output_values:
                return ConditionResult(TriState.UNKNOWN, "OUTPUT_UNKNOWN")
            status = (
                TriState.PASS
                if condition.output_ref in context.valid_output_refs
                else TriState.FAIL
            )
            return ConditionResult(status, _reason(status))

        if isinstance(condition, ExplicitDenialCondition):
            return _mapping_result(context.denial_signals.get(condition.signal_ref), "DENIAL_UNKNOWN")

        return ConditionResult(TriState.UNKNOWN, "UNKNOWN_CONDITION")


def _boolean_result(value: bool | None, unknown_code: str) -> ConditionResult:
    return _mapping_result(value, unknown_code)


def _mapping_result(value: bool | None, unknown_code: str) -> ConditionResult:
    if value is None:
        return ConditionResult(TriState.UNKNOWN, unknown_code)
    status = TriState.PASS if value else TriState.FAIL
    return ConditionResult(status, _reason(status))


def _reason(status: TriState) -> str:
    return {
        TriState.PASS: "CONDITION_PASSED",
        TriState.FAIL: "CONDITION_FAILED",
        TriState.UNKNOWN: "CONDITION_UNKNOWN",
    }[status]


def _combine_all(results) -> ConditionResult:
    collected = tuple(results)
    if any(result.status is TriState.FAIL for result in collected):
        return ConditionResult(TriState.FAIL, _significant_failure_reason(collected))
    if any(result.status is TriState.UNKNOWN for result in collected):
        return ConditionResult(TriState.UNKNOWN, "CONDITION_UNKNOWN")
    return ConditionResult(TriState.PASS, "CONDITION_PASSED")


def _combine_any(results) -> ConditionResult:
    collected = tuple(results)
    if any(result.status is TriState.PASS for result in collected):
        return ConditionResult(TriState.PASS, "CONDITION_PASSED")
    if any(result.status is TriState.UNKNOWN for result in collected):
        return ConditionResult(TriState.UNKNOWN, "CONDITION_UNKNOWN")
    return ConditionResult(TriState.FAIL, _significant_failure_reason(collected))


def _significant_failure_reason(results: tuple[ConditionResult, ...]) -> str:
    failures = tuple(result for result in results if result.status is TriState.FAIL)
    priority = {
        "TARGET_AMBIGUOUS": 0,
        "SUBJECT_MISMATCH": 1,
        "ACCOUNT_TYPE_MISMATCH": 2,
        "ACCESS_DENIED": 3,
        "INVALID_AMOUNT": 4,
        "UNKNOWN_PARSER": 5,
    }
    return min(
        enumerate(failures),
        key=lambda item: (priority.get(item[1].reason_code, 99), item[0]),
    )[1].reason_code
