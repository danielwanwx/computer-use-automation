from dataclasses import FrozenInstanceError

import pytest
from pydantic import SecretStr

from cua.evidence import EvidenceRef, SafeReasonCode
from cua.execution import (
    EffectState,
    FailureDetail,
    InvocationResult,
    InvocationStatus,
)


def test_success_result_is_memory_only_and_redacts_output_values():
    result = InvocationResult(
        run_id="run_0123456789abcdef",
        status=InvocationStatus.SUCCESS,
        outputs={
            "available_balance": SecretStr("250.00"),
            "currency": SecretStr("USD"),
        },
        code=SafeReasonCode.REPLAY_COMPLETE,
        evidence_refs=(
            EvidenceRef(
                snapshot_id="snap_0123456789ab",
                relative_path="safe_snapshots/snap_0123456789ab.json",
            ),
        ),
    )

    assert result.outputs["available_balance"].get_secret_value() == "250.00"
    assert "250.00" not in repr(result)
    with pytest.raises(TypeError):
        result.outputs["currency"] = SecretStr("EUR")
    with pytest.raises(FrozenInstanceError):
        result.status = InvocationStatus.FAILURE


def test_success_result_does_not_require_a_replay_specific_code():
    result = InvocationResult(
        run_id="run_0123456789abcdef",
        status=InvocationStatus.SUCCESS,
        outputs={"currency": SecretStr("USD")},
    )

    assert result.code is None
    assert result.outputs["currency"].get_secret_value() == "USD"


def test_non_success_cannot_expose_outputs():
    with pytest.raises(ValueError, match="only successful results"):
        InvocationResult(
            run_id="run_0123456789abcdef",
            status=InvocationStatus.FAILURE,
            outputs={"available_balance": SecretStr("250.00")},
            code=SafeReasonCode.REPLAY_FAILED,
            failure=FailureDetail(
                reason_code=SafeReasonCode.POSTCONDITION_UNKNOWN,
                step_id="read_balance",
                effect_state=EffectState.OUTCOME_UNKNOWN,
            ),
        )


def test_failure_requires_safe_failure_detail():
    with pytest.raises(ValueError, match="failure detail"):
        InvocationResult(
            run_id="run_0123456789abcdef",
            status=InvocationStatus.FAILURE,
            code=SafeReasonCode.REPLAY_FAILED,
        )


def test_input_failure_has_one_canonical_wire_code():
    assert SafeReasonCode.INVALID_INPUT.value == "INPUT_INVALID"
    assert SafeReasonCode("INPUT_INVALID") is SafeReasonCode.INVALID_INPUT
    with pytest.raises(ValueError):
        SafeReasonCode("INVALID_INPUT")
