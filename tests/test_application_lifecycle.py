import asyncio
import time

from pydantic import SecretStr
import pytest

from cua.application import (
    ApplicationConfig,
    ApplicationService,
    DiscoveryRequest,
    OracleReport,
    RunMode,
    ServiceError,
    ValidationFixture,
)
from cua.compiler import CapabilityCompiler
from cua.discovery.blueprint import parabank_savings_balance_blueprint
from cua.discovery.runtime import DiscoveryOutcome, DiscoveryStatus
from cua.evidence import EvidenceSink
from cua.evidence.models import RunState, SafeReasonCode
from cua.execution import FailureDetail, InvocationResult, InvocationStatus
from cua.models.bundles import BundleStep, StepSource
from cua.models.traces import CompletionProof, VerifiedDiscoveryTrace, VerifiedTraceEvent
from cua.models.qualification import ValidationQualification
from cua.registry import BundleRegistry
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from cua.sessions import (
    PrincipalSpec,
    SessionActor,
    SessionError,
    SessionHandle,
    SessionState,
    ValidationSessionBinding,
)


MAIN_SESSION = "s_main_0123456789abcdef"
ACCOUNT = "9135802468"
VALIDATION_ACCOUNT = "7048162359"
TARGET_REVISION = "ee82474be5f58bea3ddc8be0fd831072b00201cb"


def _principal(alias="synthetic_alpha"):
    suffix = "ALPHA" if alias == "synthetic_alpha" else "BETA"
    return PrincipalSpec(
        alias=alias,
        username_env=f"PARABANK_DEMO_{suffix}_USERNAME",
        password_env=f"PARABANK_DEMO_{suffix}_PASSWORD",
        expected_display_name=f"Synthetic {suffix.title()}",
    )


def _trace():
    trace_id = "trace_service_1"
    event_id = "event_service_1"
    return VerifiedDiscoveryTrace(
        trace_id=trace_id,
        success=True,
        completion_proof=CompletionProof(
            proof_id="proof_service_1",
            trace_id=trace_id,
            session_ref="session_service_1",
            account_binding_ref="inputs.account_id",
            observation_id="obs_service_1",
            membership_proof_id="membership_service_1",
            authentication_generation=1,
        ),
        events=(
            VerifiedTraceEvent(
                event_id=event_id,
                step=BundleStep(
                    id="open_requested_account",
                    kind="CLICK",
                    target_ref="requested_account_link",
                    source=StepSource(type="observed", event_ids=(event_id,)),
                ),
                effect_state="VERIFIED",
            ),
        ),
    )


class _Sessions:
    def __init__(self):
        self.handles = {}
        self.states = {}
        self.prepared_validation = []
        self.closed = []
        self._validation_bindings = {}
        self._next = 0

    async def prepare(self, principal_alias):
        self._next += 1
        session_id = MAIN_SESSION
        handle, state = self._new_session(session_id, principal_alias)
        self.handles[session_id] = handle
        self.states[session_id] = state
        return handle

    async def prepare_validation_session(self, principal_alias):
        self._next += 1
        session_id = f"s_validation_{self._next:08d}"
        handle, state = self._new_session(session_id, principal_alias)
        binding = ValidationSessionBinding(
            session_id=session_id,
            principal_alias=principal_alias,
            authentication_generation=handle.auth_generation,
            _token=f"trusted_validation_token_{self._next}",
        )
        self.handles[session_id] = handle
        self.states[session_id] = state
        self._validation_bindings[session_id] = binding
        self.prepared_validation.append(session_id)
        return handle, binding

    def _new_session(self, session_id, principal_alias):
        actor = SessionActor()
        handle = SessionHandle(
            session_id=session_id,
            page_id=f"p_{self._next:012d}",
            principal_alias=principal_alias,
            profile_id="parabank-native-v1",
            origin="http://127.0.0.1:8080",
            browser_version="1.60.0",
            auth_generation=1,
        )
        state = SessionState(
            session_id=session_id,
            principal_alias=principal_alias,
            state="ACTIVE",
            auth_generation=1,
            profile_id="parabank-native-v1",
            origin="http://127.0.0.1:8080",
            browser_version="1.60.0",
            actor=actor,
        )
        return handle, state

    async def get_state(self, session_id):
        try:
            return self.states[session_id]
        except KeyError:
            raise SessionError("SESSION_LOST") from None

    def consume_validation_session_binding(self, binding, session_id):
        expected = self._validation_bindings.pop(session_id, None)
        return expected is not None and expected == binding

    async def close(self, session_id, *, allow_active=False):
        state = self.states.get(session_id)
        if state and state.actor.active_run_id is not None and not allow_active:
            raise SessionError("SESSION_BUSY", session_id)
        self.closed.append(session_id)
        self.handles.pop(session_id, None)
        self.states.pop(session_id, None)
        self._validation_bindings.pop(session_id, None)


class _Discovery:
    model_id = "offline-test-model"

    async def run(self, context, intent, contract, **kwargs):
        return DiscoveryOutcome(
            status=DiscoveryStatus.SUCCESS,
            reason_code="VERIFIED",
            decisions_used=2,
            verified_action_count=1,
            model_id=self.model_id,
            trace=_trace(),
            outputs={
                "available_balance": SecretStr("41.23"),
                "currency": SecretStr("USD"),
            },
        )


class _Replay:
    def __init__(self, result=None):
        self.result = result or InvocationResult(
            run_id="run_aaaaaaaaaaaaaaaa",
            status=InvocationStatus.SUCCESS,
            outputs={
                "available_balance": SecretStr("41.23"),
                "currency": SecretStr("USD"),
            },
        )
        self.calls = []

    async def run(self, reference, context):
        raise AssertionError("ordinary replay is not used in this test")

    async def run_validation(self, reference, context, test_session_binding):
        self.calls.append((reference, context, test_session_binding))
        return InvocationResult(
            run_id=context.run_alias,
            status=self.result.status,
            outputs=self.result.outputs,
            code=self.result.code,
            failure=self.result.failure,
            evidence_refs=self.result.evidence_refs,
        )


class _Oracle:
    def __init__(self, report=None):
        self.report = report or OracleReport(passed=True, code="ORACLE_PASS")
        self.calls = []

    async def check(self, result, fixture):
        self.calls.append((result, fixture))
        assert result.status is InvocationStatus.SUCCESS
        assert result.outputs["available_balance"].get_secret_value() == "41.23"
        assert fixture.account_id.get_secret_value() == VALIDATION_ACCOUNT
        return self.report


class _Backend:
    model_id = "offline-test-model"

    async def choose(self, request, *, timeout_seconds):
        raise AssertionError("the test backend should not be needed by this scripted discovery")


def _make_service(tmp_path, *, fixtures=True, oracle=None, replay=None):
    fixture_values = (
        (ValidationFixture("synthetic_beta", SecretStr(VALIDATION_ACCOUNT)),) if fixtures else ()
    )
    config = ApplicationConfig(
        data_root=tmp_path,
        principal_specs=(_principal(), _principal("synthetic_beta")),
        validation_fixtures=fixture_values,
        provider_enabled=True,
        provider_model="offline-test-model",
        target_revision=TARGET_REVISION,
        result_ttl_seconds=30,
    )
    sessions = _Sessions()
    registry = BundleRegistry(config.registry_root)
    evidence = EvidenceSink(config.evidence_root)
    service = ApplicationService(
        config,
        sessions=sessions,
        discovery=_Discovery(),
        replay=replay or _Replay(),
        registry=registry,
        evidence=evidence,
        decision_backend=_Backend(),
        validation_oracle=oracle,
    )
    return service, sessions, registry


async def _wait_terminal(service, run_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        view = await service.get_run(run_id)
        if view.state in {
            RunState.SUCCESS,
            RunState.BUSINESS_OUTCOME,
            RunState.FAILURE,
            RunState.ABORTED,
            RunState.SESSION_LOST,
        }:
            return view
        await asyncio.sleep(0.005)
    raise AssertionError("run did not reach a terminal state")


async def _discover_draft(service):
    await service.prepare_session("synthetic_alpha")
    handle = await service.start_discovery(
        DiscoveryRequest(
            session_id=MAIN_SESSION,
            goal=SecretStr(f"Get savings balance for {ACCOUNT}"),
            inputs={"account_id": SecretStr(ACCOUNT)},
            request_id="discover-1",
        )
    )
    return handle, await _wait_terminal(service, handle.run_id)


def test_successful_verified_discovery_compiles_real_draft_and_safe_catalog(tmp_path):
    async def scenario():
        service, _, registry = _make_service(tmp_path)
        handle, view = await _discover_draft(service)

        assert view.state is RunState.SUCCESS
        assert view.capability_reference is not None
        reference = view.capability_reference
        bundle = registry.load_draft(reference)
        assert bundle.provenance.verified is True
        assert bundle.provenance.trace_id == "trace_service_1"
        assert [step.id for step in bundle.steps if step.source.type == "observed"] == [
            "open_requested_account"
        ]

        catalog = await service.list_capabilities()
        assert len(catalog) == 1
        assert catalog[0].reference == reference
        assert catalog[0].lifecycle.value == "DRAFT"
        detail = await service.inspect_capability(reference)
        assert detail.bundle == bundle
        assert detail.lifecycle.value == "DRAFT"
        assert ACCOUNT not in repr(detail)
        assert (await service.get_run(handle.run_id)).capability_reference == reference

    asyncio.run(scenario())


def test_forged_discovery_success_without_verified_trace_does_not_create_draft(tmp_path):
    async def scenario():
        service, _, registry = _make_service(tmp_path)

        class _ForgedDiscovery:
            async def run(self, *args, **kwargs):
                return DiscoveryOutcome(
                    status=DiscoveryStatus.SUCCESS,
                    reason_code="VERIFIED",
                    decisions_used=1,
                    verified_action_count=1,
                    outputs={
                        "available_balance": SecretStr("41.23"),
                        "currency": SecretStr("USD"),
                    },
                )

        service._discovery = _ForgedDiscovery()
        handle, view = await _discover_draft(service)
        assert view.state is RunState.FAILURE
        assert view.capability_reference is None
        assert await service.list_capabilities() == ()
        result = await service.read_result(handle.run_id, MAIN_SESSION)
        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.INVALID_BUNDLE

    asyncio.run(scenario())


def test_validation_is_configured_fresh_and_qualification_requires_replay_and_oracle(tmp_path):
    async def scenario():
        oracle = _Oracle()
        service, sessions, registry = _make_service(tmp_path, oracle=oracle)
        _, discovery_view = await _discover_draft(service)
        reference = discovery_view.capability_reference
        assert reference is not None

        handle = await service.validate_capability(reference, request_id="validate-1")
        assert handle.mode is RunMode.VALIDATION
        view = await _wait_terminal(service, handle.run_id)
        assert view.state is RunState.SUCCESS
        assert len(sessions.prepared_validation) == 1
        assert len(sessions.closed) == 1
        assert len(oracle.calls) == 1
        assert len(service._replay.calls) == 1
        called_reference, context, binding = service._replay.calls[0]
        assert called_reference == reference
        assert context.input_bindings["inputs.account_id"].get_secret_value() == VALIDATION_ACCOUNT
        assert binding.session_id == sessions.prepared_validation[0]
        report = await service.validation_report(handle.run_id)
        assert report is not None and report.passed

        detail = await service.inspect_capability(reference)
        assert detail.lifecycle.value == "VALIDATED"
        assert detail.qualification is not None
        assert detail.qualification.bundle_digest == reference.digest
        assert detail.qualification.validation_run_ref == handle.run_id
        assert detail.latest_validation.passed is True
        with pytest.raises(ServiceError) as protected:
            await service.read_result(handle.run_id, sessions.prepared_validation[0])
        assert protected.value.code == "RESULT_FORBIDDEN"

    asyncio.run(scenario())


def test_failed_replay_or_oracle_leaves_draft_unqualified(tmp_path):
    async def scenario():
        failed_replay = InvocationResult(
            run_id="run_aaaaaaaaaaaaaaaa",
            status=InvocationStatus.FAILURE,
            failure=FailureDetail(reason_code=SafeReasonCode.POSTCONDITION_FAILED),
        )
        oracle = _Oracle()
        service, _, registry = _make_service(
            tmp_path / "replay_failure",
            oracle=oracle,
            replay=_Replay(failed_replay),
        )
        _, discovery_view = await _discover_draft(service)
        reference = discovery_view.capability_reference
        handle = await service.validate_capability(reference, request_id="validate-fail")
        view = await _wait_terminal(service, handle.run_id)
        assert view.state is RunState.FAILURE
        assert oracle.calls == []
        report = await service.validation_report(handle.run_id)
        assert report is not None and report.passed is False
        assert report.code == "REPLAY_FAILED"
        assert (await service.inspect_capability(reference)).lifecycle.value == "DRAFT"

        mismatch = _Oracle(OracleReport(passed=False, code="ORACLE_MISMATCH"))
        service2, _, registry2 = _make_service(tmp_path / "oracle_failure", oracle=mismatch)
        _, discovery_view2 = await _discover_draft(service2)
        reference2 = discovery_view2.capability_reference
        handle2 = await service2.validate_capability(reference2, request_id="oracle-fail")
        view2 = await _wait_terminal(service2, handle2.run_id)
        assert view2.state is RunState.FAILURE
        assert len(mismatch.calls) == 1
        assert (await service2.inspect_capability(reference2)).lifecycle.value == "DRAFT"

    asyncio.run(scenario())


def test_missing_oracle_and_fixture_fail_before_validation_browser_setup(tmp_path):
    async def scenario():
        service, sessions, registry = _make_service(tmp_path, oracle=None)
        _, view = await _discover_draft(service)
        with pytest.raises(ServiceError) as no_oracle:
            await service.validate_capability(view.capability_reference, request_id="no-oracle")
        assert no_oracle.value.code == "ORACLE_UNAVAILABLE"
        assert sessions.prepared_validation == []

        no_fixture, sessions2, _ = _make_service(tmp_path / "no-fixture", fixtures=False, oracle=_Oracle())
        _, view2 = await _discover_draft(no_fixture)
        with pytest.raises(ServiceError) as no_test_account:
            await no_fixture.validate_capability(view2.capability_reference, request_id="no-fixture")
        assert no_test_account.value.code == "VALIDATION_FIXTURE_UNAVAILABLE"
        assert sessions2.prepared_validation == []

        with pytest.raises(ServiceError) as reserved:
            await service.prepare_session("synthetic_beta")
        assert reserved.value.code == "INPUT_INVALID"

    asyncio.run(scenario())


def test_validation_rejects_runtime_drift_before_qualification(tmp_path, monkeypatch):
    async def scenario():
        oracle = _Oracle()
        service, sessions, registry = _make_service(tmp_path, oracle=oracle)
        _, discovery_view = await _discover_draft(service)
        reference = discovery_view.capability_reference
        assert reference is not None

        import cua.application.service as service_module

        original = service_module.current_runtime_fingerprint
        baseline = original()
        drifted = baseline.model_copy(update={"source_sha256": "f" * 64})
        calls = 0

        def fingerprint_with_drift():
            nonlocal calls
            calls += 1
            return baseline if calls == 1 else drifted

        monkeypatch.setattr(service_module, "current_runtime_fingerprint", fingerprint_with_drift)
        handle = await service.validate_capability(reference, request_id="drift-check")
        view = await _wait_terminal(service, handle.run_id)
        assert view.state is RunState.FAILURE
        report = await service.validation_report(handle.run_id)
        assert report is not None and report.code == "INVALID_BUNDLE"
        assert (await service.inspect_capability(reference)).lifecycle.value == "DRAFT"
        assert sessions.closed == [sessions.prepared_validation[0]]

    asyncio.run(scenario())


def test_approval_requires_exact_validated_digest(tmp_path):
    async def scenario():
        service, _, _ = _make_service(tmp_path, oracle=_Oracle())
        _, discovery_view = await _discover_draft(service)
        reference = discovery_view.capability_reference
        assert reference is not None
        handle = await service.validate_capability(reference, request_id="approve-check")
        await _wait_terminal(service, handle.run_id)

        with pytest.raises(ServiceError) as mismatch:
            await service.approve_capability(
                reference,
                expected_digest="0" * 64,
                reviewer_ref="local_operator",
                reviewer_type="operator",
            )
        assert mismatch.value.code == "DIGEST_MISMATCH"
        approval = await service.approve_capability(
            reference,
            expected_digest=reference.digest,
            reviewer_ref="local_operator",
            reviewer_type="operator",
        )
        assert approval.digest == reference.digest
        assert (await service.inspect_capability(reference)).lifecycle.value == "APPROVED"

    asyncio.run(scenario())


def test_initial_prepare_failure_closes_retained_context(tmp_path):
    async def scenario():
        class _RetainedFailure(_Sessions):
            async def prepare(self, principal_alias):
                self._next += 1
                session_id = "s_retained_0123456789"
                handle, state = self._new_session(session_id, principal_alias)
                self.handles[session_id] = handle
                self.states[session_id] = state
                raise SessionError("SUBJECT_MISMATCH", session_id)

        fixture = ValidationFixture("synthetic_beta", SecretStr(VALIDATION_ACCOUNT))
        config = ApplicationConfig(
            data_root=tmp_path,
            principal_specs=(_principal(), _principal("synthetic_beta")),
            validation_fixtures=(fixture,),
            provider_enabled=True,
            provider_model="offline-test-model",
            target_revision=TARGET_REVISION,
        )
        sessions = _RetainedFailure()
        service = ApplicationService(
            config,
            sessions=sessions,
            discovery=_Discovery(),
            replay=_Replay(),
            registry=BundleRegistry(config.registry_root),
            evidence=EvidenceSink(config.evidence_root),
            decision_backend=_Backend(),
        )
        with pytest.raises(ServiceError) as error:
            await service.prepare_session("synthetic_alpha")
        assert error.value.code == "SUBJECT_MISMATCH"
        assert sessions.closed == ["s_retained_0123456789"]
        assert sessions.handles == {}

    asyncio.run(scenario())


def test_shutdown_retries_browser_cleanup_after_close_all_failure(tmp_path):
    async def scenario():
        class _CloseAllFails(_Sessions):
            def __init__(self):
                super().__init__()
                self.close_all_calls = 0

            async def close_all(self):
                self.close_all_calls += 1
                if self.close_all_calls == 1:
                    raise RuntimeError("transient browser close")

        config = ApplicationConfig(
            data_root=tmp_path,
            principal_specs=(_principal(),),
            provider_enabled=True,
            provider_model="offline-test-model",
            target_revision=TARGET_REVISION,
        )
        sessions = _CloseAllFails()
        service = ApplicationService(
            config,
            sessions=sessions,
            discovery=_Discovery(),
            replay=_Replay(),
            registry=BundleRegistry(config.registry_root),
            evidence=EvidenceSink(config.evidence_root),
            decision_backend=_Backend(),
        )
        await service.prepare_session("synthetic_alpha")
        with pytest.raises(ServiceError) as first:
            await service.shutdown()
        assert first.value.code == "SERVICE_SHUTDOWN_INCOMPLETE"
        await service.shutdown()
        assert sessions.close_all_calls == 2

    asyncio.run(scenario())
