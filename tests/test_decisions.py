from pydantic import TypeAdapter, ValidationError

from cua.models.actions import Decision


decision_adapter = TypeAdapter(Decision)


def test_decision_union_rejects_undeclared_or_extra_actions():
    valid = {
        "operation": "CLICK",
        "observation_id": "obs_1",
        "control_ref": "c_3",
        "reason_code": "NAVIGATE",
        "rationale": "Open the observed account overview.",
    }
    assert decision_adapter.validate_python(valid).operation == "CLICK"

    with pytest.raises(ValidationError):
        decision_adapter.validate_python({**valid, "operation": "TRANSFER"})

    with pytest.raises(ValidationError):
        decision_adapter.validate_python(
            {
                "operation": "DONE",
                "observation_id": "obs_1",
                "control_ref": "c_3",
                "reason_code": "DONE",
                "rationale": "Claim completion.",
            }
        )


def test_type_text_decision_carries_only_a_value_reference():
    decision = decision_adapter.validate_python(
        {
            "operation": "TYPE_TEXT",
            "observation_id": "obs_2",
            "control_ref": "c_1",
            "value_ref": "inputs.account_id",
            "reason_code": "FILL_FILTER",
            "rationale": "Use the bound account input.",
        }
    )
    assert decision.value_ref == "inputs.account_id"

    with pytest.raises(ValidationError):
        decision_adapter.validate_python(
            {
                "operation": "TYPE_TEXT",
                "observation_id": "obs_2",
                "control_ref": "c_1",
                "value_ref": "inputs.account_id",
                "text": "12345",
                "reason_code": "FILL_FILTER",
                "rationale": "Use a raw customer value.",
            }
        )


import pytest
