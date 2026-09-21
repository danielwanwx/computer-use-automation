from pydantic import SecretStr, ValidationError
import pytest

from cua.application import ApplicationConfig, DiscoveryRequest, ValidationFixture
from cua.sessions import PrincipalSpec


def _principal() -> PrincipalSpec:
    return PrincipalSpec(
        alias="synthetic_alpha",
        username_env="PARABANK_DEMO_ALPHA_USERNAME",
        password_env="PARABANK_DEMO_ALPHA_PASSWORD",
        expected_display_name="Synthetic Alpha",
    )


def test_application_provider_is_disabled_by_default_and_config_repr_is_safe(tmp_path):
    config = ApplicationConfig(
        data_root=tmp_path,
        principal_specs=(_principal(),),
    )
    fixture = ValidationFixture(
        principal_alias="synthetic_alpha",
        account_id=SecretStr("00001"),
    )

    assert config.provider_enabled is False
    assert config.provider_model is None
    assert "Synthetic Alpha" not in repr(config)
    assert "00001" not in repr(fixture)
    assert "PARABANK_DEMO_ALPHA_PASSWORD" in repr(config)


def test_discovery_request_hides_goal_and_account_and_rejects_extra_fields():
    request = DiscoveryRequest(
        session_id="s_0123456789abcdef",
        goal=SecretStr("Get savings balance 00001"),
        inputs={"account_id": SecretStr("00001")},
        request_id="client-1",
        capability_version="1.0.0",
    )

    assert "00001" not in repr(request)
    assert "Get savings balance" not in repr(request)
    with pytest.raises(ValidationError):
        DiscoveryRequest.model_validate(
            request.model_dump(mode="python") | {"operator_approved": True}
        )
