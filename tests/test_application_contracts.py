from dataclasses import FrozenInstanceError
from pathlib import Path

from pydantic import SecretStr, ValidationError
import pytest

from cua.application import (
    ApplicationConfig,
    DiscoveryRequest,
    OracleReport,
    ReplayRequest,
    RunHandle,
    RunMode,
    RunView,
    SessionView,
    ValidationFixture,
)
from cua.evidence.models import RunState
from cua.models.bundles import BundleReference
from cua.sessions.manager import PrincipalSpec


def _principal(alias: str = "synthetic_alpha") -> PrincipalSpec:
    suffix = "ALPHA" if alias == "synthetic_alpha" else "BETA"
    return PrincipalSpec(
        alias=alias,
        username_env=f"PARABANK_DEMO_{suffix}_USERNAME",
        password_env=f"PARABANK_DEMO_{suffix}_PASSWORD",
        expected_display_name=f"Synthetic {suffix.title()}",
    )


def _reference() -> BundleReference:
    return BundleReference(
        name="get_savings_balance",
        version="1.0.0",
        digest="a" * 64,
    )


def test_config_is_server_owned_pinned_and_provider_disabled_by_default(tmp_path):
    fixture = ValidationFixture("synthetic_beta", SecretStr("00002"))
    config = ApplicationConfig(
        data_root=tmp_path,
        principal_specs=(_principal(), _principal("synthetic_beta")),
        validation_fixtures=(fixture,),
    )

    assert config.provider_enabled is False
    assert config.provider_model is None
    assert config.evidence_root == tmp_path.resolve() / "evidence"
    assert config.registry_root == tmp_path.resolve() / "bundles"
    assert config.target_revision == "ee82474be5f58bea3ddc8be0fd831072b00201cb"
    assert config.validation_principal_aliases == ("synthetic_beta",)
    assert config.ordinary_principal_aliases == ("synthetic_alpha",)
    assert "Synthetic Alpha" not in repr(config)
    assert "00001" not in repr(config)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        config.provider_enabled = True


def test_discovery_request_masks_protected_values_and_forbids_extra_authority():
    request = DiscoveryRequest(
        session_id="s_0123456789abcdef",
        goal=SecretStr("Get savings balance for 00001"),
        inputs={"account_id": SecretStr("00001")},
        request_id="client-1",
        capability_version="1.0.0",
    )

    assert "00001" not in repr(request)
    assert "Get savings balance" not in repr(request)
    with pytest.raises(ValidationError):
        DiscoveryRequest.model_validate(
            request.model_dump(mode="python") | {"provider_enabled": True}
        )
    with pytest.raises(ValidationError):
        DiscoveryRequest.model_validate(
            request.model_dump(mode="python") | {"operator_approved": True}
        )


def test_requests_reject_unapproved_input_names_and_invalid_ids():
    with pytest.raises(ValidationError):
        DiscoveryRequest(
            session_id="s_0123456789abcdef",
            goal=SecretStr("Get savings balance"),
            inputs={"account_id": SecretStr("1"), "password": SecretStr("private")},
            request_id="client-1",
        )
    with pytest.raises(ValidationError):
        ReplayRequest(
            session_id="not-a-session",
            reference=_reference(),
            inputs={"account_id": SecretStr("00001")},
            request_id="client-1",
        )


def test_replay_request_and_status_views_contain_no_protected_inputs():
    request = ReplayRequest(
        session_id="s_0123456789abcdef",
        reference=_reference(),
        inputs={"account_id": SecretStr("00001")},
        request_id="client-1",
    )
    session = SessionView(
        session_id=request.session_id,
        principal_alias="synthetic_alpha",
        profile_id="parabank-native-v1",
        origin="http://127.0.0.1:8080",
        authentication_generation=1,
    )
    handle = RunHandle(run_id="run_0123456789abcdef", mode=RunMode.REPLAY)
    view = RunView(
        run_id=handle.run_id,
        mode=RunMode.REPLAY,
        state=RunState.RUNNING,
        created_at_ms=100,
        updated_at_ms=100,
        current_step="open_account",
    )

    assert "00001" not in repr(request)
    assert "00001" not in repr(session)
    assert "00001" not in repr(handle)
    assert "00001" not in repr(view)
    assert view.current_step == "open_account"


def test_oracle_report_only_accepts_safe_consistent_verdicts():
    assert OracleReport(passed=True, code="ORACLE_PASS").passed
    with pytest.raises(ValueError):
        OracleReport(passed=True, code="ORACLE_MISMATCH")
    with pytest.raises(ValueError):
        OracleReport(passed=False, code="account 00001 mismatched")


def test_config_from_environment_keeps_fixture_and_oracle_server_owned(tmp_path):
    config = ApplicationConfig.from_environment(
        (_principal(), _principal("synthetic_beta")),
        validation_fixture_env={"synthetic_beta": "FIXTURE_ACCOUNT"},
        environ={
            "CUA_DATA_ROOT": str(tmp_path),
            "FIXTURE_ACCOUNT": "7048162359",
            "CUA_VALIDATION_ORACLE_COMMAND_JSON": '["/trusted/oracle", "--stdio"]',
            "CUA_PROVIDER_ENABLED": "false",
        },
    )
    assert config.validation_principal_aliases == ("synthetic_beta",)
    assert config.validation_fixtures[0].account_id.get_secret_value() == "7048162359"
    assert config.validation_oracle_command == ("/trusted/oracle", "--stdio")
    assert config.provider_enabled is False
    assert "7048162359" not in repr(config)
