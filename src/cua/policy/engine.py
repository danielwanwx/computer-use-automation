from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from cua.models.base import StrictModel
from cua.models.observations import ControlRef


class Risk(StrEnum):
    READ_ONLY = "READ_ONLY"
    WRITE = "WRITE"
    UNKNOWN = "UNKNOWN"


Origin = Annotated[
    str,
    StringConstraints(
        pattern=r"^https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?$",
        min_length=9,
        max_length=160,
    ),
]
Route = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Operation = Literal["CLICK", "TYPE_TEXT", "SELECT", "SCROLL", "READ"]
ReasonCode = Literal[
    "AUTHORIZED",
    "RISK_NOT_READ_ONLY",
    "ORIGIN_NOT_ALLOWED",
    "ROUTE_NOT_ALLOWED",
    "OPERATION_NOT_ALLOWED",
    "CONTROL_OPERATION_NOT_ALLOWED",
    "INVALID_POLICY_CONTEXT",
    "INVALID_RUNTIME_CLASSIFICATION",
]


class PolicyContext(StrictModel):
    """Independent intersections of deployment, capability, and run authority."""

    deployment_origins: tuple[Origin, ...] = Field(min_length=1, max_length=32)
    capability_origins: tuple[Origin, ...] = Field(min_length=1, max_length=32)
    run_origins: tuple[Origin, ...] = Field(min_length=1, max_length=32)
    deployment_routes: tuple[Route, ...] = Field(min_length=1, max_length=64)
    capability_routes: tuple[Route, ...] = Field(min_length=1, max_length=64)
    run_routes: tuple[Route, ...] = Field(min_length=1, max_length=64)
    deployment_operations: tuple[Operation, ...] = Field(min_length=1, max_length=8)
    capability_operations: tuple[Operation, ...] = Field(min_length=1, max_length=8)
    run_operations: tuple[Operation, ...] = Field(min_length=1, max_length=8)
    # Retained only as provenance. Authorization uses the live classification below.
    artifact_risk_label: Risk = Risk.UNKNOWN


class RuntimeTargetClassification(StrictModel):
    """Runtime-owned classification derived from the current visible target/profile."""

    origin: Origin
    route: Route
    operation: Operation
    risk: Risk
    control_ref: ControlRef | None
    control_operations: tuple[Operation, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_control_binding(self):
        if self.operation != "READ" and self.control_ref is None:
            raise ValueError("interactive operations require a current control reference")
        if self.operation == "READ" and self.control_ref is not None:
            raise ValueError("profile field reads do not use a browser control reference")
        return self


class Authorization(StrictModel):
    allowed: bool
    reason_code: ReasonCode


class PolicyEngine:
    """Deny unless actual target risk and every authority intersection permit it."""

    def authorize(
        self,
        context: PolicyContext,
        target: RuntimeTargetClassification,
    ) -> Authorization:
        try:
            context = PolicyContext.model_validate(context.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            return Authorization(allowed=False, reason_code="INVALID_POLICY_CONTEXT")

        try:
            target = RuntimeTargetClassification.model_validate(
                target.model_dump(mode="python")
            )
        except (AttributeError, ValidationError):
            return Authorization(
                allowed=False,
                reason_code="INVALID_RUNTIME_CLASSIFICATION",
            )

        if target.risk is not Risk.READ_ONLY:
            return Authorization(allowed=False, reason_code="RISK_NOT_READ_ONLY")

        if target.origin not in (
            set(context.deployment_origins)
            & set(context.capability_origins)
            & set(context.run_origins)
        ):
            return Authorization(allowed=False, reason_code="ORIGIN_NOT_ALLOWED")

        if target.route not in (
            set(context.deployment_routes)
            & set(context.capability_routes)
            & set(context.run_routes)
        ):
            return Authorization(allowed=False, reason_code="ROUTE_NOT_ALLOWED")

        if target.operation not in (
            set(context.deployment_operations)
            & set(context.capability_operations)
            & set(context.run_operations)
        ):
            return Authorization(allowed=False, reason_code="OPERATION_NOT_ALLOWED")

        if target.operation not in target.control_operations:
            return Authorization(
                allowed=False,
                reason_code="CONTROL_OPERATION_NOT_ALLOWED",
            )

        return Authorization(allowed=True, reason_code="AUTHORIZED")
