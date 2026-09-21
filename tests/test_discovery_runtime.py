import asyncio
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from cua.discovery.goals import BoundIntent, GoalBinder
from cua.discovery.runtime import DiscoveryRuntime, DiscoveryStatus
from cua.execution.contracts import EffectState, ExecutionContext, ExecutionResult
from cua.evidence.models import SafeReasonCode
from cua.models.actions import ClickDecision, DoneDecision, WaitDecision
from cua.models.bundles import CapabilityContract, InputContract, OutputContract
from cua.models.observations import Observation, ObservedControl
from cua.models.verification import MembershipProof
from cua.policy.engine import PolicyContext
from cua.sessions.actor import SessionActor
from cua.verification.completion import CompletionVerifier, VerificationStatus
from cua.surface.playwright_surface import NormalizedView, SurfaceError
from cua.llm.decisions import DecisionProviderError, DecisionReply


RUN = "run_0123456789abcdef"
SESSION = "s_0123456789abcdef"
ACCOUNT = "100001"
ORIGIN = "http://127.0.0.1:8080"
PROFILE = "parabank-native-v1"


def _contract():
    return CapabilityContract(
        inputs=(
            InputContract(
                name="account_id",
                value_type="string",
                pattern=r"^[0-9]+$",
                sensitive=True,
            ),
        ),
        outputs=(
            OutputContract(
                name="available_balance",
                value_type="decimal_string",
                sensitive=True,
            ),
            OutputContract(name="currency", value_type="string", enum=("USD",)),
        ),
    )


def _context():
    return ExecutionContext(
        run_alias=RUN,
        session_id=SESSION,
        expected_epoch=0,
        authentication_generation=4,
        target_origin=ORIGIN,
        profile_id=PROFILE,
        input_bindings={"inputs.account_id": SecretStr(ACCOUNT)},
        policy_context=PolicyContext(
            deployment_origins=(ORIGIN,),
            capability_origins=(ORIGIN,),
            run_origins=(ORIGIN,),
            deployment_routes=("home", "accounts_overview", "account_details"),
            capability_routes=("home", "accounts_overview", "account_details"),
            run_routes=("home", "accounts_overview", "account_details"),
            deployment_operations=("CLICK", "READ"),
            capability_operations=("CLICK", "READ"),
            run_operations=("CLICK", "READ"),
        ),
        membership_proof=None,
    )


def _observation(observation_id, route, state, controls=()):
    return Observation(
        id=observation_id,
        session_id=SESSION,
        page_id="p_0123456789ab",
        document_generation=1,
        frame_generations={"f_main": 1},
        captured_monotonic_ms=time.monotonic_ns() // 1_000_000,
        safe_route=route,
        state_tags=(state,),
        controls=tuple(controls),
        safe_text=(),
        fingerprint="fingerprint_012345",
    )


def _view(
    observation_id,
    route,
    state,
    *,
    account_ids=(),
    account_number=ACCOUNT,
    account_type="SAVINGS",
    available_balance="$1,234.50",
    principal_matches=True,
    detail_identity_match=True,
):
    is_overview = route == "accounts_overview"
    fields = {}
    relations = {}
    parseable = frozenset()
    if not is_overview:
        fields = {
            "PROFILE_ACCOUNT_NUMBER": (SecretStr(account_number),),
            "PROFILE_ACCOUNT_TYPE": (SecretStr(account_type),),
            "PROFILE_AVAILABLE_BALANCE": (SecretStr(available_balance),),
        }
        relations = {
            "PROFILE_ACCOUNT_NUMBER": True,
            "PROFILE_ACCOUNT_TYPE": True,
            "PROFILE_AVAILABLE_BALANCE": True,
        }
        parseable = frozenset({"PROFILE_AVAILABLE_BALANCE"})
    return NormalizedView(
        observation_id=observation_id,
        session_id=SESSION,
        authentication_generation=4,
        profile_id=PROFILE,
        origin=ORIGIN,
        safe_route=route,
        page_state=state,
        principal_matches=principal_matches,
        overview_complete=True if is_overview else None,
        membership_validity={"inputs.account_id": True} if is_overview else {},
        account_ids=frozenset(account_ids),
        field_values=fields,
        field_relations=relations,
        parseable_fields=parseable,
        detail_identity_match=detail_identity_match if not is_overview else None,
        captured_monotonic_ms=time.monotonic_ns() // 1_000_000,
    )


def _overview_pair():
    control = ObservedControl(
        ref="c_requested",
        frame_ref="f_main",
        role="link",
        safe_name="<requested_account>",
        enabled=True,
        visible=True,
        allowed_operations=("CLICK",),
        binding_ref="inputs.account_id",
    )
    obs = _observation("obs_overview_1", "accounts_overview", "OVERVIEW_READY", (control,))
    view = _view(
        obs.id,
        "accounts_overview",
        "OVERVIEW_READY",
        account_ids=(ACCOUNT, "100002"),
    )
    return obs, view


def _detail_pair(
    observation_id,
    *,
    account_number=ACCOUNT,
    account_type="SAVINGS",
    available_balance="$1,234.50",
    detail_identity_match=True,
):
    nav = ObservedControl(
        ref="c_overview",
        frame_ref="f_main",
        role="link",
        safe_name="Accounts Overview",
        enabled=True,
        visible=True,
        allowed_operations=("CLICK",),
    )
    obs = _observation(observation_id, "account_details", "DETAIL_READY", (nav,))
    view = _view(
        obs.id,
        "account_details",
        "DETAIL_READY",
        account_number=account_number,
        account_type=account_type,
        available_balance=available_balance,
        detail_identity_match=detail_identity_match,
    )
    return obs, view


def _gateway_proof(context):
    return MembershipProof(
        proof_ref="proof_gateway_1",
        run_ref=context.run_alias,
        session_ref=context.session_id,
        authentication_generation=context.authentication_generation,
        account_binding_ref="inputs.account_id",
        account_binding_value=context.input_bindings["inputs.account_id"],
        overview_observation_ref="obs_overview_1",
        overview_complete=True,
        account_present=True,
        verified_monotonic_ms=time.monotonic_ns() // 1_000_000,
    )


class FakeSessions:
    def __init__(self, actor, *, state="ACTIVE"):
        self.actor = actor
        self.state = state
        self.state_calls = 0

    async def get_state(self, session_id):
        assert session_id == SESSION
        self.state_calls += 1
        return SimpleNamespace(
            session_id=SESSION,
            state=self.state,
            profile_id=PROFILE,
            origin=ORIGIN,
            auth_generation=4,
            actor=self.actor,
        )


class FakeSurface:
    def __init__(self, actor, views, *, errors=None):
        self.actor = actor
        self.views = list(views)
        self.errors = dict(errors or {})
        self.observe_calls = 0
        self.lock_observations = []

    async def observe(self, session_id, *, bindings=None):
        assert session_id == SESSION
        assert bindings["inputs.account_id"].get_secret_value() == ACCOUNT
        self.observe_calls += 1
        self.lock_observations.append(self.actor._command_lock.locked())
        if self.observe_calls in self.errors:
            raise SurfaceError(self.errors[self.observe_calls])
        index = min(self.observe_calls - 1, len(self.views) - 1)
        return self.views[index]


class OfflineScriptedDecisionBackend:
    """Test-only, explicit DI; never selected by DiscoveryRuntime by default."""

    model_id = "offline-test-backend"

    def __init__(self, operations=("CLICK", "DONE")):
        self.operations = list(operations)
        self.requests = []

    async def choose(self, request, *, timeout_seconds):
        self.requests.append(request)
        operation = self.operations.pop(0)
        if operation == "CLICK":
            decision = ClickDecision(
                operation="CLICK",
                observation_id=request.observation.observation_id,
                reason_code="CONTINUE",
                rationale="Open the requested account.",
                control_ref="c_requested",
            )
        elif operation == "WAIT":
            decision = WaitDecision(
                operation="WAIT",
                observation_id=request.observation.observation_id,
                reason_code="WAIT",
                rationale="Wait for more page information.",
                timeout_ms=0,
            )
        else:
            decision = DoneDecision(
                operation="DONE",
                observation_id=request.observation.observation_id,
                reason_code="COMPLETE",
                rationale="The requested result is ready.",
            )
        return DecisionReply(decision, self.model_id, 10, 3)


class FakeGateway:
    def __init__(self, *, effect_state=EffectState.VERIFIED, reason=SafeReasonCode.AUTHORIZED):
        self.effect_state = effect_state
        self.reason = reason
        self.calls = []

    async def dispatch_observed(self, context, decision):
        self.calls.append((context, decision))
        proof = _gateway_proof(context) if self.effect_state is EffectState.VERIFIED else None
        return ExecutionResult(
            step_id="observed_action",
            effect_state=self.effect_state,
            reason_code=self.reason,
            observation_id=decision.observation_id,
            membership_proof=proof,
        )


class LockCheckingVerifier(CompletionVerifier):
    def __init__(self, actor):
        super().__init__()
        self.actor = actor
        self.called = False

    def verify(self, contract, view, context):
        self.called = True
        assert self.actor._command_lock.locked()
        return super().verify(contract, view, context)


def test_discovery_exposes_only_safe_inputs_and_emits_trace_after_fresh_proof():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        overview = _overview_pair()
        pre_done = _detail_pair("obs_detail_before", available_balance="$1.00")
        fresh_detail = _detail_pair("obs_detail_fresh", available_balance="$5,432.10")
        surface = FakeSurface(actor, (overview, pre_done, fresh_detail))
        sessions = FakeSessions(actor)
        gateway = FakeGateway()
        backend = OfflineScriptedDecisionBackend()
        verifier = LockCheckingVerifier(actor)
        runtime = DiscoveryRuntime(sessions, surface, gateway, verifier=verifier, backend=backend)
        intent = GoalBinder().bind("查询我的储蓄账户 100001 的可用余额")
        context = _context()

        try:
            outcome = await runtime.run(context, intent, _contract())
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is DiscoveryStatus.SUCCESS
        assert outcome.reason_code == "VERIFIED"
        assert outcome.verified_action_count == 1
        assert outcome.trace is not None and outcome.trace.success is True
        assert len(outcome.trace.events) == 1
        assert outcome.trace.events[0].effect_state == "VERIFIED"
        assert outcome.outputs["available_balance"].get_secret_value() == "5432.10"
        assert outcome.outputs["currency"].get_secret_value() == "USD"
        assert verifier.called is True
        assert surface.observe_calls == 3
        assert surface.lock_observations == [True, True, True]
        assert len(gateway.calls) == 1
        assert len(backend.requests) == 2
        model_payload = " ".join(
            request.model_dump_json() for request in backend.requests
        )
        assert ACCOUNT not in model_payload
        assert "$1.00" not in model_payload
        assert "$5,432.10" not in model_payload

    asyncio.run(scenario())


def test_default_backend_reports_disabled_without_observing_or_using_provider():
    async def scenario():
        actor = SessionActor()
        sessions = FakeSessions(actor)
        surface = FakeSurface(actor, (_overview_pair(),))
        gateway = FakeGateway()
        runtime = DiscoveryRuntime(sessions, surface, gateway)

        outcome = await runtime.run(
            _context(),
            GoalBinder().bind("Get available savings balance for 100001"),
            _contract(),
        )

        assert outcome.status is DiscoveryStatus.FAILURE
        assert outcome.reason_code == "MODEL_NOT_CONFIGURED"
        assert outcome.decisions_used == 0
        assert sessions.state_calls == 0
        assert surface.observe_calls == 0
        assert gateway.calls == []

    asyncio.run(scenario())


def test_invalid_manually_constructed_binding_is_rejected_before_ui():
    async def scenario():
        actor = SessionActor()
        sessions = FakeSessions(actor)
        surface = FakeSurface(actor, (_overview_pair(),))
        intent = BoundIntent(
            code="get_savings_balance",
            safe_goal="Get the available balance for the requested savings account.",
            requested_account_id=SecretStr("id_100001"),
        )
        context = _context().model_copy(
            update={"input_bindings": {"inputs.account_id": SecretStr("id_100001")}}
        )
        outcome = await DiscoveryRuntime(sessions, surface, FakeGateway()).run(
            context, intent, _contract()
        )

        assert outcome.status is DiscoveryStatus.FAILURE
        assert outcome.reason_code == "INPUT_INVALID"
        assert sessions.state_calls == 0
        assert surface.observe_calls == 0

    asyncio.run(scenario())


def test_unknown_click_effect_stops_without_retry_or_verified_trace():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        surface = FakeSurface(actor, (_overview_pair(),))
        backend = OfflineScriptedDecisionBackend(("CLICK", "CLICK"))
        gateway = FakeGateway(effect_state=EffectState.OUTCOME_UNKNOWN)
        runtime = DiscoveryRuntime(FakeSessions(actor), surface, gateway, backend=backend)

        try:
            outcome = await runtime.run(
                _context(),
                GoalBinder().bind("Get available savings balance for 100001"),
                _contract(),
            )
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is DiscoveryStatus.INCOMPLETE
        assert outcome.reason_code == SafeReasonCode.AUTHORIZED.value
        assert outcome.decisions_used == 1
        assert outcome.verified_action_count == 0
        assert outcome.trace is None
        assert len(backend.requests) == len(gateway.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("account_number", "account_type", "reason"),
    [
        ("999999", "SAVINGS", "SUBJECT_MISMATCH"),
        (ACCOUNT, "CHECKING", "ACCOUNT_TYPE_MISMATCH"),
    ],
)
def test_done_with_fresh_identity_or_account_type_failure_is_hard_failure(
    account_number, account_type, reason
):
    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        surface = FakeSurface(
            actor,
            (
                _overview_pair(),
                _detail_pair("obs_detail_before"),
                _detail_pair(
                    "obs_detail_fresh",
                    account_number=account_number,
                    account_type=account_type,
                ),
            ),
        )
        runtime = DiscoveryRuntime(
            FakeSessions(actor),
            surface,
            FakeGateway(),
            backend=OfflineScriptedDecisionBackend(),
        )

        try:
            outcome = await runtime.run(
                _context(),
                GoalBinder().bind("Get available savings balance for 100001"),
                _contract(),
            )
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is DiscoveryStatus.FAILURE
        assert outcome.reason_code == reason
        assert outcome.trace is None
        assert outcome.verification is not None
        assert outcome.verification.status is VerificationStatus.FAILED
        assert outcome.outputs == {}

    asyncio.run(scenario())


def test_decision_budget_counts_each_model_choice():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        backend = OfflineScriptedDecisionBackend(("WAIT", "WAIT"))
        runtime = DiscoveryRuntime(
            FakeSessions(actor),
            FakeSurface(actor, (_overview_pair(),)),
            FakeGateway(),
            backend=backend,
        )

        try:
            outcome = await runtime.run(
                _context(),
                GoalBinder().bind("Get available savings balance for 100001"),
                _contract(),
                max_decisions=2,
            )
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is DiscoveryStatus.DECISION_LIMIT
        assert outcome.decisions_used == 2
        assert len(backend.requests) == 2
        assert outcome.trace is None

    asyncio.run(scenario())


def test_first_page_repeated_done_never_emits_outputs_or_trace():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        backend = OfflineScriptedDecisionBackend(("DONE", "DONE"))
        surface = FakeSurface(actor, (_overview_pair(),))
        runtime = DiscoveryRuntime(FakeSessions(actor), surface, FakeGateway(), backend=backend)

        try:
            outcome = await runtime.run(
                _context(),
                GoalBinder().bind("Get available savings balance for 100001"),
                _contract(),
                max_decisions=2,
            )
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is DiscoveryStatus.DECISION_LIMIT
        assert outcome.decisions_used == 2
        assert outcome.trace is None
        assert outcome.outputs == {}
        assert surface.observe_calls == 4

    asyncio.run(scenario())


def test_subject_mismatch_surface_error_is_preserved_as_hard_failure():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        surface = FakeSurface(
            actor,
            (_overview_pair(), _detail_pair("obs_detail_before")),
            errors={3: "SUBJECT_MISMATCH"},
        )
        runtime = DiscoveryRuntime(
            FakeSessions(actor), surface, FakeGateway(), backend=OfflineScriptedDecisionBackend()
        )

        try:
            outcome = await runtime.run(
                _context(),
                GoalBinder().bind("Get available savings balance for 100001"),
                _contract(),
            )
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is DiscoveryStatus.FAILURE
        assert outcome.reason_code == "SUBJECT_MISMATCH"
        assert outcome.trace is None
        assert outcome.outputs == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("state", "blocker", "expected_status", "expected_reason"),
    (
        ("APP_ERROR", False, DiscoveryStatus.FAILURE, "APP_ERROR"),
        ("ACCESS_DENIED", False, DiscoveryStatus.BUSINESS_OUTCOME, "ACCESS_DENIED"),
        ("UNKNOWN", True, DiscoveryStatus.BLOCKED, "UNKNOWN_BLOCKER"),
    ),
)
def test_terminal_or_unknown_blocker_states_stop_before_model_or_gateway(
    state, blocker, expected_status, expected_reason
):
    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        observation = _observation("obs_terminal", "account_details", state)
        populated = _view(observation.id, "account_details", state)
        view = replace(
            populated,
            field_values={},
            field_relations={},
            parseable_fields=frozenset(),
            detail_identity_match=None,
            unknown_blocker=blocker,
        )
        surface = FakeSurface(actor, ((observation, view),))
        gateway = FakeGateway()
        backend = OfflineScriptedDecisionBackend()
        runtime = DiscoveryRuntime(FakeSessions(actor), surface, gateway, backend=backend)

        try:
            outcome = await runtime.run(
                _context(),
                GoalBinder().bind("查询我的储蓄账户 100001 的可用余额"),
                _contract(),
            )
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is expected_status
        assert outcome.reason_code == expected_reason
        assert outcome.trace is None
        assert outcome.outputs == {}
        assert surface.observe_calls == 1
        assert backend.requests == []
        assert gateway.calls == []

    asyncio.run(scenario())


def test_ambiguous_surface_state_is_a_hard_failure_before_model_or_gateway():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        surface = FakeSurface(
            actor,
            (_overview_pair(),),
            errors={1: "AMBIGUOUS_STATE"},
        )
        gateway = FakeGateway()
        backend = OfflineScriptedDecisionBackend()
        runtime = DiscoveryRuntime(FakeSessions(actor), surface, gateway, backend=backend)

        try:
            outcome = await runtime.run(
                _context(),
                GoalBinder().bind("Get available savings balance for 100001"),
                _contract(),
            )
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is DiscoveryStatus.FAILURE
        assert outcome.reason_code == "AMBIGUOUS_STATE"
        assert outcome.trace is None
        assert outcome.outputs == {}
        assert backend.requests == []
        assert gateway.calls == []

    asyncio.run(scenario())


def test_expired_session_is_blocked_for_human_reauthentication():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        sessions = FakeSessions(actor, state="SESSION_EXPIRED")
        surface = FakeSurface(actor, (_overview_pair(),))
        runtime = DiscoveryRuntime(
            sessions, surface, FakeGateway(), backend=OfflineScriptedDecisionBackend()
        )

        try:
            outcome = await runtime.run(
                _context(),
                GoalBinder().bind("Get available savings balance for 100001"),
                _contract(),
            )
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is DiscoveryStatus.BLOCKED
        assert outcome.reason_code == "SESSION_EXPIRED"
        assert surface.observe_calls == 0

    asyncio.run(scenario())


def test_provider_error_marks_token_usage_unknown():
    class UnavailableBackend:
        model_id = "offline-test-error"

        async def choose(self, request, *, timeout_seconds):
            raise DecisionProviderError("PROVIDER_UNAVAILABLE")

    async def scenario():
        actor = SessionActor()
        await actor.begin_run(RUN)
        runtime = DiscoveryRuntime(
            FakeSessions(actor),
            FakeSurface(actor, (_overview_pair(),)),
            FakeGateway(),
            backend=UnavailableBackend(),
        )

        try:
            outcome = await runtime.run(
                _context(),
                GoalBinder().bind("Get available savings balance for 100001"),
                _contract(),
            )
        finally:
            await actor.finish_run(RUN)

        assert outcome.status is DiscoveryStatus.FAILURE
        assert outcome.reason_code == "MODEL_UNAVAILABLE"
        assert outcome.decisions_used == 1
        assert outcome.input_tokens is None
        assert outcome.output_tokens is None

    asyncio.run(scenario())
