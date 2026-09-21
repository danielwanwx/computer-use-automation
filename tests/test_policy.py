import pytest

from cua.policy import PolicyContext, PolicyEngine, Risk, RuntimeTargetClassification


def _context(*, artifact_risk="READ_ONLY"):
    return PolicyContext(
        deployment_origins=("http://127.0.0.1:8080",),
        capability_origins=("http://127.0.0.1:8080",),
        run_origins=("http://127.0.0.1:8080",),
        deployment_operations=("CLICK", "READ"),
        capability_operations=("CLICK", "READ"),
        run_operations=("CLICK", "READ"),
        deployment_routes=("account_overview", "account_detail"),
        capability_routes=("account_overview", "account_detail"),
        run_routes=("account_overview", "account_detail"),
        artifact_risk_label=Risk(artifact_risk),
    )


@pytest.mark.parametrize("actual_risk", [Risk.WRITE, Risk.UNKNOWN])
def test_runtime_reclassification_denies_write_or_unknown_even_if_artifact_claims_read_only(actual_risk):
    classification = RuntimeTargetClassification(
        origin="http://127.0.0.1:8080",
        route="account_overview",
        operation="CLICK",
        risk=actual_risk,
        control_ref="c_1",
        control_operations=("CLICK",),
    )

    authorization = PolicyEngine().authorize(_context(), classification)

    assert authorization.allowed is False
    assert authorization.reason_code == "RISK_NOT_READ_ONLY"


def test_safe_action_must_be_in_deployment_capability_and_run_intersection():
    target = RuntimeTargetClassification(
        origin="http://127.0.0.1:8080",
        route="account_overview",
        operation="CLICK",
        risk=Risk.READ_ONLY,
        control_ref="c_1",
        control_operations=("CLICK",),
    )

    denied = PolicyEngine().authorize(
        _context(),
        target.model_copy(update={"operation": "TRANSFER", "risk": Risk.WRITE}),
    )
    assert denied.allowed is False

    permitted = PolicyEngine().authorize(_context(), target)
    assert permitted.allowed is True
    assert permitted.reason_code == "AUTHORIZED"


def test_origin_and_route_are_independently_bound():
    target = RuntimeTargetClassification(
        origin="http://evil.example",
        route="unknown_route",
        operation="READ",
        risk=Risk.READ_ONLY,
        control_ref=None,
        control_operations=("READ",),
    )

    authorization = PolicyEngine().authorize(_context(), target)

    assert authorization.allowed is False
    assert authorization.reason_code in {"ORIGIN_NOT_ALLOWED", "ROUTE_NOT_ALLOWED"}


@pytest.mark.parametrize(
    ("target_updates", "context_updates", "expected_reason"),
    [
        ({"origin": "http://evil.example"}, {}, "ORIGIN_NOT_ALLOWED"),
        ({"route": "unknown_route"}, {}, "ROUTE_NOT_ALLOWED"),
        ({}, {"run_operations": ("READ",)}, "OPERATION_NOT_ALLOWED"),
        ({"control_operations": ("READ",)}, {}, "CONTROL_OPERATION_NOT_ALLOWED"),
        ({"operation": "TRANSFER"}, {}, "INVALID_RUNTIME_CLASSIFICATION"),
    ],
)
def test_authorization_checks_each_runtime_intersection(
    target_updates, context_updates, expected_reason
):
    target = RuntimeTargetClassification(
        origin="http://127.0.0.1:8080",
        route="account_overview",
        operation="CLICK",
        risk=Risk.READ_ONLY,
        control_ref="c_1",
        control_operations=("CLICK",),
    ).model_copy(update=target_updates)
    context = _context().model_copy(update=context_updates)

    authorization = PolicyEngine().authorize(context, target)

    assert authorization.allowed is False
    assert authorization.reason_code == expected_reason
