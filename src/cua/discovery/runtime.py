"""Bounded discovery through sanitized structured decisions and the shared gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
import re
import secrets
import time
from types import MappingProxyType
from typing import Mapping

from pydantic import SecretStr, TypeAdapter, ValidationError

from cua.discovery.goals import BoundIntent
from cua.evidence.models import SafeReasonCode
from cua.execution.contracts import EffectState, ExecutionContext, ExecutionResult
from cua.models.actions import (
    BlockedDecision,
    ClickDecision,
    Decision,
    DoneDecision,
    WaitDecision,
)
from cua.models.bundles import (
    BundleStep,
    CapabilityContract,
    StepSource,
)
from cua.models.conditions import MembershipValidCondition, PageStateCondition
from cua.models.observations import Observation, ObservedControl
from cua.models.traces import CompletionProof, VerifiedDiscoveryTrace, VerifiedTraceEvent
from cua.models.verification import CompletionContext, MembershipProof
from cua.llm.decisions import (
    DecisionBackend,
    DecisionProviderError,
    DecisionReply,
    SafeControlChoice,
    SafeDecisionRequest,
    SafeHistoryItem,
    SafeObservationSummary,
    SafeSignals,
)
from cua.sessions.actor import ActorBusy, ActorPaused, ActorStaleEpoch
from cua.sessions.manager import SessionError, SessionManager
from cua.surface.playwright_surface import NormalizedView, PlaywrightSurface, SurfaceError
from cua.verification.completion import (
    CompletionVerifier,
    VerificationResult,
    VerificationStatus,
)


class DiscoveryStatus(StrEnum):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    BLOCKED = "BLOCKED"
    FAILURE = "FAILURE"
    TIME_LIMIT = "TIME_LIMIT"
    DECISION_LIMIT = "DECISION_LIMIT"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    status: DiscoveryStatus
    reason_code: str
    decisions_used: int
    verified_action_count: int
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    trace: VerifiedDiscoveryTrace | None = field(default=None, repr=False)
    verification: VerificationResult | None = field(default=None, repr=False)
    outputs: Mapping[str, SecretStr] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))

    def __repr__(self) -> str:
        return (
            "DiscoveryOutcome("
            f"status={self.status.value!r}, reason_code={self.reason_code!r}, "
            f"decisions_used={self.decisions_used}, "
            f"verified_action_count={self.verified_action_count}, model_id={self.model_id!r})"
        )


class DisabledDecisionBackend:
    """Production-safe default; a real provider needs explicit caller configuration."""

    model_id = "disabled"

    async def choose(self, request: SafeDecisionRequest, *, timeout_seconds: float) -> DecisionReply:
        raise DecisionProviderError("MODEL_NOT_CONFIGURED")


class DiscoveryRuntime:
    MAX_DECISIONS = 20
    MAX_SECONDS = 180.0
    _SAFE_GOAL = "Get the available balance for the requested savings account."
    _ACCOUNT_ID = re.compile(r"^[0-9]{1,20}$", re.ASCII)

    def __init__(
        self,
        sessions: SessionManager,
        surface: PlaywrightSurface,
        gateway,
        *,
        verifier: CompletionVerifier | None = None,
        backend: DecisionBackend | None = None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self._sessions = sessions
        self._surface = surface
        self._gateway = gateway
        self._verifier = verifier or CompletionVerifier()
        self._backend = backend or DisabledDecisionBackend()
        self._clock = clock
        self._sleep = sleep

    async def run(
        self,
        context: ExecutionContext,
        intent: BoundIntent,
        contract: CapabilityContract,
        *,
        max_decisions: int = MAX_DECISIONS,
        timeout_seconds: float = MAX_SECONDS,
    ) -> DiscoveryOutcome:
        """Explore a sanitized page summary and stop at the first uncertain effect."""
        try:
            context = ExecutionContext.model_validate(context.model_dump(mode="python"))
            contract = CapabilityContract.model_validate(contract.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            return self._outcome(DiscoveryStatus.FAILURE, "INPUT_INVALID", 0, 0)
        if (
            not isinstance(intent, BoundIntent)
            or intent.code != "get_savings_balance"
            or intent.safe_goal != self._SAFE_GOAL
            or not isinstance(intent.requested_account_id, SecretStr)
            or type(max_decisions) is not int
            or not 1 <= max_decisions <= self.MAX_DECISIONS
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= self.MAX_SECONDS
        ):
            return self._outcome(DiscoveryStatus.FAILURE, "INPUT_INVALID", 0, 0)
        requested = intent.requested_account_id.get_secret_value()
        supplied = context.input_bindings.get("inputs.account_id")
        if (
            not self._ACCOUNT_ID.fullmatch(requested)
            or
            set(context.input_bindings) != {"inputs.account_id"}
            or supplied is None
            or supplied.get_secret_value() != requested
            or context.membership_proof is not None
        ):
            return self._outcome(DiscoveryStatus.FAILURE, "INPUT_INVALID", 0, 0)
        if isinstance(self._backend, DisabledDecisionBackend):
            return self._outcome(DiscoveryStatus.FAILURE, "MODEL_NOT_CONFIGURED", 0, 0)

        start = self._clock()
        deadline = start + float(timeout_seconds)
        if context.deadline_monotonic is not None:
            deadline = min(deadline, context.deadline_monotonic)
        decisions_used = 0
        verified_events: list[VerifiedTraceEvent] = []
        history: list[SafeHistoryItem] = []
        membership_proof: MembershipProof | None = None
        proof_from_gateway = False
        latest_model: str | None = None
        input_tokens = 0
        output_tokens = 0
        usage_complete = True
        trace_id = "trace_" + secrets.token_hex(8)

        while decisions_used < max_decisions:
            if self._clock() >= deadline:
                return self._outcome(
                    DiscoveryStatus.TIME_LIMIT,
                    "TIME_LIMIT",
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )
            try:
                observation, view = await self._observe(context)
            except (SessionError, SurfaceError, ActorBusy, ActorPaused, ActorStaleEpoch) as error:
                return self._outcome(
                    *_observation_failure(error),
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )
            if view.principal_matches is not True:
                reason = "SUBJECT_MISMATCH" if view.principal_matches is False else "PRINCIPAL_UNKNOWN"
                return self._outcome(
                    DiscoveryStatus.FAILURE,
                    reason,
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )

            if view.page_state == "ACCESS_DENIED":
                return self._outcome(
                    DiscoveryStatus.BUSINESS_OUTCOME,
                    "ACCESS_DENIED",
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )
            if view.page_state == "APP_ERROR":
                return self._outcome(
                    DiscoveryStatus.FAILURE,
                    "APP_ERROR",
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )
            if view.unknown_blocker:
                return self._outcome(
                    DiscoveryStatus.BLOCKED,
                    "UNKNOWN_BLOCKER",
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )

            if view.safe_route == "accounts_overview" and view.page_state == "OVERVIEW_READY":
                if view.overview_complete is not True:
                    continue
                if requested not in view.account_ids:
                    return self._outcome(
                        DiscoveryStatus.BUSINESS_OUTCOME,
                        "ACCOUNT_NOT_FOUND",
                        decisions_used,
                        len(verified_events),
                        latest_model,
                        input_tokens if usage_complete else None,
                        output_tokens if usage_complete else None,
                    )
                try:
                    membership_proof = view.membership_proof(
                        run_ref=context.run_alias,
                        binding_ref="inputs.account_id",
                        binding_value=SecretStr(requested),
                    )
                except SurfaceError:
                    membership_proof = None
                    proof_from_gateway = False

            safe_request = _safe_request(
                observation,
                view,
                intent,
                membership_proof,
                context,
                tuple(history[-5:]),
            )
            remaining = deadline - self._clock()
            if remaining <= 0:
                continue
            decisions_used += 1
            try:
                reply = await asyncio.wait_for(
                    self._backend.choose(safe_request, timeout_seconds=remaining),
                    timeout=remaining,
                )
            except (TimeoutError, asyncio.TimeoutError):
                usage_complete = False
                return self._outcome(
                    DiscoveryStatus.TIME_LIMIT,
                    "MODEL_TIMEOUT",
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )
            except DecisionProviderError as error:
                usage_complete = False
                if error.code == "MODEL_RESPONSE_INVALID":
                    history.append(_history("REJECTED", "none", "NOT_DISPATCHED", "MODEL_RESPONSE_INVALID"))
                    continue
                reason = error.code if error.code in {
                    "MODEL_NOT_CONFIGURED",
                    "PROVIDER_CREDENTIAL_MISSING",
                    "PROVIDER_TIMEOUT",
                } else "MODEL_UNAVAILABLE"
                return self._outcome(
                    DiscoveryStatus.FAILURE,
                    reason,
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )
            except Exception:
                usage_complete = False
                return self._outcome(
                    DiscoveryStatus.FAILURE,
                    "MODEL_UNAVAILABLE",
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )

            if not isinstance(reply, DecisionReply):
                usage_complete = False
                history.append(_history("REJECTED", "none", "NOT_DISPATCHED", "MODEL_RESPONSE_INVALID"))
                continue
            latest_model = reply.model_id
            if reply.input_tokens is None or reply.output_tokens is None:
                usage_complete = False
            else:
                input_tokens += reply.input_tokens
                output_tokens += reply.output_tokens
            try:
                decision = TypeAdapter(Decision).validate_python(reply.decision.model_dump(mode="python"))
            except (AttributeError, ValidationError):
                history.append(_history("REJECTED", "none", "NOT_DISPATCHED", "MODEL_RESPONSE_INVALID"))
                continue
            if decision.observation_id != observation.id:
                history.append(_history("REJECTED", "none", "NOT_DISPATCHED", "STALE_OBSERVATION"))
                continue

            if isinstance(decision, BlockedDecision):
                history.append(_history("BLOCKED", "none", "NOT_DISPATCHED", "MODEL_BLOCKED"))
                return self._outcome(
                    DiscoveryStatus.BLOCKED,
                    "MODEL_BLOCKED",
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )
            if isinstance(decision, WaitDecision):
                history.append(_history("WAIT", "none", "NOT_DISPATCHED", "WAIT_REQUESTED"))
                wait_seconds = min(decision.timeout_ms / 1000, max(0.0, deadline - self._clock()))
                if wait_seconds:
                    await self._sleep(wait_seconds)
                continue
            if isinstance(decision, DoneDecision):
                try:
                    current = await self._try_done(
                        context,
                        intent,
                        contract,
                        membership_proof,
                        proof_from_gateway,
                        deadline,
                    )
                except (SessionError, SurfaceError, ActorBusy, ActorPaused, ActorStaleEpoch) as error:
                    return self._outcome(
                        *_observation_failure(error),
                        decisions_used,
                        len(verified_events),
                        latest_model,
                        input_tokens if usage_complete else None,
                        output_tokens if usage_complete else None,
                    )
                if current is None:
                    history.append(_history("DONE", "none", "NOT_DISPATCHED", "DONE_NOT_VERIFIED"))
                    continue
                verified_view, proof, verification, current_observation = current
                if verification.status is VerificationStatus.SUCCESS:
                    if not verified_events:
                        return self._outcome(
                            DiscoveryStatus.FAILURE,
                            "NO_VERIFIED_ACTIONS",
                            decisions_used,
                            0,
                            latest_model,
                            input_tokens if usage_complete else None,
                            output_tokens if usage_complete else None,
                            verification=verification,
                        )
                    trace = _successful_trace(
                        trace_id,
                        context,
                        proof,
                        current_observation,
                        verified_events,
                    )
                    return self._outcome(
                        DiscoveryStatus.SUCCESS,
                        "VERIFIED",
                        decisions_used,
                        len(verified_events),
                        latest_model,
                        input_tokens if usage_complete else None,
                        output_tokens if usage_complete else None,
                        trace=trace,
                        verification=verification,
                        outputs=verification.outputs,
                    )
                if verification.status is VerificationStatus.UNKNOWN:
                    history.append(_history("DONE", "none", "NOT_DISPATCHED", "DONE_NOT_VERIFIED"))
                    continue
                return self._outcome(
                    DiscoveryStatus.FAILURE,
                    verification.reason_code,
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                    verification=verification,
                )
            if not isinstance(decision, ClickDecision):
                history.append(_history("REJECTED", "none", "NOT_DISPATCHED", "ACTION_NOT_SUPPORTED"))
                continue

            allowed = {item.control_ref: item for item in safe_request.observation.controls}
            chosen = allowed.get(decision.control_ref)
            if chosen is None or "CLICK" not in observation_control_operations(observation, decision.control_ref):
                history.append(_history("REJECTED", "none", "NOT_DISPATCHED", "ACTION_NOT_ALLOWED"))
                continue
            dispatch_context = context.model_copy(
                update={"membership_proof": membership_proof if _proof_matches(context, intent, membership_proof) else None}
            )
            try:
                result = await self._gateway.dispatch_observed(dispatch_context, decision)
            except Exception:
                return self._outcome(
                    DiscoveryStatus.FAILURE,
                    "GATEWAY_UNAVAILABLE",
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )

            target_alias = _target_alias(chosen)
            if result.effect_state is EffectState.VERIFIED:
                step = _verified_click_step(len(verified_events) + 1, chosen)
                event_id = f"event_{len(verified_events) + 1}"
                verified_events.append(
                    VerifiedTraceEvent(
                        event_id=event_id,
                        step=step,
                        effect_state=EffectState.VERIFIED.value,
                    )
                )
                history.append(_history("CLICK", target_alias, "VERIFIED", result.reason_code.value))
                if chosen.safe_name == "<requested_account>":
                    proof = result.membership_proof
                    if _proof_matches(context, intent, proof):
                        membership_proof = proof
                        proof_from_gateway = True
                    else:
                        return self._outcome(
                            DiscoveryStatus.FAILURE,
                            "MEMBERSHIP_PROOF_INVALID",
                            decisions_used,
                            len(verified_events),
                            latest_model,
                            input_tokens if usage_complete else None,
                            output_tokens if usage_complete else None,
                        )
                continue

            reason = result.reason_code.value if hasattr(result.reason_code, "value") else str(result.reason_code)
            history.append(_history("REJECTED", target_alias, result.effect_state.value, reason))
            if result.effect_state in {EffectState.DISPATCHED, EffectState.OUTCOME_UNKNOWN}:
                return self._outcome(
                    DiscoveryStatus.INCOMPLETE,
                    reason if result.effect_state is EffectState.OUTCOME_UNKNOWN else "OUTCOME_UNKNOWN",
                    decisions_used,
                    len(verified_events),
                    latest_model,
                    input_tokens if usage_complete else None,
                    output_tokens if usage_complete else None,
                )
            if reason == "STALE_OBSERVATION":
                continue
            return self._outcome(
                DiscoveryStatus.FAILURE,
                reason,
                decisions_used,
                len(verified_events),
                latest_model,
                input_tokens if usage_complete else None,
                output_tokens if usage_complete else None,
            )

        return self._outcome(
            DiscoveryStatus.DECISION_LIMIT,
            "DECISION_LIMIT",
            decisions_used,
            len(verified_events),
            latest_model,
            input_tokens if usage_complete else None,
            output_tokens if usage_complete else None,
        )

    async def _observe(
        self,
        context: ExecutionContext,
    ) -> tuple[Observation, NormalizedView]:
        state = await self._sessions.get_state(context.session_id)
        _require_session_match(state, context)

        async def observe_serialized():
            current_state = await self._sessions.get_state(context.session_id)
            _require_session_match(current_state, context)
            observation, view = await self._surface.observe(
                context.session_id,
                bindings=context.input_bindings,
            )
            _require_view_match(observation, view, context)
            return observation, view

        return await state.actor.submit(
            expected_epoch=context.expected_epoch,
            run_id=context.run_alias,
            operation=observe_serialized,
        )

    async def _try_done(
        self,
        context: ExecutionContext,
        intent: BoundIntent,
        contract: CapabilityContract,
        membership_proof: MembershipProof | None,
        proof_from_gateway: bool,
        deadline: float,
    ):
        if self._clock() >= deadline:
            return None
        try:
            state = await self._sessions.get_state(context.session_id)
            _require_session_match(state, context)

            async def verify_serialized():
                current_state = await self._sessions.get_state(context.session_id)
                _require_session_match(current_state, context)
                observation, view = await self._surface.observe(
                    context.session_id,
                    bindings=context.input_bindings,
                )
                _require_view_match(observation, view, context)
                if (
                    not proof_from_gateway
                    or not _proof_matches(context, intent, membership_proof)
                    or membership_proof is None
                    or view.safe_route != "account_details"
                    or view.page_state != "DETAIL_READY"
                    or view.detail_identity_match is not True
                ):
                    return None
                completion_view = view.completion_view(run_ref=context.run_alias)
                completion_context = CompletionContext(
                    run_ref=context.run_alias,
                    session_ref=context.session_id,
                    authentication_generation=context.authentication_generation,
                    account_binding_ref="inputs.account_id",
                    target_origin=context.target_origin,
                    approved_profile=context.profile_id,
                    requested_account_id=intent.requested_account_id,
                    membership_proof=membership_proof,
                )
                verification = self._verifier.verify(contract, completion_view, completion_context)
                return view, membership_proof, verification, observation

            return await state.actor.submit(
                expected_epoch=context.expected_epoch,
                run_id=context.run_alias,
                operation=verify_serialized,
            )
        except SurfaceError as error:
            if error.code in {
                "SESSION_EXPIRED",
                "SESSION_LOST",
                "SUBJECT_MISMATCH",
                "APP_ERROR",
                "ACCESS_DENIED",
                "AMBIGUOUS_STATE",
            }:
                raise
            return None

    def _outcome(
        self,
        status: DiscoveryStatus,
        reason: str,
        decisions: int,
        verified_actions: int,
        model_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        *,
        trace: VerifiedDiscoveryTrace | None = None,
        verification: VerificationResult | None = None,
        outputs: Mapping[str, SecretStr] | None = None,
    ) -> DiscoveryOutcome:
        return DiscoveryOutcome(
            status=status,
            reason_code=reason,
            decisions_used=decisions,
            verified_action_count=verified_actions,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            trace=trace,
            verification=verification,
            outputs=outputs or {},
        )


def _safe_request(
    observation: Observation,
    view: NormalizedView,
    intent: BoundIntent,
    proof: MembershipProof | None,
    context: ExecutionContext,
    recent: tuple[SafeHistoryItem, ...],
) -> SafeDecisionRequest:
    requested = intent.requested_account_id.get_secret_value()
    proof_valid = _proof_matches(context, intent, proof)
    requested_present = None
    if view.overview_complete is True:
        requested_present = requested in view.account_ids
    elif proof_valid:
        requested_present = True

    signals = SafeSignals(
        principal_matches=view.principal_matches,
        overview_complete=view.overview_complete,
        requested_account_present=requested_present,
        membership_valid=proof_valid,
        requested_account_matches=view.detail_identity_match,
        account_type_is_savings=_account_type_is_savings(view),
        available_balance_parseable="PROFILE_AVAILABLE_BALANCE" in view.parseable_fields,
    )
    controls = tuple(
        SafeControlChoice(
            control_ref=control.ref,
            frame_ref=control.frame_ref,
            role=control.role,
            safe_name=control.safe_name,
        )
        for control in observation.controls
        if _control_allowed_for_intent(control, view)
    )
    return SafeDecisionRequest(
        intent="get_savings_balance",
        safe_goal=intent.safe_goal,
        requested_account_alias="<requested_account>",
        observation=SafeObservationSummary(
            observation_id=observation.id,
            route=observation.safe_route if observation.safe_route in {
                "home", "accounts_overview", "account_details"
            } else "other",
            page_state=view.page_state,
            controls=controls,
        ),
        signals=signals,
        recent=recent,
    )


def _control_allowed_for_intent(control: ObservedControl, view: NormalizedView) -> bool:
    if (
        control.frame_ref != "f_main"
        or not control.visible
        or not control.enabled
        or "CLICK" not in control.allowed_operations
    ):
        return False
    if control.safe_name == "Accounts Overview":
        return view.safe_route in {"home", "accounts_overview", "account_details"}
    if (
        control.safe_name == "<requested_account>"
        and control.binding_ref == "inputs.account_id"
    ):
        return view.safe_route == "accounts_overview" and view.page_state == "OVERVIEW_READY"
    return False


def _account_type_is_savings(view: NormalizedView) -> bool | None:
    values = view.field_values.get("PROFILE_ACCOUNT_TYPE", ())
    if len(values) != 1:
        return None
    return values[0].get_secret_value().strip() == "SAVINGS"


def _proof_matches(
    context: ExecutionContext,
    intent: BoundIntent,
    proof: MembershipProof | None,
) -> bool:
    if not isinstance(proof, MembershipProof):
        return False
    now_ms = time.monotonic_ns() // 1_000_000
    requested = intent.requested_account_id.get_secret_value()
    return (
        proof.run_ref == context.run_alias
        and proof.session_ref == context.session_id
        and proof.authentication_generation == context.authentication_generation
        and proof.account_binding_ref == "inputs.account_id"
        and proof.account_binding_value.get_secret_value() == requested
        and proof.overview_complete is True
        and proof.account_present is True
        and 0 <= now_ms - proof.verified_monotonic_ms <= int(DiscoveryRuntime.MAX_SECONDS * 1000)
    )


def _require_session_match(state, context: ExecutionContext) -> None:
    if state.state != "ACTIVE":
        reason = state.state if state.state in {
            "SESSION_LOST", "SESSION_EXPIRED", "SUBJECT_MISMATCH"
        } else "SESSION_LOST"
        raise SessionError(reason, context.session_id)
    if state.profile_id != context.profile_id:
        raise SessionError("PROFILE_MISMATCH", context.session_id)
    if state.origin != context.target_origin:
        raise SessionError("ORIGIN_MISMATCH", context.session_id)
    if state.auth_generation != context.authentication_generation:
        raise SessionError("AUTHENTICATION_CHANGED", context.session_id)


def _require_view_match(
    observation: Observation,
    view: NormalizedView,
    context: ExecutionContext,
) -> None:
    if (
        observation.id != view.observation_id
        or observation.safe_route != view.safe_route
        or view.page_state not in observation.state_tags
        or observation.session_id != context.session_id
        or view.session_id != context.session_id
        or view.authentication_generation != context.authentication_generation
        or view.profile_id != context.profile_id
        or view.origin != context.target_origin
    ):
        raise SurfaceError("OBSERVATION_SESSION_MISMATCH")


def _observation_failure(error: Exception) -> tuple[DiscoveryStatus, str]:
    if isinstance(error, ActorPaused):
        return DiscoveryStatus.BLOCKED, SafeReasonCode.ACTOR_PAUSED.value
    if isinstance(error, ActorStaleEpoch):
        return DiscoveryStatus.BLOCKED, SafeReasonCode.STALE_EPOCH.value
    if isinstance(error, ActorBusy):
        return DiscoveryStatus.FAILURE, "SESSION_BUSY"
    code = getattr(error, "code", None)
    if code == "SESSION_EXPIRED":
        return DiscoveryStatus.BLOCKED, code
    if code == "ACCESS_DENIED":
        return DiscoveryStatus.BUSINESS_OUTCOME, code
    if code == "UNKNOWN_BLOCKER":
        return DiscoveryStatus.BLOCKED, code
    if code in {item.value for item in SafeReasonCode}:
        return DiscoveryStatus.FAILURE, code
    return DiscoveryStatus.FAILURE, "SESSION_OR_OBSERVATION_UNAVAILABLE"


def _target_alias(control: SafeControlChoice) -> str:
    if control.safe_name == "<requested_account>":
        return "requested_account"
    if control.safe_name == "Accounts Overview":
        return "accounts_overview"
    return "none"


def _verified_click_step(ordinal: int, control: SafeControlChoice) -> BundleStep:
    recovery_ref = None
    if control.safe_name == "Accounts Overview":
        target_ref = "overview_nav"
        post_route = "OVERVIEW_READY"
        preconditions = ()
    elif control.safe_name == "<requested_account>" and control.frame_ref == "f_main":
        target_ref = "requested_account_link"
        post_route = "DETAIL_READY"
        preconditions = (
            MembershipValidCondition(kind="membership_valid", input_ref="inputs.account_id"),
        )
        recovery_ref = "readonly_overview_anchor"
    else:
        raise ValueError("control is outside the supported observed action space")
    return BundleStep(
        id=f"click_{ordinal}",
        kind="CLICK",
        target_ref=target_ref,
        preconditions=preconditions,
        postconditions=(PageStateCondition(kind="page_state", value=post_route),),
        recovery_ref=recovery_ref,
        source=StepSource(type="observed", event_ids=(f"event_{ordinal}",)),
    )


def _successful_trace(
    trace_id: str,
    context: ExecutionContext,
    proof: MembershipProof,
    observation: Observation,
    events: list[VerifiedTraceEvent],
) -> VerifiedDiscoveryTrace:
    completion = CompletionProof(
        proof_id="completion_" + secrets.token_hex(8),
        trace_id=trace_id,
        session_ref=context.session_id,
        account_binding_ref="inputs.account_id",
        observation_id=observation.id,
        membership_proof_id=proof.proof_ref,
        authentication_generation=context.authentication_generation,
    )
    return VerifiedDiscoveryTrace(
        trace_id=trace_id,
        success=True,
        completion_proof=completion,
        events=tuple(events),
    )


def _history(
    decision: str,
    target: str,
    effect_state: str,
    reason_code: str,
) -> SafeHistoryItem:
    safe_decision = decision if decision in {"CLICK", "WAIT", "DONE", "BLOCKED"} else "REJECTED"
    safe_target = target if target in {"requested_account", "accounts_overview"} else "none"
    safe_effect = effect_state if effect_state in {"VERIFIED", "NOT_DISPATCHED", "OUTCOME_UNKNOWN"} else "NOT_DISPATCHED"
    safe_reason = reason_code if reason_code and len(reason_code) <= 64 and reason_code.isascii() else "UNKNOWN"
    return SafeHistoryItem(
        decision=safe_decision,
        target_alias=safe_target,
        effect_state=safe_effect,
        reason_code=safe_reason,
    )


def observation_control_operations(observation: Observation, control_ref: str) -> tuple[str, ...]:
    control = next((item for item in observation.controls if item.ref == control_ref), None)
    return () if control is None else control.allowed_operations
