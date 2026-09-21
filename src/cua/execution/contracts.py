"""Ephemeral, value-safe contracts for one deterministic UI dispatch."""

from dataclasses import dataclass, field
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Annotated, Literal, Mapping

from pydantic import Field, SecretStr, StringConstraints

from cua.evidence.models import EvidenceRef, RunAlias, SafeReasonCode
from cua.models.base import StrictModel
from cua.models.verification import MembershipProof, NativeOrigin
from cua.policy.engine import PolicyContext


class EffectState(StrEnum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    DISPATCHED = "DISPATCHED"
    VERIFIED = "VERIFIED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class InvocationStatus(StrEnum):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    FAILURE = "FAILURE"
    ABORTED = "ABORTED"


@dataclass(frozen=True, slots=True)
class FailureDetail:
    """Safe summary of the step that prevented a successful invocation."""

    reason_code: SafeReasonCode
    step_id: str | None = None
    effect_state: EffectState | None = None


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """Terminal caller result; sensitive outputs exist in memory only on SUCCESS."""

    run_id: RunAlias
    status: InvocationStatus
    outputs: Mapping[str, SecretStr] | None = field(default=None, repr=False)
    code: SafeReasonCode | None = None
    failure: FailureDetail | None = field(default=None, repr=False)
    evidence_refs: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if re.fullmatch(r"run_[a-f0-9]{16}", self.run_id, re.ASCII) is None:
            raise ValueError("invocation run ID is invalid")
        if not isinstance(self.status, InvocationStatus):
            raise ValueError("invocation status is invalid")
        if self.outputs is not None:
            output_values = dict(self.outputs)
            if any(
                re.fullmatch(r"[a-z][a-z0-9_]*", name, re.ASCII) is None
                or not isinstance(value, SecretStr)
                for name, value in output_values.items()
            ):
                raise ValueError("invocation outputs must be named secret values")
            object.__setattr__(self, "outputs", MappingProxyType(output_values))
        if self.status is InvocationStatus.SUCCESS:
            if self.outputs is None:
                raise ValueError("successful result requires verified outputs")
            if self.failure is not None:
                raise ValueError("successful result cannot contain failure detail")
        elif self.outputs is not None:
            raise ValueError("only successful results may expose outputs")
        if self.status in {InvocationStatus.FAILURE, InvocationStatus.ABORTED}:
            if self.failure is None:
                raise ValueError("failure detail is required for failed or aborted results")
        if self.status is InvocationStatus.BUSINESS_OUTCOME and self.code is None:
            raise ValueError("business outcome requires a safe outcome code")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    def __repr__(self) -> str:
        return (
            "InvocationResult("
            f"run_id={self.run_id!r}, status={self.status.value!r}, "
            f"code={self.code.value if self.code else None!r}, "
            f"evidence_count={len(self.evidence_refs)})"
        )


SessionId = Annotated[str, StringConstraints(pattern=r"^s_[A-Za-z0-9_-]{1,64}$")]


class ExecutionContext(StrictModel):
    """Per-run state. Never persist this object or include it in evidence."""

    run_alias: RunAlias
    session_id: SessionId
    expected_epoch: int = Field(ge=0)
    authentication_generation: int = Field(ge=0)
    target_origin: NativeOrigin
    profile_id: Literal["parabank-native-v1"] = "parabank-native-v1"
    input_bindings: dict[str, SecretStr] = Field(repr=False)
    policy_context: PolicyContext
    membership_proof: MembershipProof | None = Field(default=None, repr=False)
    deadline_monotonic: float | None = Field(default=None, ge=0, repr=False)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    step_id: str
    effect_state: EffectState
    reason_code: SafeReasonCode
    observation_id: str | None = None
    value: SecretStr | None = field(default=None, repr=False)
    membership_proof: MembershipProof | None = field(default=None, repr=False)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    retry_safe: bool = False

    def __repr__(self) -> str:
        return (
            "ExecutionResult("
            f"step_id={self.step_id!r}, effect_state={self.effect_state.value!r}, "
            f"reason_code={self.reason_code.value!r}, observation_id={self.observation_id!r})"
        )
