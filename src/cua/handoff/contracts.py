"""Safe in-memory contracts for an operator handoff.

The credential and reconciler context in this module are deliberately private
objects. They are delivered through an injected trusted callback and are never
part of an intervention view or a serialized request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Awaitable, Callable, Mapping, Protocol

from cua.evidence.models import SafeReasonCode
from cua.sessions.actor import SessionActor


_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,96}$", re.ASCII)
_STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)


class HandoffState(StrEnum):
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    HUMAN_CLAIMED = "HUMAN_CLAIMED"
    RESUMING = "RESUMING"
    RUNNING = "RUNNING"
    ABORTED = "ABORTED"
    TIMED_OUT = "TIMED_OUT"


class ReconciliationDisposition(StrEnum):
    NEXT = "NEXT"
    RETRY_SAFE = "RETRY_SAFE"
    REMAIN_PAUSED = "REMAIN_PAUSED"


class HandoffError(RuntimeError):
    """Redacted, deterministic handoff failure."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code, re.ASCII):
            raise ValueError("handoff error code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class InterventionCredential:
    """Opaque token delivered only by the trusted delivery callback."""

    intervention_id: str
    session_id: str
    epoch: int
    _token: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_id(self.intervention_id, "intervention id")
        _require_id(self.session_id, "session id")
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("intervention epoch is invalid")
        if not self._token or len(self._token) > 256:
            raise ValueError("intervention token is invalid")

    def __repr__(self) -> str:
        return (
            "InterventionCredential("
            f"intervention_id={self.intervention_id!r}, "
            f"session_id={self.session_id!r}, epoch={self.epoch}, token='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class InterventionView:
    """Safe operator-facing state; it contains no account or credential data."""

    intervention_id: str
    session_id: str
    run_id: str
    step_id: str
    reason: SafeReasonCode
    state: HandoffState
    owner: str
    epoch: int
    page_id: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.intervention_id, "intervention id"),
            (self.session_id, "session id"),
            (self.run_id, "run id"),
            (self.page_id, "page id"),
        ):
            _require_id(value, name)
        if not _STEP_ID.fullmatch(self.step_id):
            raise ValueError("step id is invalid")
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("intervention epoch is invalid")
        if self.owner not in {"NONE", "HUMAN", "RESUMING", "RUNTIME"}:
            raise ValueError("intervention owner is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ReconciliationContext:
    """Private callback context for actor-serialized identity/postcondition work."""

    intervention_id: str
    session_id: str
    run_id: str
    epoch: int
    actor: SessionActor = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_id(self.intervention_id, "intervention id")
        _require_id(self.session_id, "session id")
        _require_id(self.run_id, "run id")
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("reconciliation epoch is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ReconciliationResult:
    """Private result returned by a trusted reconciler.

    ``context`` may contain protected fresh browser facts. The handoff service
    never copies it into an intervention view or terminal result.
    """

    disposition: ReconciliationDisposition
    context: object | None = field(default=None, repr=False)
    reason: SafeReasonCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, ReconciliationDisposition):
            raise ValueError("reconciliation disposition must be typed")
        if self.reason is not None and not isinstance(self.reason, SafeReasonCode):
            raise ValueError("reconciliation reason must be safe")


@dataclass(frozen=True, slots=True)
class HandoffResult:
    intervention_id: str
    session_id: str
    run_id: str
    state: HandoffState
    epoch: int
    disposition: ReconciliationDisposition | None = None
    reason: SafeReasonCode | None = None
    context: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_id(self.intervention_id, "intervention id")
        _require_id(self.session_id, "session id")
        _require_id(self.run_id, "run id")
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("handoff result epoch is invalid")


TrustedReconciler = Callable[[ReconciliationContext], Awaitable[ReconciliationResult]]
TokenDelivery = Callable[[InterventionCredential], Awaitable[None]]
OperatorAuthorizer = Callable[[str], Awaitable[bool] | bool]
StateCallback = Callable[[InterventionView], Awaitable[None] | None]


def _require_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{name} is invalid")
