"""Actor-serialized, fail-closed dispatch for the pinned read-only profile."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import re
import secrets
import time
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from pydantic import SecretStr, TypeAdapter, ValidationError

from cua.conditions.evaluator import ConditionContext, ConditionEvaluator, TriState
from cua.conditions.parsers import AmountParseError, USDDecimalParser
from cua.evidence import EvidenceError, EvidenceSink, SafeEvent, SafeReasonCode
from cua.execution.contracts import EffectState, ExecutionContext, ExecutionResult
from cua.models.actions import ClickDecision, Decision
from cua.models.bundles import BundleStep, CapabilityBundle, TargetDefinition
from cua.models.conditions import (
    AccountPresentCondition,
    AllCondition,
    AnyCondition,
    ConditionExpr,
    ConditionReference,
    NotCondition,
)
from cua.models.observations import Observation
from cua.policy import PolicyEngine, Risk, RuntimeTargetClassification
from cua.sessions.actor import ActorBusy, ActorPaused, ActorStaleEpoch
from cua.sessions.manager import SessionError, SessionManager
from cua.surface.playwright_surface import (
    NormalizedView,
    PlaywrightSurface,
    ResolvedTarget,
    SurfaceError,
)


_INPUT_NAME = re.compile(r"^[a-z][a-z0-9_]*$", re.ASCII)
_PINNED_PROFILE = "parabank-native-v1"
_PINNED_LOCATORS = {
    "ROLE_LINK_ACCOUNTS_OVERVIEW": ("CLICK", {"home", "accounts_overview", "account_details"}),
    "TABLE_ACCOUNT_LINK_BY_INPUT": ("CLICK", {"accounts_overview"}),
    "PROFILE_ACCOUNT_NUMBER": ("READ", {"account_details"}),
    "PROFILE_ACCOUNT_TYPE": ("READ", {"account_details"}),
    "PROFILE_AVAILABLE_BALANCE": ("READ", {"account_details"}),
}
_PINNED_DESTINATIONS = {
    "ROLE_LINK_ACCOUNTS_OVERVIEW": "accounts_overview",
    "TABLE_ACCOUNT_LINK_BY_INPUT": "account_details",
}
_SAFE_REASON_CODES = {item.value for item in SafeReasonCode}
_SURFACE_PRE_DISPATCH_ERRORS = {
    "UNSAFE_DESTINATION",
    "STALE_TARGET",
    "TARGET_NOT_ACTIONABLE",
    "WRONG_FRAME",
    "ACTION_BINDING_MISMATCH",
    "ACTION_NOT_ALLOWED",
    "UNSUPPORTED_ACTION",
}
_POST_EFFECT_TIMEOUT_SECONDS = 5.0
_POST_EFFECT_POLL_INTERVAL_SECONDS = 0.1


class ExecutionGateway:
    """Execute exactly one supported, statically declared bundle step.

    Replay must obtain ``bundle`` from ``BundleRegistry.prepare_execution`` before
    calling this internal runtime seam. The gateway revalidates the model and all
    live authority immediately before each browser action.
    """

    def __init__(
        self,
        sessions: SessionManager,
        surface: PlaywrightSurface,
        policy: PolicyEngine,
        evidence: EvidenceSink,
    ) -> None:
        self._sessions = sessions
        self._surface = surface
        self._policy = policy
        self._evidence = evidence
        self._conditions = ConditionEvaluator()
        self._usd_parser = USDDecimalParser()

    async def dispatch(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step_id: str,
    ) -> ExecutionResult:
        """Validate and dispatch one step without logging protected run values."""
        safe_step_id = step_id if _INPUT_NAME.fullmatch(step_id or "") else None
        try:
            context = ExecutionContext.model_validate(context.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            return ExecutionResult(
                step_id=safe_step_id or "invalid_step",
                effect_state=EffectState.NOT_DISPATCHED,
                reason_code=SafeReasonCode.INVALID_INPUT,
            )
        if _deadline_expired(context):
            return _result(safe_step_id or "invalid_step", EffectState.NOT_DISPATCHED,
                           SafeReasonCode.RECOVERY_EXHAUSTED)

        try:
            bundle = CapabilityBundle.model_validate(bundle.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            self._emit(context, "ACTION_REJECTED", safe_step_id, SafeReasonCode.INVALID_BUNDLE,
                       EffectState.NOT_DISPATCHED)
            return _result(safe_step_id, EffectState.NOT_DISPATCHED, SafeReasonCode.INVALID_BUNDLE)

        if safe_step_id is None:
            self._emit(context, "ACTION_REJECTED", None, SafeReasonCode.INVALID_INPUT,
                       EffectState.NOT_DISPATCHED)
            return _result("invalid_step", EffectState.NOT_DISPATCHED, SafeReasonCode.INVALID_INPUT)

        step = next((item for item in bundle.steps if item.id == safe_step_id), None)
        if step is None or not _valid_run_inputs(context, bundle):
            reason = SafeReasonCode.INVALID_INPUT if step is not None else SafeReasonCode.INVALID_BUNDLE
            self._emit(context, "ACTION_REJECTED", safe_step_id, reason, EffectState.NOT_DISPATCHED)
            return _result(safe_step_id, EffectState.NOT_DISPATCHED, reason)

        if step.kind not in {"CLICK", "EXTRACT"}:
            self._emit(context, "ACTION_REJECTED", step.id, SafeReasonCode.ACTION_NOT_SUPPORTED,
                       EffectState.NOT_DISPATCHED)
            return _result(step.id, EffectState.NOT_DISPATCHED, SafeReasonCode.ACTION_NOT_SUPPORTED)

        targets = {item.ref: item for item in bundle.targets}
        target = targets.get(step.target_ref or "")
        operation = "CLICK" if step.kind == "CLICK" else "READ"
        if target is None or not _target_declared_for_step(target, operation):
            self._emit(context, "ACTION_REJECTED", step.id, SafeReasonCode.INVALID_BUNDLE,
                       EffectState.NOT_DISPATCHED)
            return _result(step.id, EffectState.NOT_DISPATCHED, SafeReasonCode.INVALID_BUNDLE)

        # A complete verified trace and its completion-proof reference are necessary;
        # durable approval itself is checked by BundleRegistry at the replay boundary.
        if not bundle.provenance.verified or not bundle.provenance.completion_proof_id:
            self._emit(context, "ACTION_REJECTED", step.id, SafeReasonCode.INVALID_BUNDLE,
                       EffectState.NOT_DISPATCHED)
            return _result(step.id, EffectState.NOT_DISPATCHED, SafeReasonCode.INVALID_BUNDLE)

        try:
            state = await self._sessions.get_state(context.session_id)
        except SessionError:
            self._emit(context, "ACTION_REJECTED", step.id, SafeReasonCode.SESSION_LOST,
                       EffectState.NOT_DISPATCHED)
            return _result(step.id, EffectState.NOT_DISPATCHED, SafeReasonCode.SESSION_LOST)

        if state.state != "ACTIVE":
            reason = _session_reason(state.state)
            self._emit(context, "ACTION_REJECTED", step.id, reason, EffectState.NOT_DISPATCHED)
            return _result(step.id, EffectState.NOT_DISPATCHED, reason)
        if state.profile_id != context.profile_id:
            self._emit(context, "ACTION_REJECTED", step.id, SafeReasonCode.PROFILE_MISMATCH,
                       EffectState.NOT_DISPATCHED)
            return _result(step.id, EffectState.NOT_DISPATCHED, SafeReasonCode.PROFILE_MISMATCH)
        if state.auth_generation != context.authentication_generation:
            self._emit(context, "ACTION_REJECTED", step.id, SafeReasonCode.AUTHENTICATION_CHANGED,
                       EffectState.NOT_DISPATCHED)
            return _result(step.id, EffectState.NOT_DISPATCHED, SafeReasonCode.AUTHENTICATION_CHANGED)
        if state.origin != context.target_origin:
            self._emit(context, "ACTION_REJECTED", step.id, SafeReasonCode.ORIGIN_MISMATCH,
                       EffectState.NOT_DISPATCHED)
            return _result(step.id, EffectState.NOT_DISPATCHED, SafeReasonCode.ORIGIN_MISMATCH)

        async def operation_in_actor():
            return await self._dispatch_serial(
                context,
                bundle,
                step,
                target,
                state.actor,
            )

        try:
            return await state.actor.submit(
                expected_epoch=context.expected_epoch,
                run_id=context.run_alias,
                operation=operation_in_actor,
            )
        except ActorStaleEpoch:
            return self._reject(context, step.id, SafeReasonCode.STALE_EPOCH)
        except ActorPaused:
            return self._reject(context, step.id, SafeReasonCode.ACTOR_PAUSED)
        except ActorBusy:
            return self._reject(context, step.id, SafeReasonCode.SESSION_LOST)
        except SessionError as error:
            return self._reject(context, step.id, _session_reason(error.code))
        except Exception:
            # Exceptions can contain URL, page, or user data; intentionally discard them.
            return self._reject(context, step.id, SafeReasonCode.INTERNAL_ERROR)

    async def dispatch_observed(
        self,
        context: ExecutionContext,
        decision: Decision,
    ) -> ExecutionResult:
        """Revalidate a discovery decision against its stored, current observation.

        This path does not synthesize a capability bundle. It shares the exact actor,
        resolver, destination-policy, execute, and post-effect checks used by replay.
        """
        step_id = "observed_action"
        try:
            context = ExecutionContext.model_validate(context.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            return _result(step_id, EffectState.NOT_DISPATCHED, SafeReasonCode.INVALID_INPUT)
        if _deadline_expired(context):
            return _result(step_id, EffectState.NOT_DISPATCHED, SafeReasonCode.RECOVERY_EXHAUSTED)
        try:
            decision = TypeAdapter(Decision).validate_python(decision.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            self._emit(context, "ACTION_REJECTED", step_id, SafeReasonCode.INVALID_INPUT,
                       EffectState.NOT_DISPATCHED)
            return _result(step_id, EffectState.NOT_DISPATCHED, SafeReasonCode.INVALID_INPUT)
        if not isinstance(decision, ClickDecision):
            return self._reject(context, step_id, SafeReasonCode.ACTION_NOT_SUPPORTED)
        if not _valid_bindings(context.input_bindings):
            return self._reject(context, step_id, SafeReasonCode.INVALID_INPUT)

        try:
            state = await self._sessions.get_state(context.session_id)
        except SessionError:
            return self._reject(context, step_id, SafeReasonCode.SESSION_LOST)
        reason = _state_reason(state, context)
        if reason is not None:
            return self._reject(context, step_id, reason)

        async def operation_in_actor():
            return await self._dispatch_observed_serial(context, decision, state.actor)

        try:
            return await state.actor.submit(
                expected_epoch=context.expected_epoch,
                run_id=context.run_alias,
                operation=operation_in_actor,
            )
        except ActorStaleEpoch:
            return self._reject(context, step_id, SafeReasonCode.STALE_EPOCH)
        except ActorPaused:
            return self._reject(context, step_id, SafeReasonCode.ACTOR_PAUSED)
        except ActorBusy:
            return self._reject(context, step_id, SafeReasonCode.SESSION_LOST)
        except SessionError as error:
            return self._reject(context, step_id, _session_reason(error.code))
        except Exception:
            return self._reject(context, step_id, SafeReasonCode.INTERNAL_ERROR)

    def evaluate_conditions(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        conditions: tuple[ConditionExpr, ...],
        view: NormalizedView,
        membership_proof=None,
    ) -> SafeReasonCode | None:
        """Evaluate bundle conditions against this view and run's current proof."""
        condition_context = _condition_context(
            view,
            context,
            membership_proof or context.membership_proof,
            bundle.targets,
        )
        return _evaluate_conditions(
            self._conditions,
            conditions,
            condition_context,
            bundle,
        )

    async def reconcile_unknown(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step_id: str,
        membership_proof=None,
    ) -> ExecutionResult:
        """Freshly inspect an unknown effect; never resend the original action here."""
        try:
            context = ExecutionContext.model_validate(context.model_dump(mode="python"))
            bundle = CapabilityBundle.model_validate(bundle.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            return _result(step_id, EffectState.NOT_DISPATCHED, SafeReasonCode.INVALID_INPUT)
        step = next((item for item in bundle.steps if item.id == step_id), None)
        if step is None or step.kind not in {"CLICK", "EXTRACT"}:
            return self._reject(context, step_id, SafeReasonCode.INVALID_BUNDLE)
        if not bundle.provenance.verified or not bundle.provenance.completion_proof_id:
            return self._reject(context, step_id, SafeReasonCode.INVALID_BUNDLE)
        target = next((item for item in bundle.targets if item.ref == step.target_ref), None)
        operation = "CLICK" if step.kind == "CLICK" else "READ"
        if target is None or not _target_declared_for_step(target, operation):
            return self._reject(context, step_id, SafeReasonCode.INVALID_BUNDLE)
        if _deadline_expired(context):
            return _result(step_id, EffectState.OUTCOME_UNKNOWN, SafeReasonCode.RECOVERY_EXHAUSTED)
        if membership_proof is not None:
            context = context.model_copy(update={"membership_proof": membership_proof})
        try:
            state = await self._sessions.get_state(context.session_id)
        except SessionError:
            return self._reject(context, step_id, SafeReasonCode.SESSION_LOST)
        reason = _state_reason(state, context)
        if reason is not None:
            return self._reject(context, step_id, reason)

        async def operation_in_actor():
            return await self._reconcile_serial(context, bundle, step, target, state.actor)

        try:
            return await state.actor.submit(
                expected_epoch=context.expected_epoch,
                run_id=context.run_alias,
                operation=operation_in_actor,
            )
        except ActorStaleEpoch:
            return self._reject(context, step_id, SafeReasonCode.STALE_EPOCH)
        except ActorPaused:
            return self._reject(context, step_id, SafeReasonCode.ACTOR_PAUSED)
        except ActorBusy:
            return self._reject(context, step_id, SafeReasonCode.SESSION_LOST)
        except SessionError as error:
            return self._reject(context, step_id, _session_reason(error.code))
        except Exception:
            return self._reject(context, step_id, SafeReasonCode.INTERNAL_ERROR)

    async def reconcile_resuming_serial(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step_id: str,
        membership_proof=None,
        *,
        actor,
        overview_anchor_step_id: str | None = None,
    ) -> ExecutionResult:
        """Reconcile one handoff step inside an already RESUMING actor command.

        The caller must have entered ``SessionActor.submit_reconciliation``.
        This method deliberately never submits another actor command: it only
        performs the gateway/surface work while the caller's actor command lock
        is held.  A reviewer-added overview anchor may be dispatched once to
        rebuild membership before the current step is evaluated.
        """
        try:
            context = ExecutionContext.model_validate(context.model_dump(mode="python"))
            bundle = CapabilityBundle.model_validate(bundle.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            return _result(step_id, EffectState.NOT_DISPATCHED, SafeReasonCode.INVALID_INPUT)

        snapshot = actor.snapshot
        if snapshot.owner != "RESUMING":
            return _result(step_id, EffectState.NOT_DISPATCHED, SafeReasonCode.ACTOR_PAUSED)
        if snapshot.epoch != context.expected_epoch:
            return _result(step_id, EffectState.NOT_DISPATCHED, SafeReasonCode.STALE_EPOCH)
        if snapshot.active_run_id != context.run_alias:
            return _result(step_id, EffectState.NOT_DISPATCHED, SafeReasonCode.SESSION_LOST)

        step = next((item for item in bundle.steps if item.id == step_id), None)
        target = (
            next((item for item in bundle.targets if item.ref == step.target_ref), None)
            if step is not None
            else None
        )
        if (
            step is None
            or step.kind not in {"CLICK", "EXTRACT"}
            or target is None
            or not _target_declared_for_step(
                target, "CLICK" if step.kind == "CLICK" else "READ"
            )
        ):
            return self._reject(context, step_id, SafeReasonCode.INVALID_BUNDLE)
        if not bundle.provenance.verified or not bundle.provenance.completion_proof_id:
            return self._reject(context, step_id, SafeReasonCode.INVALID_BUNDLE)
        if _deadline_expired(context):
            return _result(step_id, EffectState.OUTCOME_UNKNOWN, SafeReasonCode.RECOVERY_EXHAUSTED)

        if membership_proof is not None:
            context = context.model_copy(update={"membership_proof": membership_proof})
        result = await self._reconcile_serial(context, bundle, step, target, actor)
        if (
            result.effect_state is EffectState.VERIFIED
            or result.retry_safe
            or overview_anchor_step_id is None
            or membership_proof is not None
            or result.reason_code
            not in {
                SafeReasonCode.POSTCONDITION_UNKNOWN,
                SafeReasonCode.MEMBERSHIP_PROOF_INVALID,
                SafeReasonCode.DETAIL_NOT_READY,
                SafeReasonCode.ROUTE_NOT_ALLOWED,
            }
        ):
            return result

        anchor = next(
            (item for item in bundle.steps if item.id == overview_anchor_step_id),
            None,
        )
        anchor_target = (
            next((item for item in bundle.targets if item.ref == anchor.target_ref), None)
            if anchor is not None
            else None
        )
        if (
            anchor is None
            or anchor.kind != "CLICK"
            or anchor.source.type != "reviewer_added"
            or anchor_target is None
            or anchor_target.locator != "ROLE_LINK_ACCOUNTS_OVERVIEW"
            or not _target_declared_for_step(anchor_target, "CLICK")
        ):
            return _result(
                step.id,
                EffectState.OUTCOME_UNKNOWN,
                SafeReasonCode.INVALID_BUNDLE,
                observation_id=result.observation_id,
                evidence_refs=result.evidence_refs,
            )

        anchor_result = await self._dispatch_serial(
            context,
            bundle,
            anchor,
            anchor_target,
            actor,
        )
        if anchor_result.effect_state is not EffectState.VERIFIED:
            return _result(
                step.id,
                anchor_result.effect_state,
                anchor_result.reason_code,
                observation_id=anchor_result.observation_id,
                membership_proof=membership_proof,
                evidence_refs=anchor_result.evidence_refs,
                retry_safe=False,
            )

        # The anchor has returned to the approved overview.  A fresh
        # _reconcile_serial observation mints the membership proof from the
        # current DOM and evaluates the original step's postcondition again.
        return await self._reconcile_serial(context, bundle, step, target, actor)

    async def _reconcile_serial(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        target: TargetDefinition,
        actor,
    ) -> ExecutionResult:
        try:
            if _deadline_expired(context):
                return _result(step.id, EffectState.OUTCOME_UNKNOWN,
                               SafeReasonCode.RECOVERY_EXHAUSTED)
            session = await self._sessions.get(context.session_id)
            handle = session.handle
            if session.actor is not actor or session.state != "ACTIVE":
                return _result(step.id, EffectState.OUTCOME_UNKNOWN,
                               SafeReasonCode.SESSION_LOST)
            if handle.auth_generation != context.authentication_generation:
                return _result(step.id, EffectState.OUTCOME_UNKNOWN,
                               SafeReasonCode.AUTHENTICATION_CHANGED)
            if handle.origin != context.target_origin:
                return _result(step.id, EffectState.OUTCOME_UNKNOWN,
                               SafeReasonCode.ORIGIN_MISMATCH)
            if handle.profile_id != context.profile_id:
                return _result(step.id, EffectState.OUTCOME_UNKNOWN,
                               SafeReasonCode.PROFILE_MISMATCH)
            observation, view = await self._surface.observe(
                context.session_id,
                bindings=context.input_bindings,
            )
            evidence_ref = self._capture(context, observation, view)
            if evidence_ref is None:
                return _result(step.id, EffectState.OUTCOME_UNKNOWN,
                               SafeReasonCode.EVIDENCE_ERROR)
            evidence_refs = (evidence_ref,)
            if not _view_matches_session(observation, view, handle, context):
                return _result(step.id, EffectState.OUTCOME_UNKNOWN,
                               _view_mismatch_reason(view, context),
                               observation_id=observation.id,
                               evidence_refs=evidence_refs)
            principal_reason = _principal_reason(view)
            if principal_reason is not None:
                return _result(step.id, EffectState.OUTCOME_UNKNOWN, principal_reason,
                               observation_id=observation.id,
                               evidence_refs=evidence_refs)
            if getattr(view, "unknown_blocker", False) or view.page_state == "UNKNOWN":
                return _result(
                    step.id,
                    EffectState.OUTCOME_UNKNOWN,
                    SafeReasonCode.UNKNOWN_BLOCKER,
                    observation_id=observation.id,
                    evidence_refs=evidence_refs,
                )
            if view.page_state in {"APP_ERROR", "ACCESS_DENIED"}:
                return _result(
                    step.id,
                    EffectState.OUTCOME_UNKNOWN,
                    SafeReasonCode(view.page_state),
                    observation_id=observation.id,
                    evidence_refs=evidence_refs,
                )

            proof = context.membership_proof
            if (
                target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT"
                and view.safe_route == "accounts_overview"
                and view.overview_complete is True
            ):
                binding = context.input_bindings.get("inputs.account_id")
                if binding is not None:
                    try:
                        proof = view.membership_proof(
                            run_ref=context.run_alias,
                            binding_ref="inputs.account_id",
                            binding_value=binding,
                        )
                    except (AttributeError, SurfaceError):
                        proof = None

            destination = _PINNED_DESTINATIONS.get(target.locator)
            if step.kind == "CLICK" and destination is not None:
                if _click_effect_matches(view, destination, proof):
                    postcondition = self.evaluate_conditions(
                        context, bundle, step.postconditions, view, proof
                    )
                    if postcondition is None:
                        if self._emit(
                            context,
                            "ACTION_EFFECT_VERIFIED",
                            step.id,
                            SafeReasonCode.AUTHORIZED,
                            EffectState.VERIFIED,
                        ):
                            return _result(
                                step.id,
                                EffectState.VERIFIED,
                                SafeReasonCode.AUTHORIZED,
                                observation_id=observation.id,
                                membership_proof=proof,
                                evidence_refs=evidence_refs,
                            )
                        return _result(step.id, EffectState.OUTCOME_UNKNOWN,
                                       SafeReasonCode.EVIDENCE_ERROR,
                                       observation_id=observation.id,
                                       evidence_refs=evidence_refs)
                    if postcondition is not SafeReasonCode.PRECONDITION_UNKNOWN:
                        reason = (
                            SafeReasonCode.POSTCONDITION_FAILED
                            if postcondition is SafeReasonCode.PRECONDITION_FAILED
                            else postcondition
                        )
                        return _result(step.id, EffectState.OUTCOME_UNKNOWN, reason,
                                       observation_id=observation.id,
                                       membership_proof=proof,
                                       evidence_refs=evidence_refs)

            retry_safe = False
            if step.recovery_ref is not None:
                expected_source = _expected_view_status(target, view)
                condition_context = _condition_context(view, context, proof, bundle.targets)
                precondition = _evaluate_conditions(
                    self._conditions,
                    step.preconditions,
                    condition_context,
                    bundle,
                )
                retry_safe = (
                    step.kind == "CLICK"
                    and expected_source is None
                    and precondition is None
                    and (target.locator != "TABLE_ACCOUNT_LINK_BY_INPUT"
                         or _membership_proof_matches(context, proof))
                )
            return _result(
                step.id,
                EffectState.OUTCOME_UNKNOWN,
                SafeReasonCode.POSTCONDITION_UNKNOWN,
                observation_id=observation.id,
                membership_proof=proof,
                evidence_refs=evidence_refs,
                retry_safe=retry_safe,
            )
        except SessionError as error:
            return _result(step.id, EffectState.OUTCOME_UNKNOWN, _session_reason(error.code))
        except SurfaceError as error:
            return _result(step.id, EffectState.OUTCOME_UNKNOWN, _surface_reason(error.code))
        except Exception:
            return _result(step.id, EffectState.OUTCOME_UNKNOWN,
                           SafeReasonCode.POSTCONDITION_UNKNOWN)

    async def _dispatch_observed_serial(
        self,
        context: ExecutionContext,
        decision: ClickDecision,
        actor,
    ) -> ExecutionResult:
        step_id = "observed_action"
        evidence_refs = []
        resolved = None
        try:
            session = await self._sessions.get(context.session_id)
            handle = session.handle
            if session.actor is not actor or session.state != "ACTIVE":
                return self._reject(context, step_id, SafeReasonCode.SESSION_LOST)
            if handle.profile_id != context.profile_id:
                return self._reject(context, step_id, SafeReasonCode.PROFILE_MISMATCH)
            if handle.auth_generation != context.authentication_generation:
                return self._reject(context, step_id, SafeReasonCode.AUTHENTICATION_CHANGED)
            if handle.origin != context.target_origin:
                return self._reject(context, step_id, SafeReasonCode.ORIGIN_MISMATCH)

            observation, view = await self._surface.current_observation(
                context.session_id,
                decision.observation_id,
            )
            evidence_ref = self._capture(context, observation, view)
            if evidence_ref is None:
                return self._reject(context, step_id, SafeReasonCode.EVIDENCE_ERROR)
            evidence_refs.append(evidence_ref)
            if not _view_matches_session(observation, view, handle, context):
                return self._reject(context, step_id, _view_mismatch_reason(view, context),
                                    observation_id=observation.id,
                                    evidence_refs=tuple(evidence_refs))
            reason = _principal_reason(view)
            if reason is not None:
                return self._reject(context, step_id, reason,
                                    observation_id=observation.id,
                                    evidence_refs=tuple(evidence_refs))

            # Resolve only against the exact stored observation referenced by the model.
            sanitized = ClickDecision(
                observation_id=decision.observation_id,
                reason_code=SafeReasonCode.AUTHORIZED.value,
                rationale="Runtime revalidates this stored control before dispatch.",
                operation="CLICK",
                control_ref=decision.control_ref,
            )
            resolved = await self._surface.resolve_observed(
                context.session_id,
                sanitized,
                frame_ref="f_main",
            )
            if resolved.locator_key not in _PINNED_LOCATORS:
                return self._reject(context, step_id, SafeReasonCode.INVALID_RUNTIME_CLASSIFICATION,
                                    observation_id=observation.id,
                                    evidence_refs=tuple(evidence_refs))
            try:
                target = TargetDefinition(
                    ref="observed_target",
                    locator=resolved.locator_key,
                    allowed_operations=("CLICK",),
                    binding_ref=resolved.binding_ref,
                )
            except ValidationError:
                return self._reject(context, step_id, SafeReasonCode.ACTION_NOT_SUPPORTED,
                                    observation_id=observation.id,
                                    evidence_refs=tuple(evidence_refs))
            if not _target_declared_for_step(target, "CLICK"):
                return self._reject(context, step_id, SafeReasonCode.ACTION_NOT_SUPPORTED,
                                    observation_id=observation.id,
                                    evidence_refs=tuple(evidence_refs))
            validation_reason = _resolved_target_reason(
                observation, view, target, resolved, "CLICK", context
            )
            if validation_reason is not None:
                return self._reject(context, step_id, validation_reason,
                                    observation_id=observation.id,
                                    evidence_refs=tuple(evidence_refs))
            control_ref, control_operations = _live_control_binding(
                observation, resolved, "CLICK"
            )
            if control_operations is None:
                return self._reject(context, step_id, SafeReasonCode.TARGET_AMBIGUOUS,
                                    observation_id=observation.id,
                                    evidence_refs=tuple(evidence_refs))

            proof = context.membership_proof
            if target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT":
                binding = context.input_bindings.get("inputs.account_id")
                if (
                    binding is None
                    or resolved.binding_value is None
                    or resolved.binding_value.get_secret_value() != binding.get_secret_value()
                    or view.overview_complete is not True
                    or binding.get_secret_value() not in getattr(view, "account_ids", frozenset())
                ):
                    return self._reject(context, step_id, SafeReasonCode.MEMBERSHIP_PROOF_INVALID,
                                        observation_id=observation.id,
                                        evidence_refs=tuple(evidence_refs))
                try:
                    proof = view.membership_proof(
                        run_ref=context.run_alias,
                        binding_ref="inputs.account_id",
                        binding_value=binding,
                    )
                except (AttributeError, SurfaceError):
                    return self._reject(context, step_id, SafeReasonCode.MEMBERSHIP_PROOF_INVALID,
                                        observation_id=observation.id,
                                        evidence_refs=tuple(evidence_refs))

            reason, classification = _authorize_target(
                context.policy_context,
                view,
                target,
                resolved,
                "CLICK",
                control_ref,
                control_operations,
            )
            if reason is not None:
                return self._reject(context, step_id, reason,
                                    observation_id=observation.id,
                                    evidence_refs=tuple(evidence_refs))
            authorization = self._policy.authorize(context.policy_context, classification)
            if not authorization.allowed:
                return self._reject(
                    context,
                    step_id,
                    _safe_reason(authorization.reason_code, SafeReasonCode.INTERNAL_ERROR),
                    observation_id=observation.id,
                    evidence_refs=tuple(evidence_refs),
                )
            return await self._execute_click(
                context,
                step_id,
                sanitized,
                resolved,
                handle,
                membership_proof=proof,
                evidence_refs=tuple(evidence_refs),
            )
        except SessionError as error:
            return self._reject(context, step_id, _session_reason(error.code))
        except SurfaceError as error:
            return self._reject(context, step_id, _surface_reason(error.code))
        except Exception:
            return self._reject(context, step_id, SafeReasonCode.INTERNAL_ERROR)

    async def _dispatch_serial(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        target: TargetDefinition,
        actor,
    ) -> ExecutionResult:
        dispatched = False
        evidence_refs = []
        before_observation: Observation | None = None
        before_view: NormalizedView | None = None
        resolved: ResolvedTarget | None = None
        try:
            if _deadline_expired(context):
                return self._reject(context, step.id, SafeReasonCode.RECOVERY_EXHAUSTED)
            session = await self._sessions.get(context.session_id)
            handle = session.handle
            if session.actor is not actor:
                return self._reject(context, step.id, SafeReasonCode.SESSION_LOST)
            if session.state != "ACTIVE":
                return self._reject(context, step.id, _session_reason(session.state))
            if handle.profile_id != context.profile_id:
                return self._reject(context, step.id, SafeReasonCode.PROFILE_MISMATCH)
            if handle.auth_generation != context.authentication_generation:
                return self._reject(context, step.id, SafeReasonCode.AUTHENTICATION_CHANGED)
            if handle.origin != context.target_origin:
                return self._reject(context, step.id, SafeReasonCode.ORIGIN_MISMATCH)

            before_observation, before_view = await self._surface.observe(
                context.session_id,
                bindings=context.input_bindings,
            )
            evidence_ref = self._capture(context, before_observation, before_view)
            if evidence_ref is None:
                return self._reject(context, step.id, SafeReasonCode.EVIDENCE_ERROR)
            evidence_refs.append(evidence_ref)
            if not _view_matches_session(before_observation, before_view, handle, context):
                return self._reject(context, step.id, _view_mismatch_reason(before_view, context))
            status = _expected_view_status(target, before_view)
            if status is not None:
                return self._reject(context, step.id, status,
                                    observation_id=before_observation.id,
                                    evidence_refs=tuple(evidence_refs))
            identity_reason = _principal_reason(before_view)
            if identity_reason is not None:
                return self._reject(context, step.id, identity_reason,
                                    observation_id=before_observation.id,
                                    evidence_refs=tuple(evidence_refs))
            if step.kind == "EXTRACT":
                if before_view.detail_identity_match is None:
                    return self._reject(
                        context,
                        step.id,
                        SafeReasonCode.DETAIL_NOT_READY,
                        observation_id=before_observation.id,
                        evidence_refs=tuple(evidence_refs),
                    )
                if before_view.detail_identity_match is not True:
                    return self._reject(
                        context,
                        step.id,
                        SafeReasonCode.SUBJECT_MISMATCH,
                        observation_id=before_observation.id,
                        evidence_refs=tuple(evidence_refs),
                    )

            proof = context.membership_proof
            if target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT":
                input_value = context.input_bindings.get(target.binding_ref or "")
                if input_value is None or target.binding_ref != "inputs.account_id":
                    return self._reject(context, step.id, SafeReasonCode.INVALID_INPUT,
                                        observation_id=before_observation.id,
                                        evidence_refs=tuple(evidence_refs))
                if before_view.overview_complete is not True:
                    reason = SafeReasonCode.PRECONDITION_UNKNOWN
                    return self._reject(context, step.id, reason,
                                        observation_id=before_observation.id,
                                        evidence_refs=tuple(evidence_refs))
                account_id = input_value.get_secret_value()
                account_ids = getattr(before_view, "account_ids", frozenset())
                if account_id not in account_ids:
                    return self._reject(context, step.id, SafeReasonCode.ACCOUNT_NOT_FOUND,
                                        observation_id=before_observation.id,
                                        evidence_refs=tuple(evidence_refs))
                try:
                    proof = before_view.membership_proof(
                        run_ref=context.run_alias,
                        binding_ref=target.binding_ref,
                        binding_value=input_value,
                    )
                except (AttributeError, SurfaceError):
                    return self._reject(context, step.id, SafeReasonCode.MEMBERSHIP_PROOF_INVALID,
                                        observation_id=before_observation.id,
                                        evidence_refs=tuple(evidence_refs))

            if step.kind == "EXTRACT" and not _membership_proof_matches(context, proof):
                return self._reject(context, step.id, SafeReasonCode.MEMBERSHIP_PROOF_INVALID,
                                    observation_id=before_observation.id,
                                    evidence_refs=tuple(evidence_refs))
            condition_context = _condition_context(before_view, context, proof, bundle.targets)
            precondition = _evaluate_conditions(
                self._conditions,
                step.preconditions,
                condition_context,
                bundle,
            )
            if precondition is not None:
                if precondition is SafeReasonCode.PRECONDITION_FAILED and _failed_account_presence(
                    step.preconditions, bundle, before_view, condition_context
                ):
                    precondition = SafeReasonCode.ACCOUNT_NOT_FOUND
                return self._reject(context, step.id, precondition,
                                    observation_id=before_observation.id,
                                    evidence_refs=tuple(evidence_refs))

            resolved = await self._surface.resolve_target(
                context.session_id,
                before_observation.id,
                target,
                context.input_bindings,
            )
            validation_reason = _resolved_target_reason(
                before_observation,
                before_view,
                target,
                resolved,
                "CLICK" if step.kind == "CLICK" else "READ",
                context,
            )
            if validation_reason is not None:
                return self._reject(context, step.id, validation_reason,
                                    observation_id=before_observation.id,
                                    evidence_refs=tuple(evidence_refs))

            control_ref, control_operations = _live_control_binding(
                before_observation,
                resolved,
                "CLICK" if step.kind == "CLICK" else "READ",
            )
            if control_operations is None and step.kind == "CLICK":
                return self._reject(context, step.id, SafeReasonCode.TARGET_AMBIGUOUS,
                                    observation_id=before_observation.id,
                                    evidence_refs=tuple(evidence_refs))

            classification_reason, classification = _authorize_target(
                context.policy_context,
                before_view,
                target,
                resolved,
                "CLICK" if step.kind == "CLICK" else "READ",
                control_ref,
                control_operations or ("READ",),
            )
            if classification_reason is not None:
                return self._reject(context, step.id, classification_reason,
                                    observation_id=before_observation.id,
                                    evidence_refs=tuple(evidence_refs))
            authorization = self._policy.authorize(context.policy_context, classification)
            if not authorization.allowed:
                reason = _safe_reason(authorization.reason_code, SafeReasonCode.INTERNAL_ERROR)
                return self._reject(context, step.id, reason,
                                    observation_id=before_observation.id,
                                    evidence_refs=tuple(evidence_refs))

            if _deadline_expired(context):
                return self._reject(
                    context,
                    step.id,
                    SafeReasonCode.RECOVERY_EXHAUSTED,
                    observation_id=before_observation.id,
                    evidence_refs=tuple(evidence_refs),
                )

            if step.kind == "EXTRACT":
                return self._extract(
                    context,
                    bundle,
                    step,
                    target,
                    before_observation,
                    before_view,
                    resolved,
                    proof,
                    condition_context,
                    tuple(evidence_refs),
                )

            decision = ClickDecision(
                observation_id=before_observation.id,
                reason_code=SafeReasonCode.AUTHORIZED.value,
                rationale="Pinned read-only target passed live policy checks.",
                operation="CLICK",
                control_ref=resolved.control_ref,
            )
            return await self._execute_click(
                context,
                step.id,
                decision,
                resolved,
                handle,
                expected_postconditions=step.postconditions,
                bundle=bundle,
                membership_proof=proof,
                evidence_refs=tuple(evidence_refs),
            )

        except SessionError as error:
            reason = _session_reason(error.code)
            return _result(
                step.id,
                EffectState.OUTCOME_UNKNOWN if dispatched else EffectState.NOT_DISPATCHED,
                reason,
                observation_id=before_observation.id if before_observation else None,
                evidence_refs=tuple(evidence_refs),
            )
        except SurfaceError as error:
            reason = _surface_reason(error.code)
            status = EffectState.OUTCOME_UNKNOWN if dispatched else EffectState.NOT_DISPATCHED
            self._emit(
                context,
                "ACTION_OUTCOME_UNKNOWN" if dispatched else "ACTION_REJECTED",
                step.id,
                reason,
                status,
                control_ref=resolved.control_ref if resolved else None,
            )
            return _result(
                step.id,
                status,
                reason,
                observation_id=before_observation.id if before_observation else None,
                evidence_refs=tuple(evidence_refs),
            )
        except Exception:
            # Never return browser-library messages or values in the result/evidence.
            status = EffectState.OUTCOME_UNKNOWN if dispatched else EffectState.NOT_DISPATCHED
            reason = SafeReasonCode.POSTCONDITION_UNKNOWN if dispatched else SafeReasonCode.INTERNAL_ERROR
            self._emit(
                context,
                "ACTION_OUTCOME_UNKNOWN" if dispatched else "ACTION_REJECTED",
                step.id,
                reason,
                status,
                control_ref=resolved.control_ref if resolved else None,
            )
            return _result(
                step.id,
                status,
                reason,
                observation_id=before_observation.id if before_observation else None,
                evidence_refs=tuple(evidence_refs),
            )

    async def _execute_click(
        self,
        context: ExecutionContext,
        step_id: str,
        decision: ClickDecision,
        resolved: ResolvedTarget,
        handle,
        *,
        expected_postconditions: tuple = (),
        bundle: CapabilityBundle | None = None,
        membership_proof=None,
        evidence_refs: tuple = (),
    ) -> ExecutionResult:
        if _deadline_expired(context):
            return self._reject(
                context,
                step_id,
                SafeReasonCode.RECOVERY_EXHAUSTED,
                observation_id=decision.observation_id,
                evidence_refs=evidence_refs,
            )
        action_remaining = _deadline_remaining(context)
        if action_remaining is not None and action_remaining <= 0:
            return self._reject(
                context,
                step_id,
                SafeReasonCode.RECOVERY_EXHAUSTED,
                observation_id=decision.observation_id,
                evidence_refs=evidence_refs,
            )
        action_timeout = 5.0 if action_remaining is None else min(5.0, action_remaining)
        if not self._emit(
            context,
            "ACTION_DISPATCHED",
            step_id,
            SafeReasonCode.AUTHORIZED,
            EffectState.DISPATCHED,
            control_ref=resolved.control_ref,
        ):
            return _result(
                step_id,
                EffectState.NOT_DISPATCHED,
                SafeReasonCode.EVIDENCE_ERROR,
                observation_id=decision.observation_id,
                evidence_refs=evidence_refs,
                membership_proof=membership_proof,
            )

        started = time.monotonic()
        try:
            await asyncio.wait_for(
                self._surface.execute(context.session_id, decision, resolved),
                timeout=action_timeout,
            )
        except SurfaceError as error:
            reason = _surface_reason(error.code)
            state = (
                EffectState.NOT_DISPATCHED
                if error.code in _SURFACE_PRE_DISPATCH_ERRORS
                else EffectState.OUTCOME_UNKNOWN
            )
            self._emit(
                context,
                "ACTION_REJECTED"
                if state is EffectState.NOT_DISPATCHED
                else "ACTION_OUTCOME_UNKNOWN",
                step_id,
                reason,
                state,
                control_ref=resolved.control_ref,
                duration_ms=_duration_ms(started),
            )
            return _result(
                step_id,
                state,
                reason,
                observation_id=decision.observation_id,
                evidence_refs=evidence_refs,
                membership_proof=membership_proof,
            )
        except Exception:
            # Once execute has begun, an absent browser receipt cannot authorize retry.
            self._emit(
                context,
                "ACTION_OUTCOME_UNKNOWN",
                step_id,
                SafeReasonCode.POSTCONDITION_UNKNOWN,
                EffectState.OUTCOME_UNKNOWN,
                control_ref=resolved.control_ref,
                duration_ms=_duration_ms(started),
            )
            return _result(
                step_id,
                EffectState.OUTCOME_UNKNOWN,
                SafeReasonCode.POSTCONDITION_UNKNOWN,
                observation_id=decision.observation_id,
                evidence_refs=evidence_refs,
                membership_proof=membership_proof,
            )

        observation_deadline = time.monotonic() + _POST_EFFECT_TIMEOUT_SECONDS
        run_deadline = context.deadline_monotonic
        deadline = min(observation_deadline, run_deadline) if run_deadline is not None else observation_deadline
        last_observation: Observation | None = None
        last_view: NormalizedView | None = None
        final_reason = SafeReasonCode.POSTCONDITION_UNKNOWN
        if run_deadline is not None and run_deadline <= time.monotonic():
            final_reason = SafeReasonCode.RECOVERY_EXHAUSTED
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                last_observation, last_view = await asyncio.wait_for(
                    self._surface.observe(
                        context.session_id,
                        bindings=context.input_bindings,
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                break
            except SurfaceError as error:
                final_reason = _surface_reason(error.code)
                break
            except Exception:
                break

            if not _view_matches_session(last_observation, last_view, handle, context):
                final_reason = _view_mismatch_reason(last_view, context)
                break
            identity_reason = _principal_reason(last_view)
            if identity_reason is not None:
                final_reason = identity_reason
                break
            if (
                resolved.destination_route == "account_details"
                and last_view.safe_route == "account_details"
                and last_view.page_state == "DETAIL_READY"
                and last_view.detail_identity_match is False
            ):
                final_reason = SafeReasonCode.SUBJECT_MISMATCH
                break

            if _click_effect_matches(last_view, resolved.destination_route, membership_proof):
                postcondition = None
                if expected_postconditions:
                    if bundle is None:
                        postcondition = SafeReasonCode.INVALID_BUNDLE
                    else:
                        post_context = _condition_context(
                            last_view,
                            context,
                            membership_proof,
                            bundle.targets,
                        )
                        postcondition = _evaluate_conditions(
                            self._conditions,
                            expected_postconditions,
                            post_context,
                            bundle,
                        )
                if postcondition is None:
                    final_reason = SafeReasonCode.AUTHORIZED
                    break
                if postcondition is not SafeReasonCode.PRECONDITION_UNKNOWN:
                    final_reason = (
                        SafeReasonCode.POSTCONDITION_FAILED
                        if postcondition is SafeReasonCode.PRECONDITION_FAILED
                        else postcondition
                    )
                    break

            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(_POST_EFFECT_POLL_INTERVAL_SECONDS, remaining))

        captured_refs = _capture_final_observation(
            self,
            context,
            last_observation,
            last_view,
            evidence_refs,
        )
        if last_observation is not None:
            observation_id = last_observation.id
        else:
            observation_id = decision.observation_id
        if captured_refs is None:
            final_reason = SafeReasonCode.EVIDENCE_ERROR
        else:
            evidence_refs = captured_refs

        if final_reason is SafeReasonCode.AUTHORIZED:
            if self._emit(
                context,
                "ACTION_EFFECT_VERIFIED",
                step_id,
                final_reason,
                EffectState.VERIFIED,
                control_ref=resolved.control_ref,
                duration_ms=_duration_ms(started),
            ):
                return _result(
                    step_id,
                    EffectState.VERIFIED,
                    final_reason,
                    observation_id=observation_id,
                    evidence_refs=evidence_refs,
                    membership_proof=membership_proof,
                )
            final_reason = SafeReasonCode.EVIDENCE_ERROR

        self._emit(
            context,
            "ACTION_OUTCOME_UNKNOWN",
            step_id,
            final_reason,
            EffectState.OUTCOME_UNKNOWN,
            control_ref=resolved.control_ref,
            duration_ms=_duration_ms(started),
        )
        return _result(
            step_id,
            EffectState.OUTCOME_UNKNOWN,
            final_reason,
            observation_id=observation_id,
            evidence_refs=evidence_refs,
            membership_proof=membership_proof,
        )

    def _extract(
        self,
        context: ExecutionContext,
        bundle: CapabilityBundle,
        step: BundleStep,
        target: TargetDefinition,
        observation: Observation,
        view: NormalizedView,
        resolved: ResolvedTarget,
        proof,
        condition_context: ConditionContext,
        evidence_refs: tuple,
    ) -> ExecutionResult:
        if target.locator not in {
            "PROFILE_ACCOUNT_NUMBER",
            "PROFILE_ACCOUNT_TYPE",
            "PROFILE_AVAILABLE_BALANCE",
        }:
            return self._reject(context, step.id, SafeReasonCode.INVALID_BUNDLE,
                                observation_id=observation.id,
                                evidence_refs=evidence_refs)
        if (
            getattr(resolved, "control_ref", None) is not None
            or getattr(resolved, "unique", False) is not True
            or getattr(resolved, "visible", False) is not True
            or "READ" not in getattr(resolved, "allowed_operations", ())
            or resolved.observation_id != observation.id
            or resolved.session_id != context.session_id
            or resolved.authentication_generation != context.authentication_generation
        ):
            return self._reject(context, step.id, SafeReasonCode.TARGET_NOT_ACTIONABLE,
                                observation_id=observation.id,
                                evidence_refs=evidence_refs)

        relations = getattr(view, "field_relations", {})
        values_by_ref = getattr(view, "field_values", {})
        if relations.get(target.locator) is not True:
            return self._reject(context, step.id, SafeReasonCode.DETAIL_NOT_READY,
                                observation_id=observation.id,
                                evidence_refs=evidence_refs)
        values = values_by_ref.get(target.locator, ())
        if not values:
            return self._reject(context, step.id, SafeReasonCode.DETAIL_NOT_READY,
                                observation_id=observation.id,
                                evidence_refs=evidence_refs)
        if len(values) != 1:
            return self._reject(context, step.id, SafeReasonCode.TARGET_AMBIGUOUS,
                                observation_id=observation.id,
                                evidence_refs=evidence_refs)

        value = values[0]
        parsed = False
        if target.locator == "PROFILE_AVAILABLE_BALANCE":
            if (step.parser_id, step.parser_version) != (
                USDDecimalParser.parser_id,
                USDDecimalParser.version,
            ):
                return self._reject(context, step.id, SafeReasonCode.INVALID_BUNDLE,
                                    observation_id=observation.id,
                                    evidence_refs=evidence_refs)
            try:
                self._usd_parser.parse(value.get_secret_value())
                parsed = True
            except AmountParseError as error:
                reason = _safe_reason(error.code, SafeReasonCode.INVALID_AMOUNT)
                return self._reject(context, step.id, reason,
                                    observation_id=observation.id,
                                    evidence_refs=evidence_refs)

        outputs = {step.output_ref: value} if step.output_ref else {}
        valid_refs = frozenset({step.output_ref}) if step.output_ref and (parsed or target.locator != "PROFILE_AVAILABLE_BALANCE") else frozenset()
        post_context = replace(
            condition_context,
            output_values=MappingProxyType(outputs),
            valid_output_refs=valid_refs,
        )
        postcondition = _evaluate_conditions(
            self._conditions,
            step.postconditions,
            post_context,
            bundle,
        )
        if postcondition is not None:
            return self._reject(context, step.id, postcondition,
                                observation_id=observation.id,
                                evidence_refs=evidence_refs)
        if not self._emit(
            context,
            "ACTION_EFFECT_VERIFIED",
            step.id,
            SafeReasonCode.AUTHORIZED,
            EffectState.VERIFIED,
        ):
            return _result(step.id, EffectState.NOT_DISPATCHED, SafeReasonCode.EVIDENCE_ERROR,
                           observation_id=observation.id,
                           evidence_refs=evidence_refs)
        return _result(
            step.id,
            EffectState.VERIFIED,
            SafeReasonCode.AUTHORIZED,
            observation_id=observation.id,
            value=value,
            membership_proof=proof,
            evidence_refs=evidence_refs,
        )

    def _capture(self, context: ExecutionContext, observation: Observation, view: NormalizedView):
        try:
            reference = self._evidence.capture_safe(
                context.run_alias,
                view,
                observation=observation,
            )
            self._emit(context, "OBSERVATION_CAPTURED", None, None,
                       EffectState.NOT_DISPATCHED)
            self._emit(context, "EVIDENCE_CAPTURED", None, None,
                       EffectState.NOT_DISPATCHED)
            return reference
        except EvidenceError:
            return None

    def _reject(
        self,
        context: ExecutionContext,
        step_id: str,
        reason: SafeReasonCode,
        *,
        observation_id: str | None = None,
        evidence_refs: tuple = (),
    ) -> ExecutionResult:
        self._emit(context, "ACTION_REJECTED", step_id, reason, EffectState.NOT_DISPATCHED)
        return _result(
            step_id,
            EffectState.NOT_DISPATCHED,
            reason,
            observation_id=observation_id,
            evidence_refs=evidence_refs,
        )

    def _emit(
        self,
        context: ExecutionContext,
        event_type: str,
        step_id: str | None,
        reason: SafeReasonCode | None,
        effect_state: EffectState,
        *,
        control_ref: str | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        try:
            self._evidence.emit(
                SafeEvent(
                    event_id=f"e_{secrets.token_hex(12)}",
                    run_alias=context.run_alias,
                    event_type=event_type,
                    step_id=step_id,
                    reason_code=reason,
                    effect_state=effect_state.value,
                    control_ref=control_ref,
                    duration_ms=duration_ms,
                )
            )
            return True
        except (EvidenceError, ValidationError, ValueError):
            return False


def _valid_run_inputs(context: ExecutionContext, bundle: CapabilityBundle) -> bool:
    expected = {f"inputs.{item.name}": item for item in bundle.contract.inputs}
    if set(context.input_bindings) != set(expected):
        return False
    if context.profile_id != _PINNED_PROFILE:
        return False
    for reference, contract in expected.items():
        value = context.input_bindings.get(reference)
        if value is None:
            return False
        try:
            raw = value.get_secret_value()
            if re.fullmatch(contract.pattern, raw, re.ASCII) is None:
                return False
        except (AttributeError, TypeError, re.error):
            return False
    return True


def _target_declared_for_step(target: TargetDefinition, operation: str) -> bool:
    expected = _PINNED_LOCATORS.get(target.locator)
    return (
        expected is not None
        and expected[0] == operation
        and operation in target.allowed_operations
        and (
            (target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT"
             and target.binding_ref == "inputs.account_id")
            or (target.locator != "TABLE_ACCOUNT_LINK_BY_INPUT" and target.binding_ref is None)
        )
    )


def _expected_view_status(target: TargetDefinition, view: NormalizedView) -> SafeReasonCode | None:
    expected = _PINNED_LOCATORS.get(target.locator)
    if expected is None or view.safe_route not in expected[1]:
        return SafeReasonCode.ROUTE_NOT_ALLOWED
    if target.locator == "ROLE_LINK_ACCOUNTS_OVERVIEW":
        if view.page_state not in {"AUTHENTICATED_HOME", "OVERVIEW_READY", "DETAIL_READY"}:
            return SafeReasonCode.PRECONDITION_UNKNOWN
    elif target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT":
        if view.page_state != "OVERVIEW_READY" or view.overview_complete is not True:
            return SafeReasonCode.PRECONDITION_UNKNOWN
    elif view.page_state != "DETAIL_READY":
        return SafeReasonCode.DETAIL_NOT_READY
    return None


def _authorize_target(
    policy_context,
    view: NormalizedView,
    target: TargetDefinition,
    resolved: ResolvedTarget,
    operation: str,
    control_ref: str | None,
    control_operations: tuple[str, ...],
):
    if operation == "CLICK":
        expected_route = _PINNED_DESTINATIONS.get(target.locator)
        if expected_route is None or resolved.destination_route != expected_route:
            return SafeReasonCode.ROUTE_NOT_ALLOWED, None
        risk = _safe_risk(resolved.runtime_risk)
        if risk is not Risk.READ_ONLY:
            return SafeReasonCode.RISK_NOT_READ_ONLY, None
        origin = resolved.destination_origin
        route = resolved.destination_route
    else:
        risk = Risk.READ_ONLY
        origin = _authority_origin(view.origin)
        route = view.safe_route
    try:
        classification = RuntimeTargetClassification(
            origin=origin,
            route=route,
            operation=operation,
            risk=risk,
            control_ref=control_ref,
            control_operations=control_operations,
        )
    except ValidationError:
        return SafeReasonCode.INVALID_RUNTIME_CLASSIFICATION, None
    return None, classification


def _click_effect_matches(view: NormalizedView, destination_route: str, membership_proof) -> bool:
    expected_state = {
        "accounts_overview": "OVERVIEW_READY",
        "account_details": "DETAIL_READY",
    }.get(destination_route)
    if (
        expected_state is None
        or view.safe_route != destination_route
        or view.page_state != expected_state
        or view.principal_matches is not True
    ):
        return False
    if destination_route == "accounts_overview":
        return view.overview_complete is True
    if (
        view.detail_identity_match is not True
        or membership_proof is None
        or membership_proof.session_ref != view.session_id
        or membership_proof.authentication_generation != view.authentication_generation
        or membership_proof.account_binding_ref != "inputs.account_id"
        or membership_proof.overview_complete is not True
        or membership_proof.account_present is not True
    ):
        return False
    account_numbers = (getattr(view, "field_values", None) or {}).get(
        "PROFILE_ACCOUNT_NUMBER", ()
    )
    return (
        len(account_numbers) == 1
        and account_numbers[0].get_secret_value()
        == membership_proof.account_binding_value.get_secret_value()
    )


def _capture_final_observation(
    gateway: ExecutionGateway,
    context: ExecutionContext,
    observation: Observation | None,
    view: NormalizedView | None,
    evidence_refs: tuple,
) -> tuple | None:
    if observation is None or view is None:
        return evidence_refs
    evidence_ref = gateway._capture(context, observation, view)
    if evidence_ref is None:
        return None
    return (*evidence_refs, evidence_ref)


def _principal_reason(view: NormalizedView) -> SafeReasonCode | None:
    if view.principal_matches is None:
        return SafeReasonCode.PRINCIPAL_UNKNOWN
    if view.principal_matches is not True:
        return SafeReasonCode.SUBJECT_MISMATCH
    return None


def _view_matches_session(observation, view, handle, context: ExecutionContext) -> bool:
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


def _view_mismatch_reason(view: NormalizedView, context: ExecutionContext) -> SafeReasonCode:
    if getattr(view, "authentication_generation", None) != context.authentication_generation:
        return SafeReasonCode.AUTHENTICATION_CHANGED
    if getattr(view, "origin", None) != context.target_origin:
        return SafeReasonCode.ORIGIN_MISMATCH
    if getattr(view, "profile_id", None) != context.profile_id:
        return SafeReasonCode.PROFILE_MISMATCH
    return SafeReasonCode.STALE_OBSERVATION


def _resolved_target_reason(
    observation: Observation,
    view: NormalizedView,
    target: TargetDefinition,
    resolved: ResolvedTarget,
    operation: str,
    context: ExecutionContext,
) -> SafeReasonCode | None:
    if (
        resolved.target_ref != target.ref
        or resolved.locator_key != target.locator
        or resolved.observation_id != observation.id
        or resolved.session_id != context.session_id
        or resolved.page_id != observation.page_id
        or resolved.document_generation != observation.document_generation
        or resolved.authentication_generation != context.authentication_generation
    ):
        return SafeReasonCode.STALE_OBSERVATION
    if not resolved.unique:
        return SafeReasonCode.TARGET_AMBIGUOUS
    if not resolved.visible or not resolved.enabled or operation not in resolved.allowed_operations:
        return SafeReasonCode.TARGET_NOT_ACTIONABLE
    if operation == "CLICK" and resolved.control_ref is None:
        return SafeReasonCode.TARGET_NOT_ACTIONABLE
    if operation == "READ" and resolved.control_ref is not None:
        return SafeReasonCode.INVALID_RUNTIME_CLASSIFICATION
    return None


def _live_control_binding(observation, resolved, operation: str):
    if operation == "READ":
        return None, ("READ",)
    matches = [control for control in observation.controls if control.ref == resolved.control_ref]
    if len(matches) != 1:
        return resolved.control_ref, None
    control = matches[0]
    if not control.visible or not control.enabled or operation not in control.allowed_operations:
        return resolved.control_ref, None
    if control.frame_ref != resolved.frame_ref:
        return resolved.control_ref, None
    return resolved.control_ref, tuple(set(control.allowed_operations) & set(resolved.allowed_operations))


def _condition_context(view, context: ExecutionContext, proof, targets=()) -> ConditionContext:
    base = view.condition_context(context.input_bindings)
    membership = dict(base.membership_validity)
    field_values = dict(base.field_values)
    for target in targets:
        values = (getattr(view, "field_values", None) or {}).get(target.locator)
        if values is not None:
            field_values[target.ref] = values
    account_value = context.input_bindings.get("inputs.account_id")
    if proof is not None:
        membership["inputs.account_id"] = bool(
            account_value is not None
            and proof.run_ref == context.run_alias
            and proof.session_ref == context.session_id
            and proof.authentication_generation == context.authentication_generation
            and proof.account_binding_ref == "inputs.account_id"
            and proof.account_binding_value.get_secret_value() == account_value.get_secret_value()
            and proof.overview_complete is True
            and proof.account_present is True
        )
    return replace(
        base,
        membership_validity=MappingProxyType(membership),
        field_values=MappingProxyType(field_values),
    )


def _membership_proof_matches(context: ExecutionContext, proof) -> bool:
    account_value = context.input_bindings.get("inputs.account_id")
    return bool(
        proof is not None
        and account_value is not None
        and proof.run_ref == context.run_alias
        and proof.session_ref == context.session_id
        and proof.authentication_generation == context.authentication_generation
        and proof.account_binding_ref == "inputs.account_id"
        and proof.account_binding_value.get_secret_value() == account_value.get_secret_value()
        and proof.overview_complete is True
        and proof.account_present is True
    )


def _evaluate_conditions(
    evaluator: ConditionEvaluator,
    conditions: tuple[ConditionExpr, ...],
    context: ConditionContext,
    bundle: CapabilityBundle,
) -> SafeReasonCode | None:
    if not conditions:
        return None
    evaluator = ConditionEvaluator(
        condition_definitions={item.name: item.expression for item in bundle.conditions}
    )
    for condition in conditions:
        result = evaluator.evaluate(condition, context)
        if result.status is TriState.PASS:
            continue
        if result.status is TriState.UNKNOWN:
            return SafeReasonCode.PRECONDITION_UNKNOWN if result.reason_code not in _SAFE_REASON_CODES else _safe_reason(
                result.reason_code, SafeReasonCode.PRECONDITION_UNKNOWN
            )
        return _safe_reason(result.reason_code, SafeReasonCode.PRECONDITION_FAILED)
    return None


def _failed_account_presence(conditions, bundle, view, condition_context) -> bool:
    evaluator = ConditionEvaluator(
        condition_definitions={item.name: item.expression for item in bundle.conditions}
    )
    for reference in _account_present_references(
        conditions,
        {item.name: item.expression for item in bundle.conditions},
    ):
        if view.overview_complete is True and condition_context.account_presence.get(reference) is False:
            return True
    return False


def _account_present_references(conditions, definitions, stack=()):
    found: set[str] = set()
    for condition in conditions:
        if isinstance(condition, AccountPresentCondition):
            found.add(condition.input_ref)
        elif isinstance(condition, ConditionReference):
            if condition.name in stack:
                continue
            expression = definitions.get(condition.name)
            if expression is not None:
                found.update(_account_present_references((expression,), definitions, (*stack, condition.name)))
        elif isinstance(condition, (AllCondition, AnyCondition)):
            found.update(_account_present_references(condition.conditions, definitions, stack))
        elif isinstance(condition, NotCondition):
            found.update(_account_present_references((condition.child,), definitions, stack))
    return found


def _authority_origin(origin: str) -> str:
    parts = urlsplit(origin)
    return f"{parts.scheme}://{parts.netloc}"


def _safe_risk(value) -> Risk:
    try:
        return Risk(value)
    except (ValueError, TypeError):
        return Risk.UNKNOWN


def _valid_bindings(bindings: Mapping[str, SecretStr]) -> bool:
    for reference, value in bindings.items():
        if reference != "inputs.account_id" or not isinstance(value, SecretStr):
            return False
        if re.fullmatch(r"[0-9]+", value.get_secret_value(), re.ASCII) is None:
            return False
    return True


def _state_reason(state, context: ExecutionContext) -> SafeReasonCode | None:
    if state.state != "ACTIVE":
        return _session_reason(state.state)
    if state.profile_id != context.profile_id:
        return SafeReasonCode.PROFILE_MISMATCH
    if state.auth_generation != context.authentication_generation:
        return SafeReasonCode.AUTHENTICATION_CHANGED
    if state.origin != context.target_origin:
        return SafeReasonCode.ORIGIN_MISMATCH
    return None


def _deadline_remaining(context: ExecutionContext) -> float | None:
    if context.deadline_monotonic is None:
        return None
    return context.deadline_monotonic - time.monotonic()


def _deadline_expired(context: ExecutionContext) -> bool:
    remaining = _deadline_remaining(context)
    return remaining is not None and remaining <= 0


def _safe_reason(value, fallback: SafeReasonCode) -> SafeReasonCode:
    try:
        return SafeReasonCode(value)
    except (ValueError, TypeError):
        return fallback


def _session_reason(code: str) -> SafeReasonCode:
    return _safe_reason(code, SafeReasonCode.SESSION_LOST)


def _surface_reason(code: str) -> SafeReasonCode:
    aliases = {
        "TARGET_NOT_UNIQUE": SafeReasonCode.TARGET_AMBIGUOUS,
        "TARGET_NOT_FOUND": SafeReasonCode.TARGET_NOT_ACTIONABLE,
        "TARGET_NOT_VISIBLE": SafeReasonCode.TARGET_NOT_ACTIONABLE,
        "TARGET_NOT_AVAILABLE": SafeReasonCode.TARGET_NOT_ACTIONABLE,
        "FIELD_RELATION_UNVERIFIED": SafeReasonCode.DETAIL_NOT_READY,
        "ACCOUNT_NOT_PRESENT": SafeReasonCode.ACCOUNT_NOT_FOUND,
        "STALE_TARGET": SafeReasonCode.STALE_OBSERVATION,
        "STALE_RESOLVED_TARGET": SafeReasonCode.STALE_OBSERVATION,
        "UNSUPPORTED_ACTION": SafeReasonCode.ACTION_NOT_SUPPORTED,
        "ACTION_NOT_ALLOWED": SafeReasonCode.CONTROL_OPERATION_NOT_ALLOWED,
        "TARGET_ACTION_FAILED": SafeReasonCode.POSTCONDITION_UNKNOWN,
        "UNSAFE_DESTINATION": SafeReasonCode.RISK_NOT_READ_ONLY,
    }
    return aliases.get(code, _safe_reason(code, SafeReasonCode.INTERNAL_ERROR))


def _duration_ms(started: float) -> int:
    return max(0, min(3_600_000, int((time.monotonic() - started) * 1000)))


def _result(
    step_id: str,
    effect_state: EffectState,
    reason_code: SafeReasonCode,
    *,
    observation_id: str | None = None,
    value: SecretStr | None = None,
    membership_proof=None,
    evidence_refs=(),
    retry_safe: bool = False,
) -> ExecutionResult:
    return ExecutionResult(
        step_id=step_id,
        effect_state=effect_state,
        reason_code=reason_code,
        observation_id=observation_id,
        value=value,
        membership_proof=membership_proof,
        evidence_refs=tuple(evidence_refs),
        retry_safe=retry_safe,
    )
