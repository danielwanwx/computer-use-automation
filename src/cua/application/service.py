"""Local orchestration boundary for prepared sessions and ephemeral runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Awaitable, Callable, Mapping

from pydantic import SecretStr, ValidationError

from cua.application.config import ApplicationConfig, ProviderMode
from cua.application.handoff import ApplicationHandoffCoordinator
from cua.application.oracle import SubprocessValidationOracle
from cua.application.contracts import (
    ApprovalRecord,
    CapabilityDetail,
    CapabilityLifecycle,
    CapabilitySummary,
    DiscoveryRequest,
    OracleReport,
    ReplayRequest,
    RunHandle,
    RunMode,
    RunView,
    ServiceError,
    SessionView,
    ValidationFixture,
    ValidationOracle,
    ValidationReport,
)
from cua.compiler import CapabilityCompiler, CompilationError
from cua.conditions.parsers import USDDecimalParser
from cua.discovery import (
    DiscoveryOutcome,
    DiscoveryRuntime,
    DiscoveryStatus,
    GoalBindError,
    GoalBinder,
    parabank_savings_balance_blueprint,
)
from cua.discovery.runtime import DisabledDecisionBackend
from cua.evidence import EvidenceError, EvidenceSink
from cua.evidence.models import RunMetadata, RunMode as EvidenceRunMode
from cua.evidence.models import RunState, SafeReasonCode
from cua.execution import (
    EffectState,
    ExecutionContext,
    ExecutionGateway,
    FailureDetail,
    InvocationResult,
    InvocationStatus,
)
from cua.llm.decisions import (
    CodexDecisionBackend,
    DecisionBackend,
    DecisionProviderError,
    OpenAIResponsesDecisionBackend,
)
from cua.llm.local_agents import resolve_decision_backend
from cua.handoff import HandoffError, HandoffService, HandoffState, InterventionView
from cua.models.bundles import BundleReference
from cua.models.qualification import ValidationQualification
from cua.policy import PolicyContext, PolicyEngine
from cua.profiles.parabank import PROFILE_ID
from cua.registry import (
    BundleNotFoundError,
    BundleRegistry,
    DigestMismatchError,
    ImmutableRevisionError,
    InvalidBundleError,
    QualificationMismatchError,
)
from cua.registry import RegistryRevision
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from cua.replay import ReplayRuntime
from cua.replay.runtime import _supported_runtime_contract
from cua.sessions import (
    ActorBusy,
    ActorPaused,
    ActorStaleEpoch,
    PrincipalSpec,
    SessionError,
    SessionHandle,
    SessionManager,
    SessionState,
    ValidationSessionBinding,
)
from cua.surface import PlaywrightSurface
from cua.verification import CompletionVerifier


_RUN_ID = re.compile(r"^run_[a-f0-9]{16}$", re.ASCII)
_ACCOUNT_ID = re.compile(r"^[0-9]{1,20}$", re.ASCII)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,96}$", re.ASCII)
SUPPORTED_CAPABILITY = "get_savings_balance"
SUPPORTED_CAPABILITY_VERSION = "1.0.0"
_LOCAL_OPERATOR = "local_operator"


@dataclass(slots=True, repr=False)
class _RunRecord:
    run_id: str
    session_id: str
    mode: RunMode
    state: RunState
    created_at_ms: int
    updated_at_ms: int
    actor: object = field(repr=False)
    capability_reference: BundleReference | None = None
    intervention_id: str | None = None
    validation_report: ValidationReport | None = field(default=None, repr=False)
    result: InvocationResult | DiscoveryOutcome | None = field(default=None, repr=False)
    result_expires_at: float | None = field(default=None, repr=False)
    expiry_task: asyncio.Task | None = field(default=None, repr=False)
    result_expired: bool = False
    outcome_code: SafeReasonCode | None = None
    decisions_used: int = 0
    verified_action_count: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)
    started: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass(slots=True, repr=False)
class _RunExecution:
    result: InvocationResult | DiscoveryOutcome
    capability_reference: BundleReference | None = None
    validation_report: ValidationReport | None = None


RunCallable = Callable[
    [ExecutionContext], Awaitable[InvocationResult | DiscoveryOutcome | _RunExecution]
]


class ApplicationService:
    """Owns sessions, run tasks, HMAC request deduplication, and volatile results."""

    def __init__(
        self,
        config: ApplicationConfig,
        *,
        sessions: SessionManager,
        discovery: DiscoveryRuntime,
        replay: ReplayRuntime,
        registry: BundleRegistry,
        evidence: EvidenceSink,
        decision_backend: DecisionBackend | None = None,
        validation_oracle: ValidationOracle | None = None,
        handoff_coordinator: ApplicationHandoffCoordinator | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._sessions = sessions
        self._discovery = discovery
        self._replay = replay
        self._registry = registry
        self._evidence = evidence
        self._decision_backend = decision_backend
        self._validation_oracle = validation_oracle
        self._handoff_coordinator = handoff_coordinator
        self._clock = clock if clock is not None else time.monotonic
        self._goal_binder = GoalBinder()
        self._compiler = CapabilityCompiler()
        self._idempotency_secret = secrets.token_bytes(32)
        self._submission_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._validation_start_lock = asyncio.Lock()
        self._session_handles: dict[str, SessionHandle] = {}
        self._session_views: dict[str, SessionView] = {}
        self._validation_sessions: set[str] = set()
        self._closing_sessions: set[str] = set()
        self._runs: dict[str, _RunRecord] = {}
        self._active_by_session: dict[str, str] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._validation_reports: dict[str, ValidationReport] = {}
        self._tasks: set[asyncio.Task] = set()
        self._shutdown = False
        self._shutdown_complete = False

    @classmethod
    def from_config(
        cls,
        config: ApplicationConfig,
        *,
        decision_backend: DecisionBackend | None = None,
        validation_oracle: ValidationOracle | None = None,
    ) -> ApplicationService:
        """Create the production composition without starting a browser or provider."""
        if config.provider_mode is ProviderMode.OPENAI:
            if decision_backend is None:
                decision_backend = OpenAIResponsesDecisionBackend(config.provider_model or "")
        elif config.provider_mode is ProviderMode.CODEX:
            if decision_backend is None:
                decision_backend = CodexDecisionBackend(
                    config.provider_model,
                    executable=config.codex_executable,
                )
        elif config.provider_mode in {ProviderMode.AUTO, ProviderMode.CLAUDE_CODE, ProviderMode.CURSOR}:
            if decision_backend is None:
                try:
                    decision_backend, _ = resolve_decision_backend(
                        config.provider_mode.value,
                        model=config.provider_model,
                        codex_executable=config.codex_executable,
                    )
                except DecisionProviderError:
                    # Nothing to borrow on this machine; discovery reports
                    # MODEL_NOT_CONFIGURED while replay keeps working.
                    decision_backend = None
        elif decision_backend is not None:
            raise ServiceError(409, "MODEL_NOT_CONFIGURED")
        if validation_oracle is None and config.validation_oracle_command is not None:
            validation_oracle = SubprocessValidationOracle(
                config.validation_oracle_command,
                timeout_seconds=config.validation_oracle_timeout_seconds,
                max_bytes=config.validation_oracle_max_bytes,
            )

        evidence = EvidenceSink(config.evidence_root)
        registry = BundleRegistry(config.registry_root)
        sessions = SessionManager(
            config.principal_specs,
            origin=config.target_origin,
            target_lock_path=config.target_lock_path,
            browser_channel=config.browser_channel,
            headless=config.headless,
            timeout_ms=config.browser_timeout_ms,
            validation_principal_aliases=config.validation_principal_aliases,
        )
        surface = PlaywrightSurface(sessions)
        policy = PolicyEngine()
        gateway = ExecutionGateway(sessions, surface, policy, evidence)
        verifier = CompletionVerifier()
        service_holder: dict[str, ApplicationService] = {}

        async def handoff_state_callback(view: InterventionView) -> None:
            service = service_holder.get("service")
            if service is not None:
                await service._on_intervention_state(view)

        handoff_service = HandoffService(
            authorize_operator=lambda operator_ref: operator_ref == _LOCAL_OPERATOR
        )
        handoff_coordinator = ApplicationHandoffCoordinator(
            handoff_service,
            state_callback=handoff_state_callback,
        )
        replay = ReplayRuntime(
            registry,
            sessions,
            surface,
            gateway,
            verifier,
            evidence,
            target_revision=config.target_revision,
            run_timeout_seconds=config.run_timeout_seconds,
            handoff_notifier=handoff_coordinator,
        )
        discovery = DiscoveryRuntime(
            sessions,
            surface,
            gateway,
            verifier=verifier,
            backend=decision_backend,
        )
        service = cls(
            config,
            sessions=sessions,
            discovery=discovery,
            replay=replay,
            registry=registry,
            evidence=evidence,
            decision_backend=decision_backend,
            validation_oracle=validation_oracle,
            handoff_coordinator=handoff_coordinator,
        )
        service_holder["service"] = service
        return service

    async def prepare_session(self, principal_alias: str) -> SessionView:
        if self._shutdown:
            raise ServiceError(503, "SERVICE_SHUTDOWN")
        if principal_alias not in self._ordinary_principal_aliases:
            raise ServiceError(422, "INPUT_INVALID")
        async with self._lifecycle_lock:
            try:
                handle = await self._sessions.prepare(principal_alias)
            except SessionError as error:
                if error.session_id and error.code in {"SESSION_EXPIRED", "SUBJECT_MISMATCH"}:
                    # These failures intentionally retain the browser context so a
                    # handoff may inspect it. Initial preparation has no run owner,
                    # so close it here instead of orphaning its context and lock.
                    try:
                        await self._sessions.close(error.session_id)
                    except Exception:
                        pass
                raise _session_service_error(error) from None
            except Exception:
                raise ServiceError(503, "SESSION_LOST") from None
            view = _session_view(handle)
            async with self._submission_lock:
                self._session_handles[handle.session_id] = handle
                self._session_views[handle.session_id] = view
            return view

    async def list_sessions(self) -> tuple[SessionView, ...]:
        async with self._submission_lock:
            session_ids = tuple(
                session_id
                for session_id in self._session_views
                if session_id not in self._validation_sessions
            )
        views: list[SessionView] = []
        for session_id in session_ids:
            try:
                state = await self._sessions.get_state(session_id)
            except SessionError:
                continue
            if state.state == "CLOSED":
                continue
            views.append(_session_view_from_state(state))
        return tuple(sorted(views, key=lambda item: item.principal_alias))

    async def list_interventions(
        self,
        *,
        operator_ref: str = _LOCAL_OPERATOR,
        session_id: str | None = None,
    ) -> tuple[InterventionView, ...]:
        """List safe interventions through the server-owned operator identity."""
        self._require_local_operator(operator_ref)
        coordinator = self._require_handoff_coordinator()
        try:
            views = await coordinator.list(
                operator_ref=operator_ref,
                session_id=session_id,
            )
        except HandoffError as error:
            raise _handoff_service_error(error) from None
        except Exception:
            raise ServiceError(503, "HANDOFF_UNAVAILABLE") from None
        return _safe_intervention_views(views)

    async def get_intervention(
        self,
        intervention_id: str,
        *,
        operator_ref: str = _LOCAL_OPERATOR,
    ) -> InterventionView:
        """Read one value-safe intervention view."""
        self._require_local_operator(operator_ref)
        coordinator = self._require_handoff_coordinator()
        try:
            view = await coordinator.get(intervention_id, operator_ref=operator_ref)
        except HandoffError as error:
            raise _handoff_service_error(error) from None
        except Exception:
            raise ServiceError(503, "HANDOFF_UNAVAILABLE") from None
        return _safe_intervention_view(view)

    async def claim_intervention(
        self,
        intervention_id: str,
        *,
        expected_epoch: int,
        operator_ref: str = _LOCAL_OPERATOR,
    ) -> InterventionView:
        """Claim an intervention at the exact epoch shown to the operator."""
        self._validate_intervention_epoch(expected_epoch)
        self._require_local_operator(operator_ref)
        coordinator = self._require_handoff_coordinator()
        try:
            view = await coordinator.claim(
                intervention_id,
                operator_ref=operator_ref,
                expected_epoch=expected_epoch,
            )
        except HandoffError as error:
            raise _handoff_service_error(error) from None
        except Exception:
            raise ServiceError(503, "HANDOFF_UNAVAILABLE") from None
        return _safe_intervention_view(view)

    async def resume_intervention(
        self,
        intervention_id: str,
        *,
        expected_epoch: int,
        operator_ref: str = _LOCAL_OPERATOR,
    ) -> InterventionView:
        """Resume an intervention only with its current server-held epoch."""
        self._validate_intervention_epoch(expected_epoch)
        self._require_local_operator(operator_ref)
        coordinator = self._require_handoff_coordinator()
        try:
            view = await coordinator.resume(
                intervention_id,
                operator_ref=operator_ref,
                expected_epoch=expected_epoch,
            )
        except HandoffError as error:
            raise _handoff_service_error(error) from None
        except Exception:
            raise ServiceError(503, "HANDOFF_UNAVAILABLE") from None
        return _safe_intervention_view(view)

    async def abort_intervention(
        self,
        intervention_id: str,
        *,
        expected_epoch: int,
        operator_ref: str = _LOCAL_OPERATOR,
    ) -> InterventionView:
        """Abort an intervention at the exact displayed epoch."""
        self._validate_intervention_epoch(expected_epoch)
        self._require_local_operator(operator_ref)
        coordinator = self._require_handoff_coordinator()
        try:
            view = await coordinator.abort(
                intervention_id,
                operator_ref=operator_ref,
                expected_epoch=expected_epoch,
            )
        except HandoffError as error:
            raise _handoff_service_error(error) from None
        except Exception:
            raise ServiceError(503, "HANDOFF_UNAVAILABLE") from None
        return _safe_intervention_view(view)

    async def start_discovery(self, request: DiscoveryRequest) -> RunHandle:
        if self._shutdown:
            raise ServiceError(503, "SERVICE_SHUTDOWN")
        request = _validated_request(DiscoveryRequest, request)
        if request.capability_version != SUPPORTED_CAPABILITY_VERSION:
            raise ServiceError(422, "UNSUPPORTED_CAPABILITY_VERSION")
        try:
            intent = self._goal_binder.bind(
                request.goal.get_secret_value(),
                request.inputs,
            )
        except GoalBindError as error:
            raise ServiceError(422, error.code) from None

        self._require_discovery_provider()
        state = await self._registered_active_state(request.session_id)
        blueprint = parabank_savings_balance_blueprint(
            reviewer_ref="local_operator",
            capability_version=request.capability_version,
        )
        payload = {
            "target_id": request.target_id,
            "goal": request.goal.get_secret_value(),
            "inputs": _plain_bindings(request.inputs),
            "capability_version": request.capability_version,
        }
        async def run(run_context: ExecutionContext):
            outcome = await self._discovery.run(
                run_context,
                intent,
                blueprint.contract,
                timeout_seconds=self._config.run_timeout_seconds,
            )
            if not isinstance(outcome, DiscoveryOutcome):
                return _RunExecution(_internal_failure(run_context.run_alias, RunMode.DISCOVERY))
            if outcome.status is not DiscoveryStatus.SUCCESS:
                return _RunExecution(outcome)
            # Production composition always uses BundleRegistry. A deliberately
            # minimal injected test registry has no draft store and exercises only
            # discovery result plumbing; it cannot create a persisted capability.
            if not isinstance(self._registry, BundleRegistry):
                return _RunExecution(outcome)
            if (
                outcome.trace is None
                or outcome.trace.success is not True
                or outcome.trace.completion_proof is None
                or not outcome.trace.events
            ):
                return _RunExecution(_discovery_compile_failure(outcome))
            try:
                bundle = self._compiler.compile(outcome.trace, blueprint)
                reference = self._registry.put_draft(bundle)
            except (
                CompilationError,
                BundleNotFoundError,
                DigestMismatchError,
                ImmutableRevisionError,
                InvalidBundleError,
                QualificationMismatchError,
                ValidationError,
                OSError,
                ValueError,
            ):
                return _RunExecution(_discovery_compile_failure(outcome))
            except Exception:
                return _RunExecution(
                    _discovery_compile_failure(outcome, SafeReasonCode.INTERNAL_ERROR)
                )
            return _RunExecution(outcome, capability_reference=reference)

        return await self._start_run(
            session_id=request.session_id,
            expected_browser_version=state.browser_version,
            mode=RunMode.DISCOVERY,
            request_id=request.request_id,
            payload=payload,
            runner=run,
            reference=None,
            model_id=self._model_id,
            input_bindings=intent.input_bindings,
        )

    async def invoke(self, request: ReplayRequest) -> RunHandle:
        if self._shutdown:
            raise ServiceError(503, "SERVICE_SHUTDOWN")
        request = _validated_request(ReplayRequest, request)
        if (
            request.reference.name != SUPPORTED_CAPABILITY
            or request.reference.version != SUPPORTED_CAPABILITY_VERSION
        ):
            raise ServiceError(422, "UNSUPPORTED_CAPABILITY")
        account_id = _bound_account(request.inputs)
        state = await self._registered_active_state(request.session_id)
        try:
            self._registry.prepare_execution(
                request.reference,
                runtime_fingerprint=current_runtime_fingerprint(),
                browser_version=state.browser_version,
                target_revision=self._config.target_revision,
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
            raise ServiceError(409, "INVALID_BUNDLE") from None
        except Exception:
            # Registry and filesystem exception strings are never returned to callers.
            raise ServiceError(409, "INVALID_BUNDLE") from None
        bindings = {"inputs.account_id": account_id}
        payload = {
            "reference": request.reference.model_dump(mode="json"),
            "inputs": _plain_bindings(request.inputs),
        }

        async def run(run_context: ExecutionContext):
            return await self._replay.run(request.reference, run_context)

        return await self._start_run(
            session_id=request.session_id,
            expected_browser_version=state.browser_version,
            mode=RunMode.REPLAY,
            request_id=request.request_id,
            payload=payload,
            runner=run,
            reference=request.reference,
            model_id=None,
            input_bindings=bindings,
        )

    async def list_capabilities(self) -> tuple[CapabilitySummary, ...]:
        try:
            revisions = self._registry.list_revisions()
        except Exception:
            raise ServiceError(503, "REGISTRY_UNAVAILABLE") from None
        return tuple(
            CapabilitySummary(
                reference=item.reference,
                lifecycle=CapabilityLifecycle(item.lifecycle),
                step_count=len(item.bundle.steps),
                validation_run_ref=(
                    item.qualification.validation_run_ref
                    if item.qualification is not None
                    else None
                ),
                approved=item.approval is not None,
            )
            for item in revisions
        )

    async def inspect_capability(self, reference: BundleReference) -> CapabilityDetail:
        reference = _coerce_reference(reference)
        try:
            item = self._registry.inspect_revision(reference)
        except BundleNotFoundError:
            raise ServiceError(404, "CAPABILITY_NOT_FOUND") from None
        except Exception:
            raise ServiceError(409, "INVALID_BUNDLE") from None
        return _capability_detail(item, self._validation_reports.get(reference.digest))

    async def validation_report(self, run_id: str) -> ValidationReport | None:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ServiceError(404, "RUN_NOT_FOUND")
        async with self._submission_lock:
            record = self._runs.get(run_id)
            if record is None:
                raise ServiceError(404, "RUN_NOT_FOUND")
            if record.mode is not RunMode.VALIDATION:
                raise ServiceError(409, "NOT_A_VALIDATION_RUN")
            return record.validation_report

    async def validate_capability(
        self,
        reference: BundleReference,
        *,
        request_id: str,
    ) -> RunHandle:
        """Replay a DRAFT on a configured validation principal and qualify it by oracle."""
        if self._shutdown:
            raise ServiceError(503, "SERVICE_SHUTDOWN")
        reference = _coerce_reference(reference)
        if not _REQUEST_ID.fullmatch(request_id or ""):
            raise ServiceError(422, "INPUT_INVALID")
        if (
            reference.name != SUPPORTED_CAPABILITY
            or reference.version != SUPPORTED_CAPABILITY_VERSION
        ):
            raise ServiceError(422, "UNSUPPORTED_CAPABILITY")
        if not self._config.validation_fixtures:
            raise ServiceError(503, "VALIDATION_FIXTURE_UNAVAILABLE")
        if self._validation_oracle is None:
            raise ServiceError(503, "ORACLE_UNAVAILABLE")

        # Fixtures are selected only from frozen server configuration. The caller
        # cannot choose a principal, account binding, oracle, or qualification.
        fixture = self._config.validation_fixtures[0]
        payload = {
            "reference": reference.model_dump(mode="json"),
            "fixture_alias": fixture.principal_alias,
        }
        payload_fingerprint = _hmac(self._idempotency_secret, _canonical_json(payload))
        scope_key = f"validation:{fixture.principal_alias}"
        scope = self._idempotency_scope(scope_key, RunMode.VALIDATION, request_id)

        async with self._validation_start_lock:
            async with self._submission_lock:
                existing = self._idempotent_run(scope, payload_fingerprint)
                if existing is not None:
                    return existing

            try:
                revision = self._registry.inspect_revision(reference)
            except BundleNotFoundError:
                raise ServiceError(404, "CAPABILITY_NOT_FOUND") from None
            except Exception:
                raise ServiceError(409, "INVALID_BUNDLE") from None
            if revision.lifecycle != "DRAFT":
                raise ServiceError(409, "CAPABILITY_NOT_DRAFT")
            if not _supported_runtime_contract(revision.bundle):
                raise ServiceError(409, "INVALID_BUNDLE")
            validation_fingerprint = current_runtime_fingerprint()
            if revision.bundle.compatibility.profile_sha256 != validation_fingerprint.profile_sha256:
                raise ServiceError(409, "INVALID_BUNDLE")

            async with self._lifecycle_lock:
                if self._shutdown:
                    raise ServiceError(503, "SERVICE_SHUTDOWN")
                try:
                    handle, validation_binding = await self._sessions.prepare_validation_session(
                        fixture.principal_alias
                    )
                except SessionError as error:
                    await self._close_retained_failed_session(error)
                    raise _session_service_error(error) from None
                except Exception:
                    raise ServiceError(503, "SESSION_LOST") from None

            if (
                not isinstance(validation_binding, ValidationSessionBinding)
                or validation_binding.session_id != handle.session_id
                or validation_binding.principal_alias != fixture.principal_alias
                or validation_binding.authentication_generation != handle.auth_generation
            ):
                await self._discard_validation_session(handle.session_id)
                raise ServiceError(503, "SESSION_LOST")
            try:
                state = await self._sessions.get_state(handle.session_id)
            except SessionError:
                await self._discard_validation_session(handle.session_id)
                raise ServiceError(409, "SESSION_LOST") from None
            if (
                state.state != "ACTIVE"
                or state.principal_alias != fixture.principal_alias
                or state.browser_version != handle.browser_version
                or state.auth_generation != handle.auth_generation
            ):
                await self._discard_validation_session(handle.session_id)
                raise ServiceError(409, "SESSION_LOST")

            async with self._submission_lock:
                self._session_handles[handle.session_id] = handle
                self._session_views[handle.session_id] = _session_view(handle)
                self._validation_sessions.add(handle.session_id)

            async def validate_run(context: ExecutionContext):
                replay_result: InvocationResult
                try:
                    replay_value = await asyncio.wait_for(
                        self._replay.run_validation(reference, context, validation_binding),
                        timeout=self._config.run_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    replay_value = None
                if (
                    not isinstance(replay_value, InvocationResult)
                    or replay_value.run_id != context.run_alias
                    or replay_value.status is not InvocationStatus.SUCCESS
                ):
                    report = ValidationReport(
                        reference=reference,
                        run_id=context.run_alias,
                        passed=False,
                        code="REPLAY_FAILED",
                    )
                    failed = _validation_failure(
                        context.run_alias, SafeReasonCode.REPLAY_FAILED
                    )
                    return _RunExecution(failed, reference, report)

                try:
                    oracle_report = await asyncio.wait_for(
                        self._validation_oracle.check(replay_value, fixture),
                        timeout=self._config.run_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    oracle_report = OracleReport(passed=False, code="ORACLE_UNAVAILABLE")
                if not isinstance(oracle_report, OracleReport):
                    oracle_report = OracleReport(passed=False, code="ORACLE_UNAVAILABLE")
                if not oracle_report.passed:
                    report_code = (
                        "ORACLE_UNAVAILABLE"
                        if oracle_report.code == "ORACLE_UNAVAILABLE"
                        else "ORACLE_MISMATCH"
                    )
                    reason = (
                        SafeReasonCode.ORACLE_UNAVAILABLE
                        if report_code == "ORACLE_UNAVAILABLE"
                        else SafeReasonCode.ORACLE_MISMATCH
                    )
                    report = ValidationReport(
                        reference=reference,
                        run_id=context.run_alias,
                        passed=False,
                        code=report_code,
                    )
                    return _RunExecution(_validation_failure(context.run_alias, reason), reference, report)

                try:
                    qualification = ValidationQualification(
                        bundle_digest=reference.digest,
                        validation_run_ref=context.run_alias,
                        runtime_fingerprint=validation_fingerprint,
                        browser_version=state.browser_version,
                        target_revision=self._config.target_revision,
                        replay_passed=True,
                        independent_oracle_passed=True,
                    )
                    if current_runtime_fingerprint() != validation_fingerprint:
                        report = ValidationReport(
                            reference=reference,
                            run_id=context.run_alias,
                            passed=False,
                            code="INVALID_BUNDLE",
                        )
                        return _RunExecution(
                            _validation_failure(context.run_alias, SafeReasonCode.INVALID_BUNDLE),
                            reference,
                            report,
                        )
                    self._registry.validate(reference, qualification)
                except Exception:
                    report = ValidationReport(
                        reference=reference,
                        run_id=context.run_alias,
                        passed=False,
                        code="INVALID_BUNDLE",
                    )
                    return _RunExecution(
                        _validation_failure(context.run_alias, SafeReasonCode.INVALID_BUNDLE),
                        reference,
                        report,
                    )

                report = ValidationReport(
                    reference=reference,
                    run_id=context.run_alias,
                    passed=True,
                    code="VALIDATION_PASS",
                )
                return _RunExecution(replay_value, reference, report)

            try:
                return await self._start_run(
                    session_id=handle.session_id,
                    expected_browser_version=state.browser_version,
                    mode=RunMode.VALIDATION,
                    request_id=request_id,
                    payload=payload,
                    runner=validate_run,
                    reference=reference,
                    model_id=None,
                    input_bindings={"inputs.account_id": fixture.account_id},
                    scope_key=scope_key,
                )
            except BaseException:
                await self._discard_validation_session(handle.session_id)
                raise

    async def approve_capability(
        self,
        reference: BundleReference,
        *,
        expected_digest: str,
        reviewer_ref: str,
        reviewer_type: str,
    ) -> ApprovalRecord:
        """Approve only the exact digest currently stored as VALIDATED."""
        reference = _coerce_reference(reference)
        if (
            reference.name != SUPPORTED_CAPABILITY
            or reference.version != SUPPORTED_CAPABILITY_VERSION
        ):
            raise ServiceError(422, "UNSUPPORTED_CAPABILITY")
        if (
            not isinstance(expected_digest, str)
            or not hmac.compare_digest(expected_digest, reference.digest)
        ):
            raise ServiceError(409, "DIGEST_MISMATCH")
        if not isinstance(reviewer_ref, str) or not re.fullmatch(
            r"[a-z][a-z0-9_.]{0,95}", reviewer_ref, re.ASCII
        ) or reviewer_type not in {"independent_reviewer", "operator"}:
            raise ServiceError(422, "INPUT_INVALID")
        try:
            return self._registry.approve(
                reference,
                reviewer_ref=reviewer_ref,
                reviewer_type=reviewer_type,
            )
        except (BundleNotFoundError, DigestMismatchError, ImmutableRevisionError,
                InvalidBundleError, QualificationMismatchError, ValidationError,
                OSError, ValueError):
            raise ServiceError(409, "CAPABILITY_NOT_VALIDATED") from None
        except Exception:
            raise ServiceError(409, "CAPABILITY_NOT_VALIDATED") from None

    async def get_run(self, run_id: str) -> RunView:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ServiceError(404, "RUN_NOT_FOUND")
        async with self._submission_lock:
            record = self._runs.get(run_id)
            if record is None:
                raise ServiceError(404, "RUN_NOT_FOUND")
            self._expire_result(record)
            return _run_view(record)

    async def read_result(
        self,
        run_id: str,
        caller_session_id: str,
    ) -> InvocationResult:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ServiceError(404, "RUN_NOT_FOUND")
        async with self._submission_lock:
            record = self._runs.get(run_id)
            if record is None:
                try:
                    self._evidence.get_run(run_id)
                except EvidenceError:
                    raise ServiceError(404, "RUN_NOT_FOUND") from None
                raise ServiceError(410, "RESULT_EXPIRED") from None
            if record.mode is RunMode.VALIDATION:
                raise ServiceError(403, "RESULT_FORBIDDEN")
            if caller_session_id != record.session_id:
                raise ServiceError(403, "SESSION_FORBIDDEN")
            self._expire_result(record)
            if record.result is not None:
                return _public_result(record.run_id, record.result)
            if record.result_expired:
                raise ServiceError(410, "RESULT_EXPIRED")
            raise ServiceError(409, "RESULT_PENDING")

    async def close_session(self, session_id: str) -> None:
        async with self._submission_lock:
            if session_id not in self._session_handles:
                raise ServiceError(404, "SESSION_NOT_FOUND")
            if session_id in self._closing_sessions:
                raise ServiceError(409, "SESSION_BUSY")
            self._closing_sessions.add(session_id)
            active_id = self._active_by_session.get(session_id)
            record = self._runs.get(active_id) if active_id is not None else None

        try:
            state = await self._sessions.get_state(session_id)
            actor = state.actor
            if record is not None or actor.active_run_id is not None:
                await actor.pause_and_drain(expected_epoch=actor.epoch)
                task = record.task if record is not None else None
                if task is not None and not task.done():
                    if task.cancelling() == 0:
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                active_run_id = actor.active_run_id
                if active_run_id is not None:
                    # Recover a lease left by an already-terminal task. The drain
                    # above proves no accepted browser command remains in flight.
                    await actor.finish_run(active_run_id)
            async with self._lifecycle_lock:
                await self._sessions.close(session_id)
        except ServiceError:
            async with self._submission_lock:
                self._closing_sessions.discard(session_id)
            raise
        except Exception:
            async with self._submission_lock:
                self._closing_sessions.discard(session_id)
            raise ServiceError(409, "SESSION_CLOSE_FAILED") from None

        async with self._submission_lock:
            self._session_handles.pop(session_id, None)
            self._session_views.pop(session_id, None)
            self._validation_sessions.discard(session_id)
            self._closing_sessions.discard(session_id)
            for item in self._runs.values():
                if item.session_id == session_id:
                    item.result = None
                    item.result_expires_at = None
                    item.result_expired = True
                    if item.expiry_task is not None:
                        item.expiry_task.cancel()

    async def shutdown(self) -> None:
        """Drain service-owned work, close contexts, and clear volatile values."""
        async with self._submission_lock:
            if self._shutdown_complete:
                return
            self._shutdown = True
            session_ids = tuple(self._session_handles)

        for session_id in session_ids:
            try:
                await self.close_session(session_id)
            except ServiceError:
                # Continue draining other isolated contexts; failures stay local.
                continue

        # A close may have raced a manager-owned lifecycle transition. Retry after
        # all actor tasks have drained, but never force-close an accepted actor run.
        async with self._submission_lock:
            remaining_session_ids = tuple(self._session_handles)
        for session_id in remaining_session_ids:
            try:
                await self.close_session(session_id)
            except ServiceError:
                continue

        pending = tuple(task for task in self._tasks if not task.done())
        for task in pending:
            if task.cancelling() == 0:
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # close_all also stops the shared Playwright browser. By this point all
        # registered actor leases have been drained by close_session.
        close_all = getattr(self._sessions, "close_all", None)
        async with self._submission_lock:
            remaining = tuple(self._session_handles)
        if remaining:
            raise ServiceError(503, "SERVICE_SHUTDOWN_INCOMPLETE")
        close_error = False
        if close_all is not None:
            try:
                await close_all()
            except Exception:
                close_error = True

        async with self._submission_lock:
            for record in self._runs.values():
                if record.expiry_task is not None:
                    record.expiry_task.cancel()
                record.result = None
                record.result_expires_at = None
                record.result_expired = True
                record.validation_report = None
            self._runs.clear()
            self._active_by_session.clear()
            self._idempotency.clear()
            self._validation_reports.clear()
            self._session_handles.clear()
            self._session_views.clear()
            self._validation_sessions.clear()
            self._closing_sessions.clear()
            self._idempotency_secret = secrets.token_bytes(32)
        if close_error:
            raise ServiceError(503, "SERVICE_SHUTDOWN_INCOMPLETE")
        self._shutdown_complete = True

    @property
    def _ordinary_principal_aliases(self) -> frozenset[str]:
        return frozenset(self._config.ordinary_principal_aliases)

    async def _close_retained_failed_session(self, error: SessionError) -> None:
        if error.session_id is None or error.code not in {"SESSION_EXPIRED", "SUBJECT_MISMATCH"}:
            return
        try:
            await self._sessions.close(error.session_id)
        except Exception:
            # The primary safe session error remains the public result.
            return

    async def _discard_validation_session(self, session_id: str) -> None:
        # This path is used before the private session is published to the app
        # registry. Close through the manager directly so a rejected binding or
        # state check cannot leave an unowned browser context and target lock.
        async with self._submission_lock:
            registered = session_id in self._session_handles
        if not registered:
            try:
                async with self._lifecycle_lock:
                    await self._sessions.close(session_id)
            except Exception:
                return
            return
        try:
            await self.close_session(session_id)
        except ServiceError:
            # Keep a retained private session registered so shutdown can retry a
            # drain instead of losing the only path to close its context.
            return

    async def _close_validation_session(self, session_id: str, actor: object) -> bool:
        try:
            try:
                state = await self._sessions.get_state(session_id)
            except SessionError:
                state = None
            if state is not None:
                if state.actor is not actor:
                    return False
                if state.actor.active_run_id is not None:
                    await state.actor.pause_and_drain(expected_epoch=state.actor.epoch)
                    active_run_id = state.actor.active_run_id
                    if active_run_id is not None:
                        await state.actor.finish_run(active_run_id)
                async with self._lifecycle_lock:
                    await self._sessions.close(session_id)
        except Exception:
            return False
        async with self._submission_lock:
            self._session_handles.pop(session_id, None)
            self._session_views.pop(session_id, None)
            self._validation_sessions.discard(session_id)
            self._closing_sessions.discard(session_id)
        return True

    async def _on_intervention_state(self, view: InterventionView) -> None:
        """Atomically project a safe handoff view onto its run record."""
        view = _safe_intervention_view(view)
        mapped_state = {
            HandoffState.WAITING_FOR_HUMAN: RunState.WAITING_FOR_HUMAN,
            HandoffState.HUMAN_CLAIMED: RunState.WAITING_FOR_HUMAN,
            HandoffState.RESUMING: RunState.WAITING_FOR_HUMAN,
            HandoffState.RUNNING: RunState.RUNNING,
            HandoffState.ABORTED: RunState.ABORTED,
            HandoffState.TIMED_OUT: RunState.ABORTED,
        }[view.state]
        async with self._submission_lock:
            record = self._runs.get(view.run_id)
            if record is None or record.session_id != view.session_id:
                return
            record.intervention_id = view.intervention_id
            if record.state not in {
                RunState.SUCCESS,
                RunState.BUSINESS_OUTCOME,
                RunState.FAILURE,
                RunState.ABORTED,
                RunState.SESSION_LOST,
            } or mapped_state is RunState.ABORTED:
                record.state = mapped_state
            record.updated_at_ms = int(time.time() * 1000)

    def _require_handoff_coordinator(self):
        coordinator = self._handoff_coordinator
        if coordinator is None:
            raise ServiceError(503, "HANDOFF_UNAVAILABLE")
        return coordinator

    @staticmethod
    def _require_local_operator(operator_ref: str) -> None:
        if operator_ref != _LOCAL_OPERATOR:
            raise ServiceError(403, "INTERVENTION_OPERATOR_FORBIDDEN")

    @staticmethod
    def _validate_intervention_epoch(expected_epoch: int) -> None:
        if isinstance(expected_epoch, bool) or not isinstance(expected_epoch, int) or expected_epoch < 0:
            raise ServiceError(422, "INPUT_INVALID")

    def _idempotency_scope(self, scope_key: str, mode: RunMode, request_id: str) -> str:
        return _hmac(
            self._idempotency_secret,
            _canonical_json([scope_key, mode.value, request_id]),
        )

    def _idempotent_run(self, scope: str, payload_fingerprint: str) -> RunHandle | None:
        previous = self._idempotency.get(scope)
        if previous is None:
            return None
        previous_fingerprint, previous_run_id = previous
        if not hmac.compare_digest(previous_fingerprint, payload_fingerprint):
            raise ServiceError(409, "IDEMPOTENCY_CONFLICT")
        record = self._runs.get(previous_run_id)
        if record is None:
            raise ServiceError(410, "RESULT_EXPIRED")
        return _run_handle(record)

    @property
    def _model_id(self) -> str | None:
        if self._decision_backend is None or isinstance(
            self._decision_backend, DisabledDecisionBackend
        ):
            return None
        return self._decision_backend.model_id

    def _require_discovery_provider(self) -> None:
        if self._config.provider_mode is ProviderMode.DISABLED:
            raise ServiceError(503, "MODEL_NOT_CONFIGURED")
        if self._decision_backend is None or isinstance(
            self._decision_backend, DisabledDecisionBackend
        ):
            raise ServiceError(503, "MODEL_NOT_CONFIGURED")
        if isinstance(self._decision_backend, OpenAIResponsesDecisionBackend):
            key = os.environ.get("OPENAI_API_KEY", "")
            if not key.strip():
                raise ServiceError(503, "PROVIDER_CREDENTIAL_MISSING")

    async def _registered_active_state(self, session_id: str) -> SessionState:
        async with self._submission_lock:
            if self._shutdown:
                raise ServiceError(503, "SERVICE_SHUTDOWN")
            if session_id not in self._session_handles:
                raise ServiceError(404, "SESSION_NOT_FOUND")
            if session_id in self._validation_sessions:
                raise ServiceError(403, "SESSION_FORBIDDEN")
            if session_id in self._closing_sessions:
                raise ServiceError(409, "SESSION_BUSY")
        try:
            state = await self._sessions.get_state(session_id)
        except SessionError:
            raise ServiceError(409, "SESSION_LOST") from None
        if state.state != "ACTIVE":
            code = state.state if state.state in {"SESSION_EXPIRED", "SUBJECT_MISMATCH"} else "SESSION_LOST"
            raise ServiceError(409, code)
        return state

    async def _start_run(
        self,
        *,
        session_id: str,
        expected_browser_version: str,
        mode: RunMode,
        request_id: str,
        payload: Mapping[str, object],
        runner: RunCallable,
        reference: BundleReference | None,
        model_id: str | None,
        input_bindings: Mapping[str, SecretStr] | None = None,
        scope_key: str | None = None,
    ) -> RunHandle:
        copied_payload = json.loads(_canonical_json(payload))
        payload_fingerprint = _hmac(self._idempotency_secret, _canonical_json(copied_payload))
        scope = self._idempotency_scope(scope_key or session_id, mode, request_id)
        async with self._submission_lock:
            if self._shutdown:
                raise ServiceError(503, "SERVICE_SHUTDOWN")
            if session_id in self._closing_sessions or session_id not in self._session_handles:
                raise ServiceError(409, "SESSION_BUSY")
            previous = self._idempotency.get(scope)
            if previous is not None:
                previous_fingerprint, previous_run_id = previous
                if not hmac.compare_digest(previous_fingerprint, payload_fingerprint):
                    raise ServiceError(409, "IDEMPOTENCY_CONFLICT")
                record = self._runs.get(previous_run_id)
                if record is None:
                    raise ServiceError(410, "RESULT_EXPIRED")
                return _run_handle(record)
            if session_id in self._active_by_session:
                raise ServiceError(409, "SESSION_BUSY")

            try:
                state = await self._sessions.get_state(session_id)
            except SessionError:
                raise ServiceError(409, "SESSION_LOST") from None
            if state.state != "ACTIVE":
                raise ServiceError(409, "SESSION_LOST")
            if state.browser_version != expected_browser_version:
                raise ServiceError(409, "SESSION_CHANGED")
            if state.actor.active_run_id is not None:
                raise ServiceError(409, "SESSION_BUSY")

            evidence_mode = EvidenceRunMode(mode.value)
            metadata = RunMetadata(
                mode=evidence_mode,
                capability_name=reference.name if reference is not None else None,
                capability_version=reference.version if reference is not None else None,
                bundle_digest=reference.digest if reference is not None else None,
                profile_id=PROFILE_ID,
                target_revision=self._config.target_revision,
                browser_version=state.browser_version,
                model_id=model_id,
            )
            try:
                run_id = self._evidence.register_run(metadata)
            except Exception:
                raise ServiceError(503, "EVIDENCE_UNAVAILABLE") from None

            try:
                await state.actor.begin_run(run_id)
                self._evidence.transition_run(run_id, RunState.RUNNING)
            except (ActorBusy, ActorPaused, ActorStaleEpoch):
                self._mark_unstarted_aborted(run_id, SafeReasonCode.SESSION_LOST)
                raise ServiceError(409, "SESSION_BUSY") from None
            except Exception:
                if state.actor.active_run_id == run_id:
                    try:
                        await state.actor.finish_run(run_id)
                    except Exception:
                        pass
                self._mark_unstarted_aborted(run_id, SafeReasonCode.EVIDENCE_ERROR)
                raise ServiceError(503, "EVIDENCE_UNAVAILABLE") from None

            now_ms = int(time.time() * 1000)
            record = _RunRecord(
                run_id=run_id,
                session_id=session_id,
                mode=mode,
                state=RunState.RUNNING,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
                actor=state.actor,
                capability_reference=reference,
            )
            context = self._execution_context(
                state,
                run_id,
                input_bindings or {},
            )
            task = asyncio.create_task(self._execute_run(record, context, runner))
            record.task = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            self._runs[run_id] = record
            self._active_by_session[session_id] = run_id
            self._idempotency[scope] = (payload_fingerprint, run_id)

        await record.started.wait()
        async with self._submission_lock:
            return _run_handle(record)

    async def _execute_run(
        self,
        record: _RunRecord,
        context: ExecutionContext,
        runner: RunCallable,
    ) -> None:
        record.started.set()
        try:
            run_value = await runner(context)
            if isinstance(run_value, _RunExecution):
                record.capability_reference = run_value.capability_reference or record.capability_reference
                record.validation_report = run_value.validation_report
                result = run_value.result
            else:
                result = run_value
            state, code, decisions, action_count = _terminal_summary(record.mode, result)
        except asyncio.CancelledError:
            result = _aborted_result(record.run_id, record.mode)
            state, code, decisions, action_count = (
                RunState.ABORTED,
                SafeReasonCode.SESSION_LOST,
                0,
                0,
            )
        except Exception:
            result = _internal_failure(record.run_id, record.mode)
            state, code, decisions, action_count = (
                RunState.FAILURE,
                SafeReasonCode.INTERNAL_ERROR,
                0,
                0,
            )

        try:
            await record.actor.finish_run(record.run_id)
        except Exception:
            result = _internal_failure(record.run_id, record.mode)
            state, code = RunState.FAILURE, SafeReasonCode.SESSION_LOST
            decisions, action_count = 0, 0
        if record.mode is RunMode.VALIDATION:
            closed = await self._close_validation_session(record.session_id, record.actor)
            if not closed:
                result = _internal_failure(record.run_id, RunMode.VALIDATION)
                state, code = RunState.FAILURE, SafeReasonCode.SESSION_LOST
                decisions, action_count = 0, 0
        try:
            self._evidence.transition_run(record.run_id, state, outcome_code=code)
            self._evidence.finish_manifest(record.run_id)
        except Exception:
            result = _internal_failure(record.run_id, record.mode)
            state, code = RunState.FAILURE, SafeReasonCode.EVIDENCE_ERROR
            decisions, action_count = 0, 0

        async with self._submission_lock:
            if record.mode is RunMode.VALIDATION:
                # Validation outputs are consumed only by the trusted oracle and
                # then discarded; callers can read the safe report, never results.
                record.result = None
                record.result_expired = True
                if record.validation_report is not None:
                    self._validation_reports[record.validation_report.reference.digest] = (
                        record.validation_report
                    )
            else:
                record.result = result
                record.result_expires_at = self._clock() + self._config.result_ttl_seconds
                record.result_expired = False
                record.expiry_task = asyncio.create_task(
                    self._expire_result_after_ttl(record.run_id)
                )
                self._tasks.add(record.expiry_task)
                record.expiry_task.add_done_callback(self._tasks.discard)
            record.state = state
            record.outcome_code = code
            record.decisions_used = decisions
            record.verified_action_count = action_count
            record.updated_at_ms = int(time.time() * 1000)
            if self._active_by_session.get(record.session_id) == record.run_id:
                self._active_by_session.pop(record.session_id, None)

    async def _expire_result_after_ttl(self, run_id: str) -> None:
        await asyncio.sleep(self._config.result_ttl_seconds)
        async with self._submission_lock:
            record = self._runs.get(run_id)
            if record is None or record.result is None:
                return
            record.result = None
            record.result_expires_at = None
            record.result_expired = True

    def _execution_context(
        self,
        state: SessionState,
        run_id: str,
        input_bindings: Mapping[str, SecretStr],
    ) -> ExecutionContext:
        authority = state.origin
        routes = ("home", "accounts_overview", "account_details")
        operations = ("CLICK", "READ")
        policy_context = PolicyContext(
            deployment_origins=(authority,),
            capability_origins=(authority,),
            run_origins=(authority,),
            deployment_routes=routes,
            capability_routes=routes,
            run_routes=routes,
            deployment_operations=operations,
            capability_operations=operations,
            run_operations=operations,
        )
        return ExecutionContext(
            run_alias=run_id,
            session_id=state.session_id,
            expected_epoch=state.actor.epoch,
            authentication_generation=state.auth_generation,
            target_origin=authority,
            profile_id=state.profile_id,
            input_bindings=dict(input_bindings),
            policy_context=policy_context,
            deadline_monotonic=time.monotonic() + self._config.run_timeout_seconds,
        )

    def _expire_result(self, record: _RunRecord) -> None:
        if (
            record.result is not None
            and record.result_expires_at is not None
            and self._clock() >= record.result_expires_at
        ):
            record.result = None
            record.result_expires_at = None
            record.result_expired = True

    def _mark_unstarted_aborted(self, run_id: str, reason: SafeReasonCode) -> None:
        try:
            self._evidence.transition_run(run_id, RunState.ABORTED, outcome_code=reason)
            self._evidence.finish_manifest(run_id)
        except Exception:
            pass


def _validated_request(model, request):
    try:
        return model.model_validate(request.model_dump(mode="python"))
    except (AttributeError, ValidationError):
        raise ServiceError(422, "INPUT_INVALID") from None


def _bound_account(inputs: Mapping[str, SecretStr]) -> SecretStr:
    if not isinstance(inputs, Mapping) or any(
        key not in {"account_id", "inputs.account_id"} for key in inputs
    ):
        raise ServiceError(422, "INPUT_INVALID")
    values = []
    for value in inputs.values():
        if not isinstance(value, SecretStr):
            raise ServiceError(422, "INPUT_INVALID")
        plain = value.get_secret_value()
        if not _ACCOUNT_ID.fullmatch(plain):
            raise ServiceError(422, "INPUT_INVALID")
        values.append(plain)
    if not values:
        raise ServiceError(422, "INPUT_INVALID")
    if len(set(values)) != 1:
        raise ServiceError(422, "INPUT_CONFLICT")
    return SecretStr(values[0])


def _plain_bindings(inputs: Mapping[str, SecretStr]) -> dict[str, str]:
    return {key: value.get_secret_value() for key, value in sorted(inputs.items())}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hmac(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _terminal_summary(
    mode: RunMode,
    result: InvocationResult | DiscoveryOutcome,
) -> tuple[RunState, SafeReasonCode, int, int]:
    if mode in {RunMode.REPLAY, RunMode.VALIDATION} and isinstance(result, InvocationResult):
        if result.status is InvocationStatus.SUCCESS:
            if mode is RunMode.VALIDATION:
                return RunState.SUCCESS, SafeReasonCode.VALIDATION_COMPLETE, 0, 0
            return RunState.SUCCESS, result.code or SafeReasonCode.REPLAY_COMPLETE, 0, 0
        if result.status is InvocationStatus.BUSINESS_OUTCOME:
            return RunState.BUSINESS_OUTCOME, result.code or SafeReasonCode.REPLAY_FAILED, 0, 0
        if result.status is InvocationStatus.ABORTED:
            code = result.failure.reason_code if result.failure else SafeReasonCode.SESSION_LOST
            return RunState.ABORTED, code, 0, 0
        code = result.failure.reason_code if result.failure else SafeReasonCode.INTERNAL_ERROR
        state = RunState.SESSION_LOST if code is SafeReasonCode.SESSION_LOST else RunState.FAILURE
        return state, code, 0, 0

    if mode is RunMode.DISCOVERY and isinstance(result, DiscoveryOutcome):
        decisions = result.decisions_used
        actions = result.verified_action_count
        if result.status is DiscoveryStatus.SUCCESS:
            return RunState.SUCCESS, SafeReasonCode.DISCOVERY_COMPLETE, decisions, actions
        try:
            code = SafeReasonCode(result.reason_code)
        except ValueError:
            code = SafeReasonCode.INTERNAL_ERROR
        if result.status is DiscoveryStatus.BUSINESS_OUTCOME:
            return RunState.BUSINESS_OUTCOME, code, decisions, actions
        return RunState.FAILURE, code, decisions, actions
    return RunState.FAILURE, SafeReasonCode.INTERNAL_ERROR, 0, 0


def _public_result(
    run_id: str,
    result: InvocationResult | DiscoveryOutcome,
) -> InvocationResult:
    """Return one shared result contract while keeping discovery traces private."""
    if isinstance(result, InvocationResult):
        return result

    try:
        reason_code = SafeReasonCode(result.reason_code)
    except (TypeError, ValueError):
        reason_code = SafeReasonCode.INTERNAL_ERROR

    if result.status is DiscoveryStatus.SUCCESS:
        return InvocationResult(
            run_id=run_id,
            status=InvocationStatus.SUCCESS,
            outputs=dict(result.outputs),
        )
    if result.status is DiscoveryStatus.BUSINESS_OUTCOME:
        return InvocationResult(
            run_id=run_id,
            status=InvocationStatus.BUSINESS_OUTCOME,
            code=reason_code,
        )
    return InvocationResult(
        run_id=run_id,
        status=InvocationStatus.FAILURE,
        failure=FailureDetail(reason_code=reason_code),
    )


def _internal_failure(run_id: str, mode: RunMode) -> InvocationResult | DiscoveryOutcome:
    if mode is RunMode.DISCOVERY:
        return DiscoveryOutcome(
            status=DiscoveryStatus.FAILURE,
            reason_code=SafeReasonCode.INTERNAL_ERROR.value,
            decisions_used=0,
            verified_action_count=0,
        )
    return InvocationResult(
        run_id=run_id,
        status=InvocationStatus.FAILURE,
        failure=FailureDetail(
            reason_code=SafeReasonCode.INTERNAL_ERROR,
            effect_state=EffectState.NOT_DISPATCHED,
        ),
    )


def _aborted_result(run_id: str, mode: RunMode) -> InvocationResult | DiscoveryOutcome:
    if mode is RunMode.DISCOVERY:
        return DiscoveryOutcome(
            status=DiscoveryStatus.FAILURE,
            reason_code=SafeReasonCode.SESSION_LOST.value,
            decisions_used=0,
            verified_action_count=0,
        )
    return InvocationResult(
        run_id=run_id,
        status=InvocationStatus.ABORTED,
        failure=FailureDetail(
            reason_code=SafeReasonCode.SESSION_LOST,
            effect_state=EffectState.NOT_DISPATCHED,
        ),
    )


def _session_view(handle: SessionHandle) -> SessionView:
    return SessionView(
        session_id=handle.session_id,
        principal_alias=handle.principal_alias,
        profile_id=handle.profile_id,
        origin=handle.origin,
        authentication_generation=handle.auth_generation,
    )


def _session_view_from_state(state: SessionState) -> SessionView:
    return SessionView(
        session_id=state.session_id,
        principal_alias=state.principal_alias,
        profile_id=state.profile_id,
        origin=state.origin,
        authentication_generation=state.auth_generation,
    )


def _run_handle(record: _RunRecord) -> RunHandle:
    return RunHandle(run_id=record.run_id, mode=record.mode, state=record.state)


def _run_view(record: _RunRecord) -> RunView:
    return RunView(
        run_id=record.run_id,
        mode=record.mode,
        state=record.state,
        outcome_code=record.outcome_code,
        intervention_id=record.intervention_id,
        capability_reference=record.capability_reference,
        created_at_ms=record.created_at_ms,
        updated_at_ms=record.updated_at_ms,
        decisions_used=record.decisions_used,
        verified_action_count=record.verified_action_count,
    )


def _session_service_error(error: SessionError) -> ServiceError:
    if error.code == "INPUT_INVALID":
        return ServiceError(422, "INPUT_INVALID")
    if error.code == "SUBJECT_MISMATCH":
        return ServiceError(409, "SUBJECT_MISMATCH")
    if error.code == "SESSION_BUSY":
        return ServiceError(409, "SESSION_BUSY")
    return ServiceError(503, "SESSION_LOST")


def _safe_intervention_view(value: InterventionView) -> InterventionView:
    if not isinstance(value, InterventionView):
        raise ServiceError(503, "HANDOFF_UNAVAILABLE")
    try:
        return InterventionView(
            intervention_id=value.intervention_id,
            session_id=value.session_id,
            run_id=value.run_id,
            step_id=value.step_id,
            reason=value.reason,
            state=value.state,
            owner=value.owner,
            epoch=value.epoch,
            page_id=value.page_id,
        )
    except (TypeError, ValueError):
        raise ServiceError(503, "HANDOFF_UNAVAILABLE") from None


def _safe_intervention_views(values: object) -> tuple[InterventionView, ...]:
    if not isinstance(values, (tuple, list)):
        raise ServiceError(503, "HANDOFF_UNAVAILABLE")
    return tuple(_safe_intervention_view(value) for value in values)


def _handoff_service_error(error: HandoffError) -> ServiceError:
    code = error.code
    if code == "INTERVENTION_NOT_FOUND":
        return ServiceError(404, code)
    if code in {"INTERVENTION_INPUT_INVALID", "INTERVENTION_TIMEOUT_INVALID"}:
        return ServiceError(422, code)
    if code in {
        "INTERVENTION_OPERATOR_FORBIDDEN",
        "INTERVENTION_TOKEN_FORBIDDEN",
    }:
        return ServiceError(403, code)
    if code == "INTERVENTION_STALE_EPOCH":
        return ServiceError(409, code)
    return ServiceError(409, code)


def _coerce_reference(value: BundleReference) -> BundleReference:
    try:
        payload = value.model_dump(mode="python") if isinstance(value, BundleReference) else value
        return BundleReference.model_validate(payload)
    except (AttributeError, TypeError, ValidationError):
        raise ServiceError(422, "INPUT_INVALID") from None


def _capability_detail(
    revision: RegistryRevision,
    latest_validation: ValidationReport | None,
) -> CapabilityDetail:
    return CapabilityDetail(
        reference=revision.reference,
        lifecycle=CapabilityLifecycle(revision.lifecycle),
        bundle=revision.bundle,
        qualification=revision.qualification,
        approval=revision.approval,
        latest_validation=latest_validation,
    )


def _validation_failure(run_id: str, reason: SafeReasonCode) -> InvocationResult:
    return InvocationResult(
        run_id=run_id,
        status=InvocationStatus.FAILURE,
        failure=FailureDetail(reason_code=reason, effect_state=EffectState.VERIFIED),
    )


def _discovery_compile_failure(
    outcome: DiscoveryOutcome,
    reason: SafeReasonCode = SafeReasonCode.INVALID_BUNDLE,
) -> DiscoveryOutcome:
    return DiscoveryOutcome(
        status=DiscoveryStatus.FAILURE,
        reason_code=reason.value,
        decisions_used=outcome.decisions_used,
        verified_action_count=outcome.verified_action_count,
        model_id=outcome.model_id,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
    )
