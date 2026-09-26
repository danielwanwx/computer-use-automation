"""Deterministic, zero-model replay over approved capability bundles."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import time
from typing import Protocol

from pydantic import SecretStr, ValidationError

from cua.conditions.evaluator import TriState
from cua.evidence import EvidenceRef, EvidenceSink, SafeReasonCode
from cua.execution import (
    EffectState,
    ExecutionContext,
    ExecutionGateway,
    ExecutionResult,
    FailureDetail,
    InvocationResult,
    InvocationStatus,
)
from cua.models.bundles import (
    BundleReference,
    BundleStep,
    CapabilityBundle,
    RecoveryDefinition,
)
from cua.models.conditions import (
    AccountPresentCondition,
    AllCondition,
    ConditionReference,
    OverviewCompleteCondition,
    PrincipalMatchesCondition,
)
from cua.models.verification import CompletionContext
from cua.models.verification import MembershipProof
from cua.registry import (
    BundleNotFoundError,
    BundleRegistry,
    DigestMismatchError,
    ImmutableRevisionError,
    InvalidBundleError,
    QualificationMismatchError,
)
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from cua.handoff.contracts import (
    HandoffResult,
    HandoffState,
    ReconciliationContext,
    ReconciliationDisposition,
    ReconciliationResult,
    TrustedReconciler,
)
from cua.sessions.actor import ActorBusy, ActorPaused, ActorStaleEpoch, SessionActor
from cua.sessions.manager import SessionError, SessionManager, ValidationSessionBinding
from cua.surface import NormalizedView, PlaywrightSurface, SurfaceError
from cua.verification import CompletionVerifier, VerificationResult, VerificationStatus


_ACCOUNT_ID_PATTERN = re.compile(r"^[0-9]{1,20}$", re.ASCII)
_RUN_ID_PATTERN = re.compile(r"^run_[a-f0-9]{16}$", re.ASCII)
_MAX_SAFE_RETRIES = 2
_DEFAULT_CONDITION_TIMEOUT_SECONDS = 5.0
_DEFAULT_STEP_POLL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class HandoffRequest:
    """Value-free notice for an application-owned manual intervention adapter."""

    run_id: str
    session_id: str
    step_id: str
    reason_code: SafeReasonCode
    ownership_epoch: int


@dataclass(frozen=True, slots=True, repr=False)
class TrustedHandoffContext:
    """Private handoff inputs kept outside the value-free operator request.

    The replay coordinator receives this object through an in-process keyword
    argument.  It is never serialized, rendered in an intervention view, or
    passed to an operator.  Step position and retry counters therefore remain
    runtime state while the public request stays safe and compact.
    """

    actor: SessionActor = field(repr=False, compare=False)
    reconciler: TrustedReconciler = field(repr=False, compare=False)
    page_id: str
    deadline_monotonic: float = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.deadline_monotonic < 0:
            raise ValueError("handoff deadline is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ReplayResumeContext:
    """Private, in-process replay state returned by a trusted reconciler.

    The object is deliberately separate from ``HandoffRequest`` and
    ``InterventionView``.  It can carry a fresh authentication generation and
    membership proof back to the deterministic replay loop, but it is never
    safe to render or serialize for an operator.
    """

    context: ExecutionContext = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.context, ExecutionContext):
            raise TypeError("replay resume context must hold an ExecutionContext")

    def __repr__(self) -> str:
        return "ReplayResumeContext(context='<protected>')"


class HandoffNotifier(Protocol):
    async def require_human(
        self,
        request: HandoffRequest,
        *,
        trusted: TrustedHandoffContext,
    ) -> HandoffResult | None: ...


@dataclass(frozen=True, slots=True)
class _CurrentObservation:
    observation: object
    view: NormalizedView
    evidence_ref: EvidenceRef


class _ReplayStop(Exception):
    def __init__(
        self,
        reason_code: SafeReasonCode,
        step_id: str | None = None,
        effect_state: EffectState = EffectState.NOT_DISPATCHED,
    ) -> None:
        self.reason_code = reason_code
        self.step_id = step_id
        self.effect_state = effect_state
        super().__init__(reason_code.value)


class ReplayRuntime:
    """Runs approved steps serially with a shared deadline and bounded recovery.

    The runtime accepts no DecisionBackend/model dependency. Only CLICK and EXTRACT
    pass through ExecutionGateway; ASSERT, WAIT, and VERIFY are read-only checks.
    """

    def __init__(
        self,
        registry: BundleRegistry,
        sessions: SessionManager,
        surface: PlaywrightSurface,
        gateway: ExecutionGateway,
        verifier: CompletionVerifier,
        evidence: EvidenceSink,
        *,
        target_revision: str,
        run_timeout_seconds: float = 180.0,
        poll_interval_seconds: float = _DEFAULT_STEP_POLL_SECONDS,
        handoff_notifier: HandoffNotifier | None = None,
    ) -> None:
        if re.fullmatch(r"[a-f0-9]{40}", target_revision, re.ASCII) is None:
            raise ValueError("target revision pin is invalid")
        if not 0 < run_timeout_seconds <= 1800:
            raise ValueError("run timeout must be between 0 and 1800 seconds")
        if not 0 < poll_interval_seconds <= 1:
            raise ValueError("poll interval must be between 0 and 1 second")
        self._registry = registry
        self._sessions = sessions
        self._surface = surface
        self._gateway = gateway
        self._verifier = verifier
        self._evidence = evidence
        self._target_revision = target_revision
        self._run_timeout_seconds = run_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._handoff_notifier = handoff_notifier

    async def run(
        self,
        reference: BundleReference,
        context: ExecutionContext,
    ) -> InvocationResult:
        """Load approval, execute steps, and expose outputs only after final VERIFY."""
        return await self._run(reference, context, validation_mode=False, validation_binding=None)

    async def run_validation(
        self,
        reference: BundleReference,
        context: ExecutionContext,
        test_session_binding: ValidationSessionBinding,
    ) -> InvocationResult:
        """Replay a static DRAFT through the same executor using trusted test-session authority."""
        return await self._run(
            reference,
            context,
            validation_mode=True,
            validation_binding=test_session_binding,
        )

    async def _run(
        self,
        reference: BundleReference,
        context: ExecutionContext,
        *,
        validation_mode: bool,
        validation_binding: ValidationSessionBinding | None,
    ) -> InvocationResult:
        run_id = getattr(context, "run_alias", "")
        if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise ValueError("a valid registered run alias is required")
        try:
            context = ExecutionContext.model_validate(context.model_dump(mode="python"))
            reference = BundleReference.model_validate(reference.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            return self._failure(
                run_id,
                SafeReasonCode.INVALID_INPUT,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )

        if not _valid_input_bindings(context):
            return self._failure(
                context.run_alias,
                SafeReasonCode.INVALID_INPUT,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        deadline = time.monotonic() + self._run_timeout_seconds
        if context.deadline_monotonic is not None:
            deadline = min(deadline, context.deadline_monotonic)
        context = context.model_copy(update={"deadline_monotonic": deadline})
        evidence_refs: list[EvidenceRef] = []
        membership_proof = context.membership_proof
        retry_counts: dict[str, int] = {}

        try:
            session_state = await self._sessions.get_state(context.session_id)
            if (
                session_state.state != "ACTIVE"
                or session_state.profile_id != context.profile_id
                or session_state.auth_generation != context.authentication_generation
                or session_state.origin != context.target_origin
                or session_state.actor.active_run_id != context.run_alias
            ):
                return self._failure(
                    context.run_alias,
                    SafeReasonCode.SESSION_LOST,
                    step_id=None,
                    effect_state=EffectState.NOT_DISPATCHED,
                )
            qualification = {
                "runtime_fingerprint": current_runtime_fingerprint(),
                "browser_version": session_state.browser_version,
                "target_revision": self._target_revision,
            }
            if validation_mode:
                bundle = self._registry.prepare_validation(reference, **qualification)
            else:
                bundle = self._registry.prepare_execution(reference, **qualification)
            bundle = CapabilityBundle.model_validate(bundle.model_dump(mode="python"))
        except (ActorBusy, ActorPaused, ActorStaleEpoch):
            return self._failure(
                context.run_alias,
                SafeReasonCode.STALE_EPOCH,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        except SessionError as error:
            return self._failure(
                context.run_alias,
                _session_reason(error.code),
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        except (
            BundleNotFoundError,
            DigestMismatchError,
            ImmutableRevisionError,
            InvalidBundleError,
            QualificationMismatchError,
            AttributeError,
            ValidationError,
            OSError,
            ValueError,
        ):
            return self._failure(
                context.run_alias,
                SafeReasonCode.INVALID_BUNDLE,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )

        if not bundle.provenance.verified or not bundle.provenance.completion_proof_id:
            return self._failure(
                context.run_alias,
                SafeReasonCode.INVALID_BUNDLE,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        if not _supported_runtime_contract(bundle):
            return self._failure(
                context.run_alias,
                SafeReasonCode.INVALID_BUNDLE,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        if not _valid_contract_inputs(bundle, context):
            return self._failure(
                context.run_alias,
                SafeReasonCode.INVALID_INPUT,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        if not bundle.steps or bundle.steps[-1].kind != "VERIFY":
            return self._failure(
                context.run_alias,
                SafeReasonCode.INVALID_BUNDLE,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        if any(step.kind == "VERIFY" for step in bundle.steps[:-1]):
            return self._failure(
                context.run_alias,
                SafeReasonCode.INVALID_BUNDLE,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        if any(
            step.kind not in {"CLICK", "EXTRACT", "ASSERT", "WAIT", "VERIFY"}
            for step in bundle.steps
        ):
            return self._failure(
                context.run_alias,
                SafeReasonCode.ACTION_NOT_SUPPORTED,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )

        if time.monotonic() >= deadline:
            return self._failure(
                context.run_alias,
                SafeReasonCode.RECOVERY_EXHAUSTED,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )

        if validation_mode and (
            not isinstance(validation_binding, ValidationSessionBinding)
            or not self._sessions.consume_validation_session_binding(
                validation_binding,
                context.session_id,
            )
        ):
            return self._failure(
                context.run_alias,
                SafeReasonCode.INVALID_BUNDLE,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )

        try:
            handle = await session_state.actor.submit(
                expected_epoch=context.expected_epoch,
                run_id=context.run_alias,
                operation=lambda: self._read_session_handle(context.session_id),
            )
        except (ActorBusy, ActorPaused, ActorStaleEpoch):
            return self._failure(
                context.run_alias,
                SafeReasonCode.STALE_EPOCH,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        except SessionError as error:
            return self._failure(
                context.run_alias,
                _session_reason(error.code),
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        except Exception:
            return self._failure(
                context.run_alias,
                SafeReasonCode.INTERNAL_ERROR,
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )
        if (
            handle.profile_id != context.profile_id
            or handle.auth_generation != context.authentication_generation
            or handle.origin != context.target_origin
            or handle.browser_version != session_state.browser_version
        ):
            return self._failure(
                context.run_alias,
                _handle_mismatch_reason(handle, context),
                step_id=None,
                effect_state=EffectState.NOT_DISPATCHED,
            )

        # Keep the position explicit so a handoff can resume the exact step.
        # A retry never advances this index; NEXT advances only after the
        # trusted coordinator has verified the intervening postcondition.
        step_index = 0
        while step_index < len(bundle.steps):
            step = bundle.steps[step_index]
            if time.monotonic() >= deadline:
                return await self._finish_failure(
                    context,
                    SafeReasonCode.RECOVERY_EXHAUSTED,
                    step.id,
                    EffectState.NOT_DISPATCHED,
                    tuple(evidence_refs),
                )
            if step.kind == "VERIFY":
                if step_index != len(bundle.steps) - 1:
                    return self._failure(
                        context.run_alias,
                        SafeReasonCode.INVALID_BUNDLE,
                        step_id=step.id,
                        effect_state=EffectState.NOT_DISPATCHED,
                        evidence_refs=tuple(evidence_refs),
                    )
                return await self._verify_completion(
                    context,
                    bundle,
                    step,
                    membership_proof,
                    deadline,
                    tuple(evidence_refs),
                )
            if step.kind == "ASSERT":
                result = await self._assert_step(context, bundle, step, membership_proof)
            elif step.kind == "WAIT":
                result = await self._wait_step(context, bundle, step, membership_proof, deadline)
            elif step.kind in {"CLICK", "EXTRACT"}:
                result = await self._run_action_step(
                    context,
                    bundle,
                    step,
                    membership_proof,
                    retry_counts,
                    deadline,
                )
            else:
                result = ExecutionResult(
                    step_id=step.id,
                    effect_state=EffectState.NOT_DISPATCHED,
                    reason_code=SafeReasonCode.ACTION_NOT_SUPPORTED,
                )

            evidence_refs.extend(result.evidence_refs)
            if result.membership_proof is not None:
                membership_proof = result.membership_proof
                context = context.model_copy(update={"membership_proof": membership_proof})
            if result.effect_state is not EffectState.VERIFIED:
                reason = result.reason_code
                if (
                    reason in {
                        SafeReasonCode.ACCOUNT_NOT_FOUND,
                        SafeReasonCode.ACCESS_DENIED,
                    }
                    and reason.value in bundle.contract.business_outcomes
                ):
                    return InvocationResult(
                        run_id=context.run_alias,
                        status=InvocationStatus.BUSINESS_OUTCOME,
                        code=reason,
                        evidence_refs=tuple(evidence_refs),
                    )
                handoff = None
                if _requires_handoff(result):
                    handoff = await self._notify_handoff(
                        context,
                        bundle,
                        step,
                        membership_proof,
                        result,
                        step.id,
                        reason,
                        actor=session_state.actor,
                        page_id=handle.page_id,
                        deadline=deadline,
                    )
                if handoff is not None:
                    if handoff.epoch < context.expected_epoch:
                        return self._failure(
                            context.run_alias,
                            SafeReasonCode.STALE_EPOCH,
                            step_id=step.id,
                            effect_state=result.effect_state,
                            evidence_refs=tuple(evidence_refs),
                        )
                    resumed_context, context_error = _apply_resume_context(
                        context,
                        handoff,
                    )
                    if context_error is not None:
                        return self._failure(
                            context.run_alias,
                            context_error,
                            step_id=step.id,
                            effect_state=result.effect_state,
                            evidence_refs=tuple(evidence_refs),
                        )
                    # A legacy trusted notifier may not return protected
                    # state.  In that case only the ownership epoch advances;
                    # all other run state remains exactly as it was.
                    assert resumed_context is not None
                    context = resumed_context
                    if handoff.context is not None:
                        membership_proof = context.membership_proof
                    if (
                        handoff.state is HandoffState.RUNNING
                        and handoff.disposition is ReconciliationDisposition.NEXT
                    ):
                        step_index += 1
                        continue
                    if (
                        handoff.state is HandoffState.RUNNING
                        and handoff.disposition is ReconciliationDisposition.RETRY_SAFE
                    ):
                        retry_limit = _handoff_retry_limit(bundle, step)
                        attempts = retry_counts.get(step.id, 0)
                        if attempts >= retry_limit:
                            return self._failure(
                                context.run_alias,
                                SafeReasonCode.RECOVERY_EXHAUSTED,
                                step_id=step.id,
                                effect_state=result.effect_state,
                                evidence_refs=tuple(evidence_refs),
                            )
                        retry_counts[step.id] = attempts + 1
                        continue
                    # REMAIN_PAUSED, ABORTED, and TIMED_OUT do not allow the
                    # runtime to reacquire the browser lease implicitly.
                    if handoff.state in {
                        HandoffState.ABORTED,
                        HandoffState.TIMED_OUT,
                    }:
                        return self._aborted(
                            context.run_alias,
                            handoff.reason or reason,
                            step_id=step.id,
                            effect_state=result.effect_state,
                            evidence_refs=tuple(evidence_refs),
                        )
                return self._failure(
                    context.run_alias,
                    reason,
                    step_id=step.id,
                    effect_state=result.effect_state,
                    evidence_refs=tuple(evidence_refs),
                )

            step_index += 1

        return self._failure(
            context.run_alias,
            SafeReasonCode.INVALID_BUNDLE,
            step_id=None,
            effect_state=EffectState.NOT_DISPATCHED,
            evidence_refs=tuple(evidence_refs),
        )

    async def _run_action_step(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        membership_proof,
        retry_counts: dict[str, int],
        deadline: float,
    ) -> ExecutionResult:
        while True:
            if time.monotonic() >= deadline:
                return _step_result(
                    step.id,
                    EffectState.OUTCOME_UNKNOWN,
                    SafeReasonCode.RECOVERY_EXHAUSTED,
                    membership_proof=membership_proof,
                )
            context = context.model_copy(update={"membership_proof": membership_proof})
            result = await self._gateway.dispatch(context, bundle, step.id)
            if result.membership_proof is not None:
                membership_proof = result.membership_proof
            if result.effect_state is EffectState.VERIFIED:
                return result

            attempts = retry_counts.get(step.id, 0)
            if (
                result.effect_state is EffectState.NOT_DISPATCHED
                and result.reason_code is SafeReasonCode.PRECONDITION_UNKNOWN
                and attempts < _MAX_SAFE_RETRIES
            ):
                wait_result = await self._wait_until_preconditions(
                    context, bundle, step, membership_proof, deadline
                )
                if wait_result is None:
                    retry_counts[step.id] = attempts + 1
                    continue
                return _step_result(
                    step.id,
                    wait_result.effect_state,
                    wait_result.reason_code,
                    observation_id=wait_result.observation_id,
                    evidence_refs=wait_result.evidence_refs,
                    membership_proof=membership_proof,
                )

            if result.effect_state is not EffectState.OUTCOME_UNKNOWN:
                return result

            reconciliation = await self._gateway.reconcile_unknown(
                context,
                bundle,
                step.id,
                membership_proof,
            )
            if reconciliation.membership_proof is not None:
                membership_proof = reconciliation.membership_proof
            if reconciliation.effect_state is EffectState.VERIFIED:
                return reconciliation
            recovery = next(
                (item for item in bundle.recoveries if item.ref == step.recovery_ref),
                None,
            )
            if not reconciliation.retry_safe or recovery is None:
                return reconciliation
            attempts = retry_counts.get(step.id, 0)
            if attempts >= recovery.max_attempts:
                return _step_result(
                    step.id,
                    EffectState.OUTCOME_UNKNOWN,
                    SafeReasonCode.RECOVERY_EXHAUSTED,
                    observation_id=reconciliation.observation_id,
                    evidence_refs=reconciliation.evidence_refs,
                    membership_proof=membership_proof,
                )

            anchor_step = _recovery_anchor_step(bundle, recovery)
            if anchor_step is None:
                return _step_result(
                    step.id,
                    EffectState.NOT_DISPATCHED,
                    SafeReasonCode.INVALID_BUNDLE,
                    membership_proof=membership_proof,
                )
            anchor = await self._gateway.dispatch(context, bundle, anchor_step.id)
            if anchor.effect_state is not EffectState.VERIFIED:
                return _step_result(
                    step.id,
                    anchor.effect_state,
                    anchor.reason_code,
                    observation_id=anchor.observation_id,
                    evidence_refs=anchor.evidence_refs,
                    membership_proof=membership_proof,
                )
            if anchor.membership_proof is not None:
                membership_proof = anchor.membership_proof

            membership_step = _membership_assertion_step(bundle)
            if target_is_account_binding(bundle, step):
                if membership_step is None:
                    return _step_result(
                        step.id,
                        EffectState.NOT_DISPATCHED,
                        SafeReasonCode.INVALID_BUNDLE,
                        membership_proof=membership_proof,
                    )
                assertion = await self._assert_step(
                    context.model_copy(update={"membership_proof": membership_proof}),
                    bundle,
                    membership_step,
                    membership_proof,
                )
                if assertion.effect_state is not EffectState.VERIFIED:
                    return assertion
                membership_proof = assertion.membership_proof or membership_proof

            retry_counts[step.id] = attempts + 1

    async def _wait_until_preconditions(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        membership_proof,
        run_deadline: float,
    ) -> ExecutionResult | None:
        deadline = min(run_deadline, time.monotonic() + _DEFAULT_CONDITION_TIMEOUT_SECONDS)
        last: _CurrentObservation | None = None
        while time.monotonic() < deadline:
            try:
                last = await self._observe_current(context, step.id, deadline)
            except _ReplayStop as stop:
                return _step_result(
                    step.id,
                    EffectState.NOT_DISPATCHED,
                    stop.reason_code,
                    membership_proof=membership_proof,
                )
            current_context = context.model_copy(
                update={"membership_proof": membership_proof}
            )
            reason = self._gateway.evaluate_conditions(
                current_context,
                bundle,
                step.preconditions,
                last.view,
                membership_proof,
            )
            if reason is None and _expected_source_satisfied(step, bundle, last.view):
                return None
            if reason is SafeReasonCode.PRECONDITION_FAILED:
                missing = _account_not_found(bundle, step, last.view, current_context)
                if missing:
                    return _step_result(
                        step.id,
                        EffectState.NOT_DISPATCHED,
                        SafeReasonCode.ACCOUNT_NOT_FOUND,
                        observation_id=last.observation.id,
                        evidence_refs=(last.evidence_ref,),
                        membership_proof=membership_proof,
                    )
                return _step_result(
                    step.id,
                    EffectState.NOT_DISPATCHED,
                    reason,
                    observation_id=last.observation.id,
                    evidence_refs=(last.evidence_ref,),
                    membership_proof=membership_proof,
                )
            await asyncio.sleep(min(self._poll_interval_seconds, max(0, deadline - time.monotonic())))
        return _step_result(
            step.id,
            EffectState.NOT_DISPATCHED,
            SafeReasonCode.PRECONDITION_UNKNOWN,
            observation_id=last.observation.id if last else None,
            evidence_refs=(last.evidence_ref,) if last else (),
            membership_proof=membership_proof,
        )

    async def _assert_step(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        membership_proof,
    ) -> ExecutionResult:
        try:
            current = await self._observe_current(
                context,
                step.id,
                context.deadline_monotonic,
            )
        except _ReplayStop as stop:
            return _step_result(step.id, EffectState.NOT_DISPATCHED, stop.reason_code)
        reason = self._gateway.evaluate_conditions(
            context,
            bundle,
            step.preconditions,
            current.view,
            membership_proof,
        )
        if reason is not None:
            if reason is SafeReasonCode.PRECONDITION_FAILED and _account_not_found(
                bundle, step, current.view, context
            ):
                reason = SafeReasonCode.ACCOUNT_NOT_FOUND
            return _step_result(
                step.id,
                EffectState.NOT_DISPATCHED,
                reason,
                observation_id=current.observation.id,
                evidence_refs=(current.evidence_ref,),
                membership_proof=membership_proof,
            )
        if _is_membership_assertion(step, bundle):
            account_value = context.input_bindings.get("inputs.account_id")
            if account_value is None:
                return _step_result(
                    step.id,
                    EffectState.NOT_DISPATCHED,
                    SafeReasonCode.INVALID_INPUT,
                    observation_id=current.observation.id,
                    evidence_refs=(current.evidence_ref,),
                )
            try:
                membership_proof = current.view.membership_proof(
                    run_ref=context.run_alias,
                    binding_ref="inputs.account_id",
                    binding_value=account_value,
                )
            except SurfaceError:
                return _step_result(
                    step.id,
                    EffectState.NOT_DISPATCHED,
                    SafeReasonCode.MEMBERSHIP_PROOF_INVALID,
                    observation_id=current.observation.id,
                    evidence_refs=(current.evidence_ref,),
                )
        self._emit_step(
            context,
            step.id,
            SafeReasonCode.AUTHORIZED,
            EffectState.VERIFIED,
        )
        return _step_result(
            step.id,
            EffectState.VERIFIED,
            SafeReasonCode.AUTHORIZED,
            observation_id=current.observation.id,
            evidence_refs=(current.evidence_ref,),
            membership_proof=membership_proof,
        )

    async def _wait_step(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        membership_proof,
        run_deadline: float,
    ) -> ExecutionResult:
        timeout = (step.timeout_ms or 0) / 1000
        deadline = min(run_deadline, time.monotonic() + timeout)
        last: _CurrentObservation | None = None
        while True:
            try:
                last = await self._observe_current(context, step.id, deadline)
            except _ReplayStop as stop:
                return _step_result(
                    step.id,
                    EffectState.NOT_DISPATCHED,
                    stop.reason_code,
                    membership_proof=membership_proof,
                )
            precondition = self._gateway.evaluate_conditions(
                context, bundle, step.preconditions, last.view, membership_proof
            )
            if precondition is not None:
                return _step_result(
                    step.id,
                    EffectState.NOT_DISPATCHED,
                    precondition,
                    observation_id=last.observation.id,
                    evidence_refs=(last.evidence_ref,),
                    membership_proof=membership_proof,
                )
            postcondition = self._gateway.evaluate_conditions(
                context, bundle, step.postconditions, last.view, membership_proof
            )
            if postcondition is None:
                self._emit_step(context, step.id, SafeReasonCode.AUTHORIZED, EffectState.VERIFIED)
                return _step_result(
                    step.id,
                    EffectState.VERIFIED,
                    SafeReasonCode.AUTHORIZED,
                    observation_id=last.observation.id,
                    evidence_refs=(last.evidence_ref,),
                    membership_proof=membership_proof,
                )
            if time.monotonic() >= deadline:
                return _step_result(
                    step.id,
                    EffectState.OUTCOME_UNKNOWN,
                    SafeReasonCode.POSTCONDITION_UNKNOWN,
                    observation_id=last.observation.id,
                    evidence_refs=(last.evidence_ref,),
                    membership_proof=membership_proof,
                )
            await asyncio.sleep(min(self._poll_interval_seconds, deadline - time.monotonic()))

    async def _verify_completion(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        membership_proof,
        deadline: float,
        evidence_refs: tuple[EvidenceRef, ...],
    ) -> InvocationResult:
        while time.monotonic() < deadline:
            try:
                current, verification = await self._verify_once_in_actor(
                    context,
                    bundle,
                    membership_proof,
                    deadline,
                    step.id,
                )
            except _ReplayStop as stop:
                return self._failure(
                    context.run_alias,
                    stop.reason_code,
                    step_id=step.id,
                    effect_state=stop.effect_state,
                    evidence_refs=evidence_refs,
                )
            evidence_refs = (*evidence_refs, current.evidence_ref)
            if verification is None:
                await asyncio.sleep(
                    min(self._poll_interval_seconds, max(0, deadline - time.monotonic()))
                )
                continue
            if verification.status is VerificationStatus.SUCCESS:
                return InvocationResult(
                    run_id=context.run_alias,
                    status=InvocationStatus.SUCCESS,
                    outputs=verification.outputs,
                    code=SafeReasonCode.REPLAY_COMPLETE,
                    evidence_refs=evidence_refs,
                )
            if verification.status is VerificationStatus.UNKNOWN:
                await asyncio.sleep(
                    min(self._poll_interval_seconds, max(0, deadline - time.monotonic()))
                )
                continue
            reason = _safe_reason(verification.reason_code, SafeReasonCode.REPLAY_FAILED)
            return self._failure(
                context.run_alias,
                reason,
                step_id=step.id,
                effect_state=EffectState.NOT_DISPATCHED,
                evidence_refs=evidence_refs,
            )
        return self._failure(
            context.run_alias,
            SafeReasonCode.RECOVERY_EXHAUSTED,
            step_id=step.id,
            effect_state=EffectState.NOT_DISPATCHED,
            evidence_refs=evidence_refs,
        )

    async def _verify_once_in_actor(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        membership_proof,
        deadline: float,
        step_id: str,
    ) -> tuple[_CurrentObservation, VerificationResult | None]:
        if membership_proof is None:
            raise _ReplayStop(SafeReasonCode.MEMBERSHIP_PROOF_INVALID, step_id)
        if time.monotonic() >= deadline:
            raise _ReplayStop(SafeReasonCode.RECOVERY_EXHAUSTED, step_id)
        try:
            state = await self._sessions.get_state(context.session_id)
        except SessionError as error:
            raise _ReplayStop(_session_reason(error.code), step_id) from None
        if (
            state.state != "ACTIVE"
            or state.profile_id != context.profile_id
            or state.auth_generation != context.authentication_generation
            or state.origin != context.target_origin
            or state.actor.active_run_id != context.run_alias
        ):
            raise _ReplayStop(SafeReasonCode.SESSION_LOST, step_id)

        async def verify_in_actor():
            session = await self._sessions.get(context.session_id)
            handle = session.handle
            if session.actor is not state.actor or session.state != "ACTIVE":
                raise _ReplayStop(SafeReasonCode.SESSION_LOST, step_id)
            mismatch = _handle_mismatch_reason(handle, context)
            if mismatch is not None:
                raise _ReplayStop(mismatch, step_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ReplayStop(SafeReasonCode.RECOVERY_EXHAUSTED, step_id)
            observation, view = await asyncio.wait_for(
                self._surface.observe(context.session_id, bindings=context.input_bindings),
                timeout=remaining,
            )
            evidence_ref = self._gateway._capture(context, observation, view)
            if evidence_ref is None:
                raise _ReplayStop(SafeReasonCode.EVIDENCE_ERROR, step_id)
            if not _view_matches_session(observation, view, handle, context):
                raise _ReplayStop(_view_mismatch_reason(view, context), step_id)
            if view.principal_matches is None:
                raise _ReplayStop(SafeReasonCode.PRINCIPAL_UNKNOWN, step_id)
            if view.principal_matches is not True:
                raise _ReplayStop(SafeReasonCode.SUBJECT_MISMATCH, step_id)
            current = _CurrentObservation(observation, view, evidence_ref)
            if view.detail_identity_match is False:
                return current, VerificationResult(
                    status=VerificationStatus.FAILED,
                    reason_code="SUBJECT_MISMATCH",
                )
            if view.detail_identity_match is None:
                return current, None
            try:
                completion_view = view.completion_view(run_ref=context.run_alias)
                completion_context = CompletionContext(
                    run_ref=context.run_alias,
                    session_ref=context.session_id,
                    authentication_generation=context.authentication_generation,
                    account_binding_ref="inputs.account_id",
                    target_origin=context.target_origin,
                    approved_profile=context.profile_id,
                    requested_account_id=context.input_bindings["inputs.account_id"],
                    membership_proof=membership_proof,
                )
            except SurfaceError as error:
                if error.code == "COMPLETION_VIEW_UNAVAILABLE":
                    return current, None
                raise _ReplayStop(_surface_reason(error.code), step_id) from None
            except (KeyError, ValidationError):
                return current, VerificationResult(
                    status=VerificationStatus.FAILED,
                    reason_code="MEMBERSHIP_PROOF_INVALID",
                )
            return current, self._verifier.verify(
                bundle.contract,
                completion_view,
                completion_context,
            )

        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ReplayStop(SafeReasonCode.RECOVERY_EXHAUSTED, step_id)
            submit = state.actor.submit(
                expected_epoch=context.expected_epoch,
                run_id=context.run_alias,
                operation=verify_in_actor,
            )
            return await asyncio.wait_for(submit, timeout=remaining)
        except _ReplayStop:
            raise
        except ActorStaleEpoch:
            raise _ReplayStop(SafeReasonCode.STALE_EPOCH, step_id) from None
        except ActorPaused:
            raise _ReplayStop(SafeReasonCode.ACTOR_PAUSED, step_id) from None
        except ActorBusy:
            raise _ReplayStop(SafeReasonCode.SESSION_LOST, step_id) from None
        except SessionError as error:
            raise _ReplayStop(_session_reason(error.code), step_id) from None
        except TimeoutError:
            raise _ReplayStop(SafeReasonCode.RECOVERY_EXHAUSTED, step_id) from None
        except SurfaceError as error:
            raise _ReplayStop(_surface_reason(error.code), step_id) from None

    async def _observe_current(
        self,
        context: ExecutionContext,
        step_id: str,
        deadline: float | None,
    ) -> _CurrentObservation:
        if time.monotonic() >= (deadline if deadline is not None else float("inf")):
            raise _ReplayStop(SafeReasonCode.RECOVERY_EXHAUSTED, step_id)
        try:
            state = await self._sessions.get_state(context.session_id)
        except SessionError as error:
            raise _ReplayStop(_session_reason(error.code), step_id) from None
        if (
            state.state != "ACTIVE"
            or state.profile_id != context.profile_id
            or state.auth_generation != context.authentication_generation
            or state.origin != context.target_origin
            or state.actor.active_run_id != context.run_alias
        ):
            raise _ReplayStop(SafeReasonCode.SESSION_LOST, step_id)

        async def observe_in_actor():
            session = await self._sessions.get(context.session_id)
            handle = session.handle
            if session.actor is not state.actor or session.state != "ACTIVE":
                raise _ReplayStop(SafeReasonCode.SESSION_LOST, step_id)
            mismatch = _handle_mismatch_reason(handle, context)
            if mismatch is not None:
                raise _ReplayStop(mismatch, step_id)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise _ReplayStop(SafeReasonCode.RECOVERY_EXHAUSTED, step_id)
            observe = self._surface.observe(
                context.session_id,
                bindings=context.input_bindings,
            )
            if remaining is not None:
                observation, view = await asyncio.wait_for(observe, timeout=remaining)
            else:
                observation, view = await observe
            if not _view_matches_session(observation, view, handle, context):
                raise _ReplayStop(_view_mismatch_reason(view, context), step_id)
            if view.principal_matches is None:
                raise _ReplayStop(SafeReasonCode.PRINCIPAL_UNKNOWN, step_id)
            if view.principal_matches is not True:
                raise _ReplayStop(SafeReasonCode.SUBJECT_MISMATCH, step_id)
            evidence_ref = self._gateway._capture(context, observation, view)
            if evidence_ref is None:
                raise _ReplayStop(SafeReasonCode.EVIDENCE_ERROR, step_id)
            if view.unknown_blocker:
                # An unclassified dialog is not a failed precondition: the snapshot
                # is captured above, and the run escalates instead of failing hard.
                raise _ReplayStop(SafeReasonCode.UNKNOWN_BLOCKER, step_id)
            return _CurrentObservation(observation, view, evidence_ref)

        try:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise _ReplayStop(SafeReasonCode.RECOVERY_EXHAUSTED, step_id)
            submit = state.actor.submit(
                expected_epoch=context.expected_epoch,
                run_id=context.run_alias,
                operation=observe_in_actor,
            )
            if remaining is not None:
                return await asyncio.wait_for(submit, timeout=remaining)
            return await submit
        except ActorStaleEpoch:
            raise _ReplayStop(SafeReasonCode.STALE_EPOCH, step_id) from None
        except ActorPaused:
            raise _ReplayStop(SafeReasonCode.ACTOR_PAUSED, step_id) from None
        except ActorBusy:
            raise _ReplayStop(SafeReasonCode.SESSION_LOST, step_id) from None
        except SessionError as error:
            raise _ReplayStop(_session_reason(error.code), step_id) from None
        except TimeoutError:
            raise _ReplayStop(SafeReasonCode.RECOVERY_EXHAUSTED, step_id) from None
        except SurfaceError as error:
            raise _ReplayStop(_surface_reason(error.code), step_id) from None

    async def _read_session_handle(self, session_id):
        return (await self._sessions.get(session_id)).handle

    async def _finish_failure(
        self,
        context: ExecutionContext,
        reason: SafeReasonCode,
        step_id: str,
        effect_state: EffectState,
        evidence_refs: tuple[EvidenceRef, ...],
    ) -> InvocationResult:
        return self._failure(
            context.run_alias,
            reason,
            step_id=step_id,
            effect_state=effect_state,
            evidence_refs=evidence_refs,
        )

    async def _notify_handoff(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        membership_proof,
        result: ExecutionResult,
        step_id: str,
        reason: SafeReasonCode,
        *,
        actor: SessionActor,
        page_id: str,
        deadline: float,
    ) -> HandoffResult | None:
        if self._handoff_notifier is None:
            return None

        async def reconcile(reconciliation: ReconciliationContext) -> ReconciliationResult:
            return await self._reconcile_after_handoff(
                reconciliation,
                context=context,
                bundle=bundle,
                step=step,
                membership_proof=membership_proof,
                actor=actor,
                page_id=page_id,
                deadline=deadline,
            )

        trusted = TrustedHandoffContext(
            actor=actor,
            reconciler=reconcile,
            page_id=page_id,
            deadline_monotonic=deadline,
        )
        try:
            return await self._handoff_notifier.require_human(
                HandoffRequest(
                    run_id=context.run_alias,
                    session_id=context.session_id,
                    step_id=step_id,
                    reason_code=reason,
                    ownership_epoch=context.expected_epoch,
                ),
                trusted=trusted,
            )
        except Exception:
            # The safe terminal result is still returned if the optional
            # coordinator fails to establish an intervention.
            return None

    async def _reconcile_after_handoff(
        self,
        reconciliation: ReconciliationContext,
        *,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        membership_proof,
        actor: SessionActor,
        page_id: str,
        deadline: float,
    ) -> ReconciliationResult:
        """Reconcile one failed step during the actor's RESUMING lease."""

        def remain(reason: SafeReasonCode) -> ReconciliationResult:
            return ReconciliationResult(
                disposition=ReconciliationDisposition.REMAIN_PAUSED,
                reason=reason,
            )

        if time.monotonic() >= deadline:
            return remain(SafeReasonCode.RECOVERY_EXHAUSTED)
        snapshot = actor.snapshot
        if (
            reconciliation.actor is not actor
            or reconciliation.session_id != context.session_id
            or reconciliation.run_id != context.run_alias
            or reconciliation.epoch != snapshot.epoch
            or snapshot.owner != "RESUMING"
            or snapshot.active_run_id != context.run_alias
        ):
            return remain(SafeReasonCode.STALE_EPOCH)

        try:
            state = await self._sessions.get_state(context.session_id)
        except SessionError as error:
            return remain(_session_reason(error.code))
        if (
            state.actor is not actor
            or state.session_id != context.session_id
            or state.profile_id != context.profile_id
            or state.origin != context.target_origin
            or getattr(state, "principal_alias", None) is None
        ):
            return remain(SafeReasonCode.SESSION_LOST)
        if state.state == "SUBJECT_MISMATCH":
            return remain(SafeReasonCode.SUBJECT_MISMATCH)
        if state.state not in {"ACTIVE", "SESSION_EXPIRED"}:
            return remain(_session_reason(state.state))

        working_proof = membership_proof
        working_context = context.model_copy(
            update={
                "expected_epoch": reconciliation.epoch,
                "membership_proof": working_proof,
            }
        )
        auth_changed = state.auth_generation != context.authentication_generation
        if state.state == "SESSION_EXPIRED" or auth_changed:
            try:
                handle = await self._sessions.reauthenticate_existing(
                    context.session_id,
                    reconciliation.epoch,
                )
            except SessionError as error:
                return remain(_session_reason(error.code))
            if (
                handle.page_id != page_id
                or handle.session_id != context.session_id
                or handle.profile_id != context.profile_id
                or handle.origin != context.target_origin
                or handle.principal_alias != state.principal_alias
                or handle.auth_generation < context.authentication_generation
            ):
                return remain(SafeReasonCode.SUBJECT_MISMATCH)
            working_proof = None
            working_context = working_context.model_copy(
                update={
                    "authentication_generation": handle.auth_generation,
                    "membership_proof": None,
                }
            )

        if time.monotonic() >= deadline:
            return remain(SafeReasonCode.RECOVERY_EXHAUSTED)
        recovery = next(
            (item for item in bundle.recoveries if item.ref == step.recovery_ref),
            None,
        )
        anchor_step = _recovery_anchor_step(bundle, recovery) if recovery else None
        anchor_id = anchor_step.id if anchor_step is not None else None

        async def gateway_operation():
            return await self._gateway.reconcile_resuming_serial(
                working_context,
                bundle,
                step.id,
                working_proof,
                actor=actor,
                overview_anchor_step_id=anchor_id if working_proof is None else None,
            )

        try:
            result = await actor.submit_reconciliation(
                expected_epoch=reconciliation.epoch,
                run_id=reconciliation.run_id,
                operation=gateway_operation,
            )
        except ActorStaleEpoch:
            return remain(SafeReasonCode.STALE_EPOCH)
        except ActorPaused:
            return remain(SafeReasonCode.ACTOR_PAUSED)
        except ActorBusy:
            return remain(SafeReasonCode.SESSION_LOST)
        except SessionError as error:
            return remain(_session_reason(error.code))
        except Exception:
            return remain(SafeReasonCode.UNKNOWN_BLOCKER)

        fresh_proof = result.membership_proof or working_proof
        if result.effect_state is EffectState.VERIFIED:
            disposition = ReconciliationDisposition.NEXT
        elif result.retry_safe:
            disposition = ReconciliationDisposition.RETRY_SAFE
        else:
            return remain(result.reason_code)
        resumed_context = working_context.model_copy(
            update={
                # SessionActor.complete_resume advances exactly one epoch after
                # the RESUMING callback returns.
                "expected_epoch": reconciliation.epoch + 1,
                "membership_proof": fresh_proof,
            }
        )
        return ReconciliationResult(
            disposition=disposition,
            context=ReplayResumeContext(resumed_context),
            reason=SafeReasonCode.AUTHORIZED,
        )

    def _emit_step(
        self,
        context: ExecutionContext,
        step_id: str,
        reason: SafeReasonCode,
        effect: EffectState,
    ) -> None:
        from secrets import token_hex

        from cua.evidence import SafeEvent

        event_type = "ACTION_EFFECT_VERIFIED" if effect is EffectState.VERIFIED else "ACTION_REJECTED"
        try:
            self._evidence.emit(
                SafeEvent(
                    event_id=f"e_{token_hex(12)}",
                    run_alias=context.run_alias,
                    event_type=event_type,
                    step_id=step_id,
                    reason_code=reason,
                    effect_state=effect.value,
                )
            )
        except Exception:
            return

    @staticmethod
    def _failure(
        run_id: str,
        reason: SafeReasonCode,
        *,
        step_id: str | None,
        effect_state: EffectState,
        evidence_refs: tuple[EvidenceRef, ...] = (),
    ) -> InvocationResult:
        return InvocationResult(
            run_id=run_id,
            status=InvocationStatus.FAILURE,
            code=SafeReasonCode.REPLAY_FAILED,
            failure=FailureDetail(
                reason_code=reason,
                step_id=step_id,
                effect_state=effect_state,
            ),
            evidence_refs=evidence_refs,
        )

    @staticmethod
    def _aborted(
        run_id: str,
        reason: SafeReasonCode,
        *,
        step_id: str | None,
        effect_state: EffectState,
        evidence_refs: tuple[EvidenceRef, ...] = (),
    ) -> InvocationResult:
        return InvocationResult(
            run_id=run_id,
            status=InvocationStatus.ABORTED,
            code=reason,
            failure=FailureDetail(
                reason_code=reason,
                step_id=step_id,
                effect_state=effect_state,
            ),
            evidence_refs=evidence_refs,
        )

async def _remain_paused_reconciler(
    _context: ReconciliationContext,
) -> ReconciliationResult:
    """Default private seam until an application installs trusted reauth logic."""

    return ReconciliationResult(
        disposition=ReconciliationDisposition.REMAIN_PAUSED,
        reason=SafeReasonCode.UNKNOWN_BLOCKER,
    )


def _apply_resume_context(
    current: ExecutionContext,
    handoff: HandoffResult,
) -> tuple[ExecutionContext | None, SafeReasonCode | None]:
    """Apply only a typed, unchanged-scope context returned by reconciliation.

    Older trusted notifiers return no protected context.  They retain the
    historical behavior of advancing only the actor epoch.  A supplied
    context is treated as untrusted data at this boundary and must prove that
    it belongs to the same run, session, policy, input binding, and deadline.
    """

    if handoff.context is None:
        try:
            return current.model_copy(update={"expected_epoch": handoff.epoch}), None
        except (AttributeError, TypeError, ValueError):
            return None, SafeReasonCode.INVALID_INPUT
    if not isinstance(handoff.context, ReplayResumeContext):
        return None, SafeReasonCode.INVALID_INPUT
    candidate = handoff.context.context
    reason = _validate_resume_context(
        current,
        candidate,
        expected_epoch=handoff.epoch,
    )
    if reason is not None:
        return None, reason
    return candidate, None


def _validate_resume_context(
    current: ExecutionContext,
    candidate: ExecutionContext,
    *,
    expected_epoch: int,
) -> SafeReasonCode | None:
    """Return a safe rejection reason for a forged or stale replay context."""

    try:
        if candidate.run_alias != current.run_alias or candidate.session_id != current.session_id:
            return SafeReasonCode.SESSION_LOST
        if candidate.target_origin != current.target_origin:
            return SafeReasonCode.ORIGIN_MISMATCH
        if candidate.profile_id != current.profile_id:
            return SafeReasonCode.PROFILE_MISMATCH
        if candidate.expected_epoch != expected_epoch:
            return SafeReasonCode.STALE_EPOCH
        if candidate.authentication_generation < current.authentication_generation:
            return SafeReasonCode.AUTHENTICATION_CHANGED
        if candidate.deadline_monotonic != current.deadline_monotonic:
            return SafeReasonCode.INVALID_INPUT
        if not _secret_bindings_equal(
            candidate.input_bindings,
            current.input_bindings,
        ):
            return SafeReasonCode.INVALID_INPUT
        if candidate.policy_context.model_dump(mode="python") != current.policy_context.model_dump(
            mode="python"
        ):
            return SafeReasonCode.INVALID_INPUT
    except (AttributeError, TypeError, ValueError):
        return SafeReasonCode.INVALID_INPUT

    proof = candidate.membership_proof
    if proof is None:
        return None
    if not isinstance(proof, MembershipProof):
        return SafeReasonCode.MEMBERSHIP_PROOF_INVALID
    try:
        requested = candidate.input_bindings.get("inputs.account_id")
        if not isinstance(requested, SecretStr):
            return SafeReasonCode.MEMBERSHIP_PROOF_INVALID
        if (
            proof.run_ref != candidate.run_alias
            or proof.session_ref != candidate.session_id
            or proof.authentication_generation != candidate.authentication_generation
            or proof.account_binding_ref != "inputs.account_id"
            or proof.account_binding_value.get_secret_value() != requested.get_secret_value()
            or proof.overview_complete is not True
            or proof.account_present is not True
        ):
            return SafeReasonCode.MEMBERSHIP_PROOF_INVALID
    except (AttributeError, TypeError, ValueError):
        return SafeReasonCode.MEMBERSHIP_PROOF_INVALID
    return None


def _secret_bindings_equal(left, right) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict) or set(left) != set(right):
        return False
    return all(
        isinstance(left[key], SecretStr)
        and isinstance(right[key], SecretStr)
        and left[key].get_secret_value() == right[key].get_secret_value()
        for key in left
    )


def _requires_handoff(result: ExecutionResult) -> bool:
    """Return whether an unresolved result needs an operator-owned boundary."""

    if result.reason_code in {
        SafeReasonCode.APP_ERROR,
        SafeReasonCode.AMBIGUOUS_STATE,
        SafeReasonCode.SUBJECT_MISMATCH,
    }:
        return False
    if result.effect_state is EffectState.OUTCOME_UNKNOWN:
        return True
    return result.reason_code in {
        SafeReasonCode.UNKNOWN_BLOCKER,
        SafeReasonCode.SESSION_EXPIRED,
        SafeReasonCode.SESSION_LOST,
        SafeReasonCode.PRECONDITION_UNKNOWN,
        SafeReasonCode.POSTCONDITION_UNKNOWN,
    }


def _handoff_retry_limit(bundle: CapabilityBundle, step: BundleStep) -> int:
    recovery = next(
        (item for item in bundle.recoveries if item.ref == step.recovery_ref),
        None,
    )
    return recovery.max_attempts if recovery is not None else _MAX_SAFE_RETRIES


def _valid_input_bindings(context: ExecutionContext) -> bool:
    if set(context.input_bindings) != {"inputs.account_id"}:
        return False
    account_id = context.input_bindings.get("inputs.account_id")
    return (
        isinstance(account_id, SecretStr)
        and _ACCOUNT_ID_PATTERN.fullmatch(account_id.get_secret_value()) is not None
    )


def _valid_contract_inputs(bundle: CapabilityBundle, context: ExecutionContext) -> bool:
    if not _supported_runtime_contract(bundle):
        return False
    value = context.input_bindings.get("inputs.account_id")
    return (
        set(context.input_bindings) == {"inputs.account_id"}
        and isinstance(value, SecretStr)
        and _ACCOUNT_ID_PATTERN.fullmatch(value.get_secret_value()) is not None
    )


def _supported_runtime_contract(bundle: CapabilityBundle) -> bool:
    """Only the pinned balance contract can reach native execution or verification."""
    inputs = bundle.contract.inputs
    outputs = {item.name: item for item in bundle.contract.outputs}
    return (
        bundle.capability.name == "get_savings_balance"
        and bundle.capability.version == "1.0.0"
        and len(inputs) == 1
        and inputs[0].name == "account_id"
        and inputs[0].value_type == "string"
        and inputs[0].pattern == r"^[0-9]+$"
        and inputs[0].sensitive is True
        and set(outputs) == {"available_balance", "currency"}
        and outputs["available_balance"].value_type == "decimal_string"
        and outputs["available_balance"].sensitive is True
        and outputs["available_balance"].enum == ()
        and outputs["currency"].value_type == "string"
        and outputs["currency"].sensitive is False
        and outputs["currency"].enum == ("USD",)
        and set(bundle.contract.business_outcomes).issubset(
            {"ACCOUNT_NOT_FOUND", "ACCESS_DENIED"}
        )
    )


def _is_membership_assertion(step: BundleStep, bundle: CapabilityBundle) -> bool:
    facts = set()
    definitions = {item.name: item.expression for item in bundle.conditions}
    for expression in step.preconditions:
        facts.update(_positive_facts(expression, definitions, ()))
    return {
        ("principal_matches", None),
        ("overview_complete", None),
        ("account_present", "inputs.account_id"),
    }.issubset(facts)


def _positive_facts(expression, definitions, stack):
    if isinstance(expression, ConditionReference):
        if expression.name in stack or expression.name not in definitions:
            return set()
        return _positive_facts(
            definitions[expression.name],
            definitions,
            (*stack, expression.name),
        )
    if isinstance(expression, AllCondition):
        facts = set()
        for child in expression.conditions:
            facts.update(_positive_facts(child, definitions, stack))
        return facts
    if isinstance(expression, PrincipalMatchesCondition):
        return {("principal_matches", None)}
    if isinstance(expression, OverviewCompleteCondition):
        return {("overview_complete", None)}
    if isinstance(expression, AccountPresentCondition):
        return {("account_present", expression.input_ref)}
    return set()


def _membership_assertion_step(bundle: CapabilityBundle) -> BundleStep | None:
    return next(
        (
            step
            for step in bundle.steps
            if step.kind == "ASSERT" and _is_membership_assertion(step, bundle)
        ),
        None,
    )


def target_is_account_binding(bundle: CapabilityBundle, step: BundleStep) -> bool:
    target = next((item for item in bundle.targets if item.ref == step.target_ref), None)
    return target is not None and target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT"


def _recovery_anchor_step(
    bundle: CapabilityBundle,
    recovery: RecoveryDefinition,
) -> BundleStep | None:
    target = next(
        (item for item in bundle.targets if item.ref == recovery.anchor_target_ref),
        None,
    )
    if target is None or target.locator != "ROLE_LINK_ACCOUNTS_OVERVIEW":
        return None
    return next(
        (
            step
            for step in bundle.steps
            if step.kind == "CLICK"
            and step.target_ref == recovery.anchor_target_ref
            and step.source.type == "reviewer_added"
        ),
        None,
    )


def _expected_source_satisfied(step: BundleStep, bundle: CapabilityBundle, view) -> bool:
    target = next((item for item in bundle.targets if item.ref == step.target_ref), None)
    if target is None:
        return step.kind in {"ASSERT", "WAIT", "VERIFY"}
    if target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT":
        return (
            view.safe_route == "accounts_overview"
            and view.page_state == "OVERVIEW_READY"
            and view.overview_complete is True
        )
    if target.locator == "ROLE_LINK_ACCOUNTS_OVERVIEW":
        return view.page_state in {"AUTHENTICATED_HOME", "OVERVIEW_READY", "DETAIL_READY"}
    return view.safe_route == "account_details" and view.page_state == "DETAIL_READY"


def _account_not_found(bundle, step, view, context) -> bool:
    if view.overview_complete is not True:
        return False
    # Absence is a business outcome when judged from a complete overview, either by
    # the declared membership ASSERT or by the step that clicks the account link.
    if not (target_is_account_binding(bundle, step) or _is_membership_assertion(step, bundle)):
        return False
    account_id = context.input_bindings["inputs.account_id"].get_secret_value()
    return account_id not in view.account_ids


def _view_matches_session(observation, view, handle, context) -> bool:
    return (
        observation.session_id == context.session_id == view.session_id == handle.session_id
        and observation.page_id == handle.page_id
        and observation.id == view.observation_id
        and observation.safe_route == view.safe_route
        and view.profile_id == handle.profile_id == context.profile_id
        and view.authentication_generation == handle.auth_generation == context.authentication_generation
        and view.origin == handle.origin == context.target_origin
        and view.page_state in observation.state_tags
    )


def _handle_mismatch_reason(handle, context) -> SafeReasonCode | None:
    if handle.auth_generation != context.authentication_generation:
        return SafeReasonCode.AUTHENTICATION_CHANGED
    if handle.origin != context.target_origin:
        return SafeReasonCode.ORIGIN_MISMATCH
    if handle.profile_id != context.profile_id:
        return SafeReasonCode.PROFILE_MISMATCH
    return None


def _view_mismatch_reason(view, context) -> SafeReasonCode:
    if view.authentication_generation != context.authentication_generation:
        return SafeReasonCode.AUTHENTICATION_CHANGED
    if view.origin != context.target_origin:
        return SafeReasonCode.ORIGIN_MISMATCH
    if view.profile_id != context.profile_id:
        return SafeReasonCode.PROFILE_MISMATCH
    return SafeReasonCode.STALE_OBSERVATION


def _session_reason(code: str) -> SafeReasonCode:
    try:
        return SafeReasonCode(code)
    except ValueError:
        return SafeReasonCode.SESSION_LOST


def _surface_reason(code: str) -> SafeReasonCode:
    aliases = {
        "FIELD_RELATION_UNVERIFIED": SafeReasonCode.DETAIL_NOT_READY,
        "ACCOUNT_NOT_PRESENT": SafeReasonCode.ACCOUNT_NOT_FOUND,
        "STALE_OBSERVATION": SafeReasonCode.STALE_OBSERVATION,
        "SUBJECT_MISMATCH": SafeReasonCode.SUBJECT_MISMATCH,
    }
    if code in aliases:
        return aliases[code]
    try:
        return SafeReasonCode(code)
    except ValueError:
        return SafeReasonCode.INTERNAL_ERROR


def _safe_reason(code: str, fallback: SafeReasonCode) -> SafeReasonCode:
    try:
        return SafeReasonCode(code)
    except ValueError:
        return fallback


def _step_result(
    step_id: str,
    effect_state: EffectState,
    reason_code: SafeReasonCode,
    *,
    observation_id: str | None = None,
    evidence_refs: tuple[EvidenceRef, ...] = (),
    membership_proof=None,
    retry_safe: bool = False,
) -> ExecutionResult:
    return ExecutionResult(
        step_id=step_id,
        effect_state=effect_state,
        reason_code=reason_code,
        observation_id=observation_id,
        evidence_refs=evidence_refs,
        membership_proof=membership_proof,
        retry_safe=retry_safe,
    )
