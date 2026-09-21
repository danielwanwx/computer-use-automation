from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from cua.models.base import StrictModel
from cua.models.bundles import Name, Revision, Sha256, ProfileId
from cua.models.observations import ControlRef
from cua.models.qualification import GitRevision, SafeVersion


RunAlias = Annotated[str, StringConstraints(pattern=r"^run_[a-f0-9]{16}$")]
EventId = Annotated[str, StringConstraints(pattern=r"^e_[A-Za-z0-9_-]{1,48}$")]
SnapshotId = Annotated[str, StringConstraints(pattern=r"^snap_[a-f0-9]{12}$")]

class RunMode(StrEnum):
    DISCOVERY = "DISCOVERY"
    VALIDATION = "VALIDATION"
    REPLAY = "REPLAY"


class RunState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    FAILURE = "FAILURE"
    ABORTED = "ABORTED"
    SESSION_LOST = "SESSION_LOST"


EventType = Literal[
    "RUN_CREATED",
    "RUN_STATE_CHANGED",
    "OBSERVATION_CAPTURED",
    "ACTION_REJECTED",
    "ACTION_DISPATCHED",
    "ACTION_EFFECT_VERIFIED",
    "ACTION_OUTCOME_UNKNOWN",
    "EVIDENCE_CAPTURED",
    "RUN_FINISHED",
]
EffectState = Literal["NOT_DISPATCHED", "DISPATCHED", "VERIFIED", "OUTCOME_UNKNOWN"]
class SafeReasonCode(StrEnum):
    RUN_CREATED = "RUN_CREATED"
    STARTED = "STARTED"
    AUTHORIZED = "AUTHORIZED"
    POLICY_DENIED = "POLICY_DENIED"
    RISK_NOT_READ_ONLY = "RISK_NOT_READ_ONLY"
    ORIGIN_NOT_ALLOWED = "ORIGIN_NOT_ALLOWED"
    ROUTE_NOT_ALLOWED = "ROUTE_NOT_ALLOWED"
    OPERATION_NOT_ALLOWED = "OPERATION_NOT_ALLOWED"
    CONTROL_OPERATION_NOT_ALLOWED = "CONTROL_OPERATION_NOT_ALLOWED"
    INVALID_POLICY_CONTEXT = "INVALID_POLICY_CONTEXT"
    INVALID_RUNTIME_CLASSIFICATION = "INVALID_RUNTIME_CLASSIFICATION"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"
    SESSION_LOST = "SESSION_LOST"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    ACTOR_PAUSED = "ACTOR_PAUSED"
    STALE_EPOCH = "STALE_EPOCH"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    TARGET_NOT_UNIQUE = "TARGET_NOT_UNIQUE"
    TARGET_NOT_ACTIONABLE = "TARGET_NOT_ACTIONABLE"
    ACTION_NOT_SUPPORTED = "ACTION_NOT_SUPPORTED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    PRECONDITION_UNKNOWN = "PRECONDITION_UNKNOWN"
    POSTCONDITION_FAILED = "POSTCONDITION_FAILED"
    POSTCONDITION_UNKNOWN = "POSTCONDITION_UNKNOWN"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
    APP_ERROR = "APP_ERROR"
    ACCESS_DENIED = "ACCESS_DENIED"
    AMBIGUOUS_STATE = "AMBIGUOUS_STATE"
    UNKNOWN_BLOCKER = "UNKNOWN_BLOCKER"
    ACCOUNT_TYPE_MISMATCH = "ACCOUNT_TYPE_MISMATCH"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    AMBIGUOUS_AMOUNT = "AMBIGUOUS_AMOUNT"
    DETAIL_NOT_READY = "DETAIL_NOT_READY"
    PRINCIPAL_UNKNOWN = "PRINCIPAL_UNKNOWN"
    ORIGIN_MISMATCH = "ORIGIN_MISMATCH"
    AUTHENTICATION_CHANGED = "AUTHENTICATION_CHANGED"
    MEMBERSHIP_PROOF_INVALID = "MEMBERSHIP_PROOF_INVALID"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
    INPUT_INVALID = "INPUT_INVALID"
    INVALID_INPUT = "INPUT_INVALID"
    INVALID_BUNDLE = "INVALID_BUNDLE"
    EVIDENCE_ERROR = "EVIDENCE_ERROR"
    DISCOVERY_COMPLETE = "DISCOVERY_COMPLETE"
    REPLAY_COMPLETE = "REPLAY_COMPLETE"
    REPLAY_FAILED = "REPLAY_FAILED"
    VALIDATION_COMPLETE = "VALIDATION_COMPLETE"
    ORACLE_MISMATCH = "ORACLE_MISMATCH"
    ORACLE_UNAVAILABLE = "ORACLE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
Role = Literal[
    "link",
    "button",
    "textbox",
    "combobox",
    "checkbox",
    "radio",
    "tab",
    "heading",
    "generic",
    "other",
]
ControlOperation = Literal["CLICK", "TYPE_TEXT", "SELECT", "SCROLL"]
SafePageState = Literal[
    "AUTHENTICATED_HOME",
    "OVERVIEW_LOADING",
    "OVERVIEW_READY",
    "DETAIL_READY",
    "LOGIN_READY",
    "LOGIN",
    "APP_ERROR",
    "ACCESS_DENIED",
    "UNKNOWN",
]
SafeRoute = Literal["home", "accounts_overview", "account_details", "UNKNOWN"]
SafeProfile = Literal["parabank-native-v1", "UNKNOWN"]


class RunMetadata(StrictModel):
    """Explicitly safe persistent metadata; goal, inputs, and session IDs are absent."""

    mode: RunMode
    capability_name: Name | None
    capability_version: Revision | None
    bundle_digest: Sha256 | None
    profile_id: ProfileId
    target_revision: GitRevision
    browser_version: SafeVersion
    model_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,96}$")] | None = None


class SafeEvent(StrictModel):
    event_id: EventId
    run_alias: RunAlias
    event_type: EventType
    step_id: Name | None = None
    state: RunState | None = None
    reason_code: SafeReasonCode | None = None
    effect_state: EffectState | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    control_ref: ControlRef | None = None


class SafeControlSummary(StrictModel):
    control_ref: ControlRef
    role: Role
    visible: bool
    enabled: bool
    allowed_operations: tuple[ControlOperation, ...] = Field(max_length=4)


class SafeSnapshot(StrictModel):
    snapshot_id: SnapshotId
    profile_id: SafeProfile
    safe_route: SafeRoute
    page_state: SafePageState
    control_count: int = Field(ge=0, le=10000)
    visible_control_count: int = Field(ge=0, le=10000)
    controls_truncated: bool
    controls: tuple[SafeControlSummary, ...] = Field(max_length=64)


class EvidenceRef(StrictModel):
    snapshot_id: SnapshotId
    relative_path: Annotated[
        str,
        StringConstraints(pattern=r"^safe_snapshots/snap_[a-f0-9]{12}\.json$"),
    ]
