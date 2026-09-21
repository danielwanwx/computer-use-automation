"""Frozen, server-owned application settings."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import math
import re
from typing import Mapping
from urllib.parse import urlsplit

from pydantic import SecretStr

from cua.application.contracts import ValidationFixture
from cua.sessions.manager import PrincipalSpec


DEFAULT_TARGET_ORIGIN = "http://127.0.0.1:8080/parabank"
DEFAULT_TARGET_REVISION = "ee82474be5f58bea3ddc8be0fd831072b00201cb"


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    """Local paths and allowlists; credential values are never configuration fields."""

    data_root: Path
    principal_specs: tuple[PrincipalSpec, ...]
    target_origin: str = DEFAULT_TARGET_ORIGIN
    target_revision: str = DEFAULT_TARGET_REVISION
    target_lock_path: Path | None = None
    validation_fixtures: tuple[ValidationFixture, ...] = ()
    provider_enabled: bool = False
    provider_model: str | None = None
    browser_channel: str = "chrome"
    headless: bool = True
    browser_timeout_ms: int = 8_000
    run_timeout_seconds: float = 180.0
    result_ttl_seconds: float = 900.0
    validation_oracle_command: tuple[str, ...] | None = None
    validation_oracle_timeout_seconds: float = 5.0
    validation_oracle_max_bytes: int = 65_536
    operator_bind: str = "127.0.0.1"
    operator_port: int = 8765
    operator_token: SecretStr | None = None

    def __post_init__(self) -> None:
        root = Path(self.data_root).expanduser().resolve()
        principals = tuple(self.principal_specs)
        fixtures = tuple(self.validation_fixtures)
        if not principals or any(not isinstance(item, PrincipalSpec) for item in principals):
            raise ValueError("at least one principal specification is required")
        aliases = tuple(item.alias for item in principals)
        if len(set(aliases)) != len(aliases):
            raise ValueError("principal aliases must be unique")
        if any(not isinstance(item, ValidationFixture) for item in fixtures):
            raise ValueError("validation fixtures must be trusted fixture values")
        fixture_aliases = tuple(item.principal_alias for item in fixtures)
        if len(set(fixture_aliases)) != len(fixture_aliases):
            raise ValueError("validation fixture principal aliases must be unique")
        if not set(fixture_aliases).issubset(aliases):
            raise ValueError("validation fixtures must use configured principals")
        if not set(aliases).difference(fixture_aliases):
            raise ValueError("at least one ordinary principal must be separate from validation")
        fixture_accounts = tuple(item.account_id.get_secret_value() for item in fixtures)
        if len(set(fixture_accounts)) != len(fixture_accounts):
            raise ValueError("validation fixtures must use distinct account bindings")
        _validate_target_origin(self.target_origin)
        if re.fullmatch(r"[a-f0-9]{40}", self.target_revision, re.ASCII) is None:
            raise ValueError("target revision must be an immutable commit SHA")
        if type(self.provider_enabled) is not bool or type(self.headless) is not bool:
            raise ValueError("provider and browser flags must be booleans")
        if self.provider_model is not None and re.fullmatch(
            r"[A-Za-z0-9._:-]{1,96}", self.provider_model, re.ASCII
        ) is None:
            raise ValueError("provider model identifier is invalid")
        if self.provider_enabled and self.provider_model is None:
            raise ValueError("an enabled provider requires a configured model")
        if self.browser_channel not in {"chrome", "msedge"}:
            raise ValueError("browser channel is not allowlisted")
        if type(self.browser_timeout_ms) is not int or not 100 <= self.browser_timeout_ms <= 60_000:
            raise ValueError("browser timeout is invalid")
        _bounded_seconds(self.run_timeout_seconds, 0 < self.run_timeout_seconds <= 1_800,
                         "run timeout is invalid")
        _bounded_seconds(self.result_ttl_seconds, 1 <= self.result_ttl_seconds <= 86_400,
                         "result retention is invalid")
        command = self.validation_oracle_command
        if command is not None:
            command = tuple(command)
            if not command or any(not isinstance(item, str) or not item for item in command):
                raise ValueError("validation oracle command is invalid")
            object.__setattr__(self, "validation_oracle_command", command)
        _bounded_seconds(
            self.validation_oracle_timeout_seconds,
            0.1 <= self.validation_oracle_timeout_seconds <= 30.0,
            "validation oracle timeout is invalid",
        )
        if (
            isinstance(self.validation_oracle_max_bytes, bool)
            or not isinstance(self.validation_oracle_max_bytes, int)
            or not 256 <= self.validation_oracle_max_bytes <= 1_048_576
        ):
            raise ValueError("validation oracle output limit is invalid")
        if self.operator_bind not in {"127.0.0.1", "localhost"}:
            raise ValueError("operator bind must be loopback")
        if isinstance(self.operator_port, bool) or not isinstance(self.operator_port, int) or not 1024 <= self.operator_port <= 65535:
            raise ValueError("operator port is invalid")
        if self.operator_token is not None and (
            not isinstance(self.operator_token, SecretStr)
            or not self.operator_token.get_secret_value()
            or len(self.operator_token.get_secret_value()) > 256
        ):
            raise ValueError("operator token is invalid")
        lock_path = self.target_lock_path
        if lock_path is not None:
            object.__setattr__(self, "target_lock_path", Path(lock_path).expanduser().resolve())
        object.__setattr__(self, "data_root", root)
        object.__setattr__(self, "principal_specs", principals)
        object.__setattr__(self, "validation_fixtures", fixtures)

    @property
    def evidence_root(self) -> Path:
        return self.data_root / "evidence"

    @property
    def registry_root(self) -> Path:
        return self.data_root / "bundles"

    @property
    def validation_principal_aliases(self) -> tuple[str, ...]:
        return tuple(item.principal_alias for item in self.validation_fixtures)

    @property
    def ordinary_principal_aliases(self) -> tuple[str, ...]:
        validation_aliases = set(self.validation_principal_aliases)
        return tuple(
            item.alias
            for item in self.principal_specs
            if item.alias not in validation_aliases
        )

    @property
    def operator_origin(self) -> str:
        return f"http://{self.operator_bind}:{self.operator_port}"

    @classmethod
    def from_environment(
        cls,
        principal_specs: tuple[PrincipalSpec, ...] | None = None,
        *,
        validation_fixture_env: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "ApplicationConfig":
        """Load server-owned settings from an explicit environment mapping."""
        values = os.environ if environ is None else environ
        if principal_specs is None:
            principal_specs = principal_specs_from_environment(values)
        if validation_fixture_env is None:
            validation_fixture_env = _json_env_mapping(
                values.get("CUA_VALIDATION_FIXTURE_ENV_JSON", "")
                or values.get("CUA_VALIDATION_FIXTURES_JSON", "")
            )
        fixtures: list[ValidationFixture] = []
        for alias, env_name in validation_fixture_env.items():
            if not isinstance(alias, str) or not isinstance(env_name, str):
                raise ValueError("validation fixture environment mapping is invalid")
            account = values.get(env_name, "")
            if not account:
                raise ValueError("configured validation fixture is unavailable")
            fixtures.append(ValidationFixture(alias, SecretStr(account)))
        command = _json_argv(values.get("CUA_VALIDATION_ORACLE_COMMAND_JSON", ""))
        return cls(
            data_root=Path(values.get("CUA_DATA_ROOT", ".cua")),
            principal_specs=tuple(principal_specs),
            target_origin=values.get("CUA_TARGET_ORIGIN", DEFAULT_TARGET_ORIGIN),
            target_revision=values.get("CUA_TARGET_REVISION", DEFAULT_TARGET_REVISION),
            target_lock_path=values.get("CUA_TARGET_LOCK_PATH") or None,
            validation_fixtures=tuple(fixtures),
            provider_enabled=_env_bool(values.get("CUA_PROVIDER_ENABLED", "false")),
            provider_model=values.get("CUA_PROVIDER_MODEL") or None,
            browser_channel=values.get("CUA_BROWSER_CHANNEL", "chrome"),
            headless=_env_bool(values.get("CUA_BROWSER_HEADLESS", "true")),
            validation_oracle_command=command,
            operator_bind=values.get("CUA_OPERATOR_BIND", "127.0.0.1"),
            operator_port=int(values.get("CUA_OPERATOR_PORT", "8765")),
            operator_token=(
                SecretStr(values["CUA_OPERATOR_TOKEN"])
                if values.get("CUA_OPERATOR_TOKEN")
                else None
            ),
        )


def principal_specs_from_environment(
    environ: Mapping[str, str] | None = None,
) -> tuple[PrincipalSpec, ...]:
    """Parse server-owned principal metadata; credentials remain env references."""
    values = os.environ if environ is None else environ
    raw = values.get("CUA_PRINCIPALS_JSON", "")
    if not raw:
        raise ValueError("CUA_PRINCIPALS_JSON is required")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("principal configuration is invalid") from None
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("principal configuration is invalid")
    result: list[PrincipalSpec] = []
    expected = {"alias", "username_env", "password_env", "expected_display_name"}
    for item in parsed:
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError("principal configuration is invalid")
        try:
            result.append(PrincipalSpec(**item))
        except (TypeError, ValueError):
            raise ValueError("principal configuration is invalid") from None
    return tuple(result)


def _json_env_mapping(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("validation fixture configuration is invalid") from None
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in parsed.items()
    ):
        raise ValueError("validation fixture configuration is invalid")
    return parsed


def _bounded_seconds(value: object, in_range: bool, error: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(error)
    if not math.isfinite(float(value)) or not in_range:
        raise ValueError(error)


def _validate_target_origin(origin: str) -> None:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        raise ValueError("target must be the loopback ParaBank origin") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port != 8080
        or parsed.path != "/parabank"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in origin
        or "#" in origin
    ):
        raise ValueError("target must be the loopback ParaBank origin")


def _env_bool(value: str) -> bool:
    value = value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("boolean environment setting is invalid")


def _json_argv(value: str) -> tuple[str, ...] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError("oracle command must be a JSON argv array") from None
    if not isinstance(parsed, list) or not parsed or any(
        not isinstance(item, str) or not item for item in parsed
    ):
        raise ValueError("oracle command must be a JSON argv array")
    return tuple(parsed)
