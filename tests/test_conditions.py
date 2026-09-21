import pytest
from pydantic import SecretStr

from cua.conditions import ConditionContext, ConditionEvaluator, TriState
from cua.models.conditions import (
    AccountPresentCondition,
    AllCondition,
    AnyCondition,
    ApprovedConstant,
    ConditionReference,
    FieldEqualsCondition,
    MembershipValidCondition,
    NotCondition,
    PageStateCondition,
)


def _membership_unknown():
    return MembershipValidCondition(kind="membership_valid", input_ref="inputs.account_id")


def _account_present():
    return AccountPresentCondition(kind="account_present", input_ref="inputs.account_id")


def test_not_unknown_remains_unknown():
    condition = NotCondition(kind="not", child=_membership_unknown())

    result = ConditionEvaluator().evaluate(condition, ConditionContext())

    assert result.status is TriState.UNKNOWN
    assert result.reason_code == "MEMBERSHIP_UNKNOWN"


@pytest.mark.parametrize(
    ("kind", "account_present", "expected"),
    [
        ("all", False, TriState.FAIL),
        ("any", True, TriState.PASS),
    ],
)
def test_composite_conditions_use_strong_kleene_logic(kind, account_present, expected):
    children = (_account_present(), _membership_unknown())
    condition = (
        AllCondition(kind="all", conditions=children)
        if kind == "all"
        else AnyCondition(kind="any", conditions=children)
    )
    context = ConditionContext(
        overview_complete=True,
        account_presence={"inputs.account_id": account_present},
    )

    assert ConditionEvaluator().evaluate(condition, context).status is expected


@pytest.mark.parametrize(
    ("overview_complete", "account_present"),
    [
        (False, False),
        (False, True),
        (None, False),
        (None, True),
    ],
)
def test_account_presence_is_unknown_until_overview_is_complete(
    overview_complete, account_present
):
    context = ConditionContext(
        overview_complete=overview_complete,
        account_presence={"inputs.account_id": account_present},
    )

    result = ConditionEvaluator().evaluate(_account_present(), context)

    assert result.status is TriState.UNKNOWN
    assert result.reason_code in {"OVERVIEW_INCOMPLETE", "OVERVIEW_UNKNOWN"}


def test_all_preserves_ambiguous_target_failure_reason():
    condition = AllCondition(
        kind="all",
        conditions=(
            PageStateCondition(kind="page_state", value="DETAIL_READY"),
            FieldEqualsCondition(
                kind="field_equals",
                target_ref="detail.account_type",
                expected=ApprovedConstant(kind="constant", value="SAVINGS"),
            ),
        ),
    )
    context = ConditionContext(
        page_state="LOGIN",
        field_values={
            "detail.account_type": (SecretStr("SAVINGS"), SecretStr("CHECKING"))
        },
    )

    result = ConditionEvaluator().evaluate(condition, context)

    assert result.status is TriState.FAIL
    assert result.reason_code == "TARGET_AMBIGUOUS"


def test_named_condition_reference_evaluates_its_declared_expression():
    condition = ConditionReference(kind="condition_ref", name="overview_ready")
    evaluator = ConditionEvaluator(
        condition_definitions={
            "overview_ready": _overview_complete_condition(),
        }
    )

    result = evaluator.evaluate(condition, ConditionContext(overview_complete=True))

    assert result.status is TriState.PASS


def _overview_complete_condition():
    from cua.models.conditions import OverviewCompleteCondition

    return OverviewCompleteCondition(kind="overview_complete")
