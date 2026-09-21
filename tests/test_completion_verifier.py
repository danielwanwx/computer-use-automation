import pytest
from pydantic import SecretStr

from cua.models.bundles import (
    CapabilityContract,
    InputContract,
    OutputContract,
)
from cua.verification import (
    CompletionContext,
    CompletionVerifier,
    CompletionView,
    MembershipProof,
    VerificationStatus,
)


def _contract():
    return CapabilityContract(
        inputs=(
            InputContract(
                name="account_id",
                value_type="string",
                pattern=r"^[0-9]+$",
                sensitive=True,
            ),
        ),
        outputs=(
            OutputContract(
                name="available_balance",
                value_type="decimal_string",
                sensitive=True,
            ),
            OutputContract(name="currency", value_type="string", enum=("USD",)),
        ),
    )


def _context(*, requested_account_id="100001", proof_account_id="100001", **proof_updates):
    proof = MembershipProof(
        proof_ref="membership_1",
        run_ref="run_1",
        session_ref="session_1",
        authentication_generation=4,
        account_binding_ref="inputs.account_id",
        account_binding_value=SecretStr(proof_account_id),
        overview_observation_ref="overview_1",
        overview_complete=True,
        account_present=True,
        verified_monotonic_ms=100,
    ).model_copy(update=proof_updates)
    return CompletionContext(
        run_ref="run_1",
        session_ref="session_1",
        authentication_generation=4,
        account_binding_ref="inputs.account_id",
        target_origin="http://127.0.0.1:8080",
        approved_profile="parabank-native-v1",
        requested_account_id=SecretStr(requested_account_id),
        membership_proof=proof,
    )


def _view(**updates):
    values = {
        "run_ref": "run_1",
        "session_ref": "session_1",
        "observation_ref": "detail_2",
        "authentication_generation": 4,
        "origin": "http://127.0.0.1:8080",
        "profile_id": "parabank-native-v1",
        "safe_route": "account_details",
        "account_number_field_ref": "PROFILE_ACCOUNT_NUMBER",
        "account_type_field_ref": "PROFILE_ACCOUNT_TYPE",
        "available_balance_field_ref": "PROFILE_AVAILABLE_BALANCE",
        "available_balance_parser_id": "USD_DECIMAL_V1",
        "available_balance_parser_version": "1",
        "available_balance_currency": "USD",
        "page_state": "DETAIL_READY",
        "principal_matches": True,
        "account_number_values": (SecretStr("100001"),),
        "account_type_values": (SecretStr("SAVINGS"),),
        "available_balance_values": (SecretStr("$1,234.50"),),
    }
    values.update(updates)
    return CompletionView(**values)


def test_completion_verifier_returns_currently_verified_outputs_only():
    result = CompletionVerifier().verify(_contract(), _view(), _context())

    assert result.status is VerificationStatus.SUCCESS
    assert result.outputs["available_balance"].get_secret_value() == "1234.50"
    assert result.outputs["currency"].get_secret_value() == "USD"


def test_completion_verifier_keeps_zero_available_balance_as_zero():
    result = CompletionVerifier().verify(
        _contract(),
        _view(available_balance_values=(SecretStr("$0.00"),)),
        _context(),
    )

    assert result.status is VerificationStatus.SUCCESS
    assert result.outputs["available_balance"].get_secret_value() == "0.00"


@pytest.mark.parametrize(
    ("view_updates", "proof_updates", "expected_reason"),
    [
        ({"session_ref": "session_2"}, {}, "SESSION_MISMATCH"),
        ({"authentication_generation": 5}, {}, "AUTHENTICATION_CHANGED"),
        ({"account_number_values": (SecretStr("100002"),)}, {}, "SUBJECT_MISMATCH"),
        ({"account_type_values": (SecretStr("CHECKING"),)}, {}, "ACCOUNT_TYPE_MISMATCH"),
        ({"account_type_values": ()}, {}, "FIELD_UNKNOWN"),
        ({"account_type_values": (SecretStr("SAVINGS"), SecretStr("CHECKING"))}, {}, "TARGET_AMBIGUOUS"),
        ({"available_balance_values": (SecretStr("$1.00"), SecretStr("$2.00"))}, {}, "TARGET_AMBIGUOUS"),
        ({"available_balance_values": (SecretStr("$USD 1.00"),)}, {}, "INVALID_AMOUNT"),
        ({}, {"authentication_generation": 5}, "MEMBERSHIP_PROOF_INVALID"),
        ({}, {"overview_complete": False}, "INVALID_EVIDENCE"),
    ],
)
def test_completion_failure_never_returns_outputs(view_updates, proof_updates, expected_reason):
    result = CompletionVerifier().verify(
        _contract(),
        _view(**view_updates),
        _context(**proof_updates),
    )

    assert result.status is not VerificationStatus.SUCCESS
    assert result.reason_code == expected_reason
    assert result.outputs == {}


def test_incomplete_detail_and_unknown_principal_do_not_verify():
    verifier = CompletionVerifier()

    incomplete = verifier.verify(
        _contract(), _view(page_state="OVERVIEW_READY"), _context()
    )
    unknown_principal = verifier.verify(
        _contract(), _view(principal_matches=None), _context()
    )

    assert incomplete.reason_code == "DETAIL_NOT_READY"
    assert unknown_principal.reason_code == "PRINCIPAL_UNKNOWN"
    assert incomplete.outputs == unknown_principal.outputs == {}


def test_membership_proof_for_account_a_cannot_be_reused_for_account_b():
    context = _context(requested_account_id="100002", proof_account_id="100001")
    view = _view(account_number_values=(SecretStr("100002"),))

    result = CompletionVerifier().verify(_contract(), view, context)

    assert result.status is VerificationStatus.FAILED
    assert result.reason_code == "MEMBERSHIP_PROOF_INVALID"
    assert result.outputs == {}


@pytest.mark.parametrize(
    ("view_updates", "expected_reason"),
    [
        ({"origin": "http://localhost:8080"}, "ORIGIN_MISMATCH"),
        ({"safe_route": "account_overview"}, "INVALID_EVIDENCE"),
        ({"profile_id": "other-profile"}, "INVALID_EVIDENCE"),
        ({"available_balance_currency": "EUR"}, "INVALID_EVIDENCE"),
        ({"available_balance_parser_id": "generic_decimal"}, "INVALID_EVIDENCE"),
    ],
)
def test_completion_rejects_unbound_target_or_currency_semantics(view_updates, expected_reason):
    untrusted_view = _view().model_copy(update=view_updates)
    result = CompletionVerifier().verify(
        _contract(), untrusted_view, _context()
    )

    assert result.status is not VerificationStatus.SUCCESS
    assert result.reason_code == expected_reason
    assert result.outputs == {}


def test_contract_conditions_cannot_weaken_runtime_owned_verification():
    # The verifier accepts only the output contract, current view, and bound proof.
    # Capability predicates or a model's DONE decision are not verifier inputs.
    result = CompletionVerifier().verify(_contract(), _view(), _context())

    assert result.status is VerificationStatus.SUCCESS
