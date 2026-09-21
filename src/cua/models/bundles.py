from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from cua.models.base import StrictModel
from cua.models.conditions import ConditionExpr


Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
Reference = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]*$", max_length=96)]
Revision = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)]
ProfileId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]

StepKind = Literal["CLICK", "TYPE_TEXT", "SELECT", "SCROLL", "ASSERT", "EXTRACT", "VERIFY", "WAIT"]
StepOrigin = Literal["observed", "declared", "reviewer_added"]
BundleOperation = Literal["CLICK", "TYPE_TEXT", "SELECT", "SCROLL", "READ"]


class CapabilityIdentity(StrictModel):
    name: Name
    version: Revision


class Compatibility(StrictModel):
    profile: ProfileId
    runtime_contract: ProfileId
    profile_sha256: Sha256
    condition_runtime_sha256: Sha256


class InputContract(StrictModel):
    name: Name
    value_type: Literal["string"]
    pattern: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    sensitive: bool


class OutputContract(StrictModel):
    name: Name
    value_type: Literal["decimal_string", "string"]
    sensitive: bool = False
    enum: tuple[Annotated[str, StringConstraints(min_length=1, max_length=32)], ...] = ()


class CapabilityContract(StrictModel):
    inputs: tuple[InputContract, ...]
    outputs: tuple[OutputContract, ...]
    business_outcomes: tuple[ReasonCode, ...] = ()


class StepSource(StrictModel):
    type: StepOrigin
    event_ids: tuple[Reference, ...] = ()
    reviewer_ref: Reference | None = None

    @model_validator(mode="after")
    def validate_origin_evidence(self):
        if self.type == "observed" and not self.event_ids:
            raise ValueError("observed steps require source event IDs")
        if self.type == "declared" and (self.event_ids or self.reviewer_ref):
            raise ValueError("declared steps cannot claim observed or reviewer provenance")
        if self.type == "reviewer_added" and self.reviewer_ref is None:
            raise ValueError("reviewer-added steps require a reviewer reference")
        if self.type != "observed" and self.event_ids:
            raise ValueError("only observed steps may carry observed event IDs")
        return self


class BundleStep(StrictModel):
    id: Name
    kind: StepKind
    target_ref: Reference | None = None
    preconditions: tuple[ConditionExpr, ...] = ()
    postconditions: tuple[ConditionExpr, ...] = ()
    recovery_ref: Reference | None = None
    input_ref: Reference | None = None
    option_ref: Reference | None = None
    direction: Literal["UP", "DOWN"] | None = None
    pixels: int | None = Field(default=None, gt=0, le=1000)
    timeout_ms: int | None = Field(default=None, ge=0, le=30000)
    output_ref: Reference | None = None
    parser_id: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)] | None = None
    parser_version: Annotated[str, StringConstraints(min_length=1, max_length=32)] | None = None
    source: StepSource

    @model_validator(mode="after")
    def validate_step_shape(self):
        target_kinds = {"CLICK", "TYPE_TEXT", "SELECT", "SCROLL", "EXTRACT"}
        if self.kind in target_kinds and self.target_ref is None:
            raise ValueError(f"{self.kind} steps require a target reference")
        if self.kind not in target_kinds and self.target_ref is not None:
            raise ValueError(f"{self.kind} steps cannot name a browser target")
        if self.kind == "TYPE_TEXT" and self.input_ref is None:
            raise ValueError("TYPE_TEXT requires a bound input reference")
        if self.kind != "TYPE_TEXT" and self.input_ref is not None:
            raise ValueError("only TYPE_TEXT may name an input reference")
        if self.kind == "SELECT" and self.option_ref is None:
            raise ValueError("SELECT requires an observed option reference")
        if self.kind != "SELECT" and self.option_ref is not None:
            raise ValueError("only SELECT may name an option reference")
        if self.kind == "SCROLL" and (self.direction is None or self.pixels is None):
            raise ValueError("SCROLL requires bounded direction and pixels")
        if self.kind != "SCROLL" and (self.direction is not None or self.pixels is not None):
            raise ValueError("only SCROLL may specify direction or pixels")
        if self.kind == "WAIT" and self.timeout_ms is None:
            raise ValueError("WAIT requires a bounded timeout")
        if self.kind != "WAIT" and self.timeout_ms is not None:
            raise ValueError("only WAIT may specify a condition timeout")
        if self.kind == "EXTRACT":
            if self.output_ref is None or self.parser_id is None or self.parser_version is None:
                raise ValueError("EXTRACT requires output and parser references")
        elif self.output_ref is not None or self.parser_id is not None or self.parser_version is not None:
            raise ValueError("only EXTRACT may name an output or parser")
        if self.kind == "WAIT" and not self.postconditions:
            raise ValueError("WAIT must be condition-driven")
        if self.kind == "ASSERT" and not self.preconditions:
            raise ValueError("ASSERT requires explicit conditions")
        if self.kind == "VERIFY" and (self.preconditions or self.postconditions):
            raise ValueError("VERIFY uses runtime-owned checks, not bundle conditions")
        return self


class TargetDefinition(StrictModel):
    ref: Reference
    locator: Literal[
        "ROLE_LINK_ACCOUNTS_OVERVIEW",
        "TABLE_ACCOUNT_LINK_BY_INPUT",
        "PROFILE_ACCOUNT_NUMBER",
        "PROFILE_ACCOUNT_TYPE",
        "PROFILE_AVAILABLE_BALANCE",
    ]
    allowed_operations: tuple[BundleOperation, ...]
    binding_ref: Reference | None = None


class ConditionDefinition(StrictModel):
    name: Name
    expression: ConditionExpr


class RecoveryDefinition(StrictModel):
    ref: Reference
    anchor_target_ref: Reference
    max_attempts: int = Field(ge=0, le=2)


class ParserPin(StrictModel):
    parser_id: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)]
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    implementation_sha256: Sha256


class LocatorProfileOverride(StrictModel):
    field: Literal[
        "overview_nav",
        "account_table",
        "requested_account_link",
        "detail_account_id",
        "detail_account_type",
        "detail_available_balance",
    ]
    locator: Literal[
        "ROLE_LINK_ACCOUNTS_OVERVIEW",
        "TABLE_ACCOUNT_LINK_BY_INPUT",
        "PROFILE_ACCOUNT_NUMBER",
        "PROFILE_ACCOUNT_TYPE",
        "PROFILE_AVAILABLE_BALANCE",
    ]


class CapabilityBlueprint(StrictModel):
    """Non-executable definitions supplied to the trace compiler."""
    schema_version: Literal["1"]
    capability: CapabilityIdentity
    compatibility: Compatibility
    contract: CapabilityContract
    targets: tuple[TargetDefinition, ...] = ()
    conditions: tuple[ConditionDefinition, ...] = ()
    recoveries: tuple[RecoveryDefinition, ...] = ()
    parsers: tuple[ParserPin, ...] = ()
    profile_overrides: tuple[LocatorProfileOverride, ...] = ()
    profile_signals: tuple[ReasonCode, ...] = ()
    checkpoints: tuple["CompilerCheckpoint", ...] = ()

    @model_validator(mode="after")
    def validate_checkpoint_ids(self):
        _unique(self.checkpoints, "checkpoint step ID", lambda item: item.step.id)
        return self


class CompilerCheckpoint(StrictModel):
    step: BundleStep
    placement: Literal["BEFORE_ALL", "BEFORE_EVENT", "AFTER_EVENT", "AFTER_ALL"]
    event_id: Reference | None = None

    @model_validator(mode="after")
    def validate_placement(self):
        if self.placement in {"BEFORE_EVENT", "AFTER_EVENT"} and self.event_id is None:
            raise ValueError("event-relative checkpoints require an event ID")
        if self.placement in {"BEFORE_ALL", "AFTER_ALL"} and self.event_id is not None:
            raise ValueError("global checkpoints cannot name an event ID")
        if self.step.source.type == "observed":
            raise ValueError("checkpoints must be declared or reviewer-added")
        if self.step.kind == "VERIFY" and self.placement != "AFTER_ALL":
            raise ValueError("runtime completion verification must be last")
        if self.step.kind == "VERIFY" and (
            self.step.preconditions or self.step.postconditions
        ):
            raise ValueError("completion verification cannot be weakened by bundle conditions")
        return self


CapabilityBlueprint.model_rebuild()


class TraceProvenance(StrictModel):
    trace_id: Reference
    verified: bool
    completion_proof_id: Reference | None = None


class CapabilityBundle(StrictModel):
    schema_version: Literal["1"]
    capability: CapabilityIdentity
    compatibility: Compatibility
    contract: CapabilityContract
    steps: tuple[BundleStep, ...] = Field(min_length=1, max_length=32)
    targets: tuple[TargetDefinition, ...] = ()
    conditions: tuple[ConditionDefinition, ...] = ()
    recoveries: tuple[RecoveryDefinition, ...] = ()
    parsers: tuple[ParserPin, ...] = ()
    profile_overrides: tuple[LocatorProfileOverride, ...] = ()
    profile_signals: tuple[ReasonCode, ...] = ()
    provenance: TraceProvenance

    @model_validator(mode="after")
    def validate_unique_names(self):
        _unique(self.contract.inputs, "input name", lambda item: item.name)
        _unique(self.contract.outputs, "output name", lambda item: item.name)
        _unique(self.steps, "step ID", lambda item: item.id)
        _unique(self.targets, "target reference", lambda item: item.ref)
        _unique(self.conditions, "condition name", lambda item: item.name)
        _unique(self.recoveries, "recovery reference", lambda item: item.ref)
        _unique(self.parsers, "parser ID/version", lambda item: (item.parser_id, item.version))
        _unique(self.profile_overrides, "profile override", lambda item: item.field)
        _unique(self.profile_signals, "profile signal", lambda item: item)
        return self


class BundleReference(StrictModel):
    name: Name
    version: Revision
    digest: Sha256


def _unique(items, label: str, key):
    seen = set()
    for item in items:
        value = key(item)
        if value in seen:
            raise ValueError(f"duplicate {label}")
        seen.add(value)
