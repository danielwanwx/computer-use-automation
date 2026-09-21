import pytest

from cua.discovery.goals import GoalBindError, GoalBinder


def test_chinese_read_goal_binds_account_and_emits_only_safe_canonical_intent():
    bound = GoalBinder().bind("查询我的储蓄账户 12345 的可用余额")

    assert bound.code == "get_savings_balance"
    assert bound.safe_goal == "Get the available balance for the requested savings account."
    assert bound.requested_account_id.get_secret_value() == "12345"
    assert bound.input_bindings["inputs.account_id"].get_secret_value() == "12345"
    assert "12345" not in repr(bound)


@pytest.mark.parametrize(
    ("goal", "inputs", "code"),
    [
        ("Get my savings account's available balance", None, "INPUT_INVALID"),
        ("Get savings balance for 12345 or 54321", None, "INPUT_CONFLICT"),
        ("Get savings balance for account 12345, amount 12345", None, "INPUT_CONFLICT"),
        ("Get savings balance for account 12345", {"account_id": "54321"}, "INPUT_CONFLICT"),
        ("Transfer savings funds for account 12345", None, "UNSUPPORTED_GOAL"),
        ("查询储蓄账户 12345 的可用余额并转账", None, "UNSUPPORTED_GOAL"),
        ("检查 savings balance for id_12345", None, "INPUT_INVALID"),
    ],
)
def test_goal_binder_rejects_unsupported_or_ambiguous_requests(goal, inputs, code):
    with pytest.raises(GoalBindError) as raised:
        GoalBinder().bind(goal, inputs)

    assert raised.value.code == code


def test_matching_text_and_explicit_account_binding_is_allowed():
    bound = GoalBinder().bind(
        "Get the available savings balance for account 12345",
        {"inputs.account_id": "12345"},
    )

    assert bound.requested_account_id.get_secret_value() == "12345"
