"""Value-safe request, status, and validation contracts for the app boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Annotated, Literal, Protocol

from pydantic import Field, SecretStr, StringConstraints, model_validator

from cua.evidence.models import RunAlias, RunState, SafeReasonCode
from cua.execution.contracts import InvocationResult, SessionId
from cua.models.base import StrictModel
from cua.models.bundles import BundleReference, CapabilityBundle
from cua.models.qualification import ApprovalRecord, ValidationQualification
from cua.sessions.manager import PrincipalSpec


RequestId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,96}$", min_length=1, max_length=96),
]
StepId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
SafeIdentifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,96}$")]


class RunMode(StrEnum):
    DISCOVERY = "DISCOVERY"
    VALIDATION = "VALIDATION"
    REPLAY = "REPLAY"


class DiscoveryRequest(StrictModel):
    """Discovery input; goal and optional business bindings stay protected."""

    target_id: str = "parabank-local"
    session_id: SessionId
    goal: SecretStr = Field(repr=False, min_length=1, max_length=2_000)
    inputs: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    request_id: RequestId
    capability_version: Annotated[
        str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)
    ] = "1.0.0"

    @model_validator(mode="after")
    def validate_public_scope(self):
        if self.target_id != "parabank-local":
            raise ValueError("target is not configured")
        if any(key not in {"account_id", "inputs.account_id"} for key in self.inputs):
            raise ValueError("input name is not supported")
        return self


class ReplayRequest(StrictModel):
    reference: BundleReference
    session_id: SessionId
    inputs: dict[str, SecretStr] = Field(repr=False)
    request_id: RequestId

    @model_validator(mode="after")
    def validate_public_scope(self):
        if any(key not in {"account_id", "inputs.account_id"} for key in self.inputs):
            raise ValueError("input name is not supported")
        return self


class SessionView(StrictModel):
    session_id: SessionId
    principal_alias: Annotated[
        str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$", max_length=32)
    ]
    profile_id: Annotated[
        str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$", max_length=64)
    ]
    origin: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    authentication_generation: int = Field(ge=0)


class RunHandle(StrictModel):
    run_id: RunAlias
    mode: RunMode
    state: RunState = RunState.CREATED


class RunView(StrictModel):
    """Safe status only; there are no goal, account, output, or session fields."""

    run_id: RunAlias
    mode: RunMode
    state: RunState
    current_step: StepId | None = None
    outcome_code: SafeReasonCode | None = None
    intervention_id: SafeIdentifier | None = None
    capability_reference: BundleReference | None = None
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    decisions_used: int = Field(default=0, ge=0)
    verified_action_count: int = Field(default=0, ge=0)


class CapabilityLifecycle(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"


class CapabilitySummary(StrictModel):
    reference: BundleReference
    lifecycle: CapabilityLifecycle
    step_count: int = Field(ge=1, le=32)
    validation_run_ref: SafeIdentifier | None = None
    approved: bool


class ValidationReport(StrictModel):
    reference: BundleReference
    run_id: RunAlias
    passed: bool
    code: Literal[
        "VALIDATION_PASS",
        "REPLAY_FAILED",
        "ORACLE_MISMATCH",
        "ORACLE_UNAVAILABLE",
        "INVALID_BUNDLE",
    ]


class CapabilityDetail(StrictModel):
    reference: BundleReference
    lifecycle: CapabilityLifecycle
    bundle: CapabilityBundle
    qualification: ValidationQualification | None = None
    approval: ApprovalRecord | None = None
    latest_validation: ValidationReport | None = None


class ServiceError(RuntimeError):
    """Expected, redacted service failure with a stable status and safe code."""

    def __init__(self, status: int, code: str) -> None:
        if type(status) is not int or not 400 <= status <= 599:
            raise ValueError("service error status is invalid")
        if not isinstance(code, str) or not re.fullmatch(
            r"[A-Z][A-Z0-9_]{0,63}", code, re.ASCII
        ):
            raise ValueError("service error code is invalid")
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ValidationFixture:
    """Server-owned synthetic fixture; its account binding is memory-only."""

    principal_alias: str
    account_id: SecretStr = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.principal_alias, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,31}", self.principal_alias, re.ASCII
        ):
            raise ValueError("validation fixture principal alias is invalid")
        if not isinstance(self.account_id, SecretStr):
            raise ValueError("validation fixture account ID must be secret")
        value = self.account_id.get_secret_value()
        if not re.fullmatch(r"[0-9]{1,20}", value, re.ASCII):
            raise ValueError("validation fixture account ID is invalid")

    def __repr__(self) -> str:
        return f"ValidationFixture(principal_alias={self.principal_alias!r}, account_id='<redacted>')"


@dataclass(frozen=True, slots=True)
class OracleReport:
    passed: bool
    code: str

    def __post_init__(self) -> None:
        if type(self.passed) is not bool:
            raise ValueError("oracle pass state must be boolean")
        if self.code not in {"ORACLE_PASS", "ORACLE_MISMATCH", "ORACLE_UNAVAILABLE"}:
            raise ValueError("oracle report code is invalid")
        if self.passed != (self.code == "ORACLE_PASS"):
            raise ValueError("oracle report pass state and code disagree")


class ValidationOracle(Protocol):
    async def check(self, result: InvocationResult, fixture: ValidationFixture) -> OracleReport: ...


# Keep these imports visible to type consumers without making them public request fields.
ConfiguredPrincipal = PrincipalSpec
