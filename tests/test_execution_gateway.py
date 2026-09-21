import asyncio
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
import time

import pytest
from pydantic import SecretStr

from cua.conditions.evaluator import ConditionContext
from cua.execution import EffectState, ExecutionContext, ExecutionGateway
from cua.evidence import EvidenceSink, RunMetadata, RunMode
from cua.models.actions import ClickDecision, WaitDecision
from cua.models.bundles import (
    BundleStep,
    CapabilityBundle,
    CapabilityContract,
    CapabilityIdentity,
    Compatibility,
    InputContract,
    OutputContract,
    RecoveryDefinition,
    StepSource,
    TargetDefinition,
    TraceProvenance,
)
from cua.models.conditions import PageStateCondition, ParseableCondition
from cua.models.observations import Observation, ObservedControl
from cua.models.verification import MembershipProof
from cua.policy import PolicyContext, PolicyEngine, Risk
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from cua.sessions.actor import SessionActor
from cua.sessions.manager import SessionHandle


@dataclass(frozen=True)
class FakeView:
    observation_id: str
    session_id: str
    authentication_generation: int
    profile_id: str
    origin: str
    safe_route: str
    page_state: str
    principal_matches: bool | None = True
    detail_identity_match: bool | None = True
    overview_complete: bool | None = True
    membership_validity: dict | None = None
    field_values: dict | None = None
    field_relations: dict | None = None

    def condition_context(self, input_values=None):
        return ConditionContext(
            page_state=self.page_state,
            principal_matches=self.principal_matches,
            overview_complete=self.overview_complete,
            account_presence=MappingProxyType({"inputs.account_id": True}),
            membership_validity=MappingProxyType(self.membership_validity or {}),
            field_values=MappingProxyType(self.field_values or {}),
            input_values=MappingProxyType(dict(input_values or {})),
        )


@dataclass(frozen=True)
class FakeResolvedTarget:
    target_ref: str = "overview_nav"
    locator_key: str = "ROLE_LINK_ACCOUNTS_OVERVIEW"
    allowed_operations: tuple[str, ...] = ("CLICK",)
    binding_ref: str | None = None
    observation_id: str = "obs_before"
    control_ref: str | None = "c_1"
    frame_ref: str = "f_main"
    page_id: str = "p_1"
    document_generation: int = 1
    authentication_generation: int = 4
    session_id: str = "s_1"
    unique: bool = True
    visible: bool = True
    enabled: bool = True
    token: str = "token"
    binding_value: SecretStr | None = None
    destination_origin: str = "http://127.0.0.1:8080"
    destination_route: str = "accounts_overview"
    runtime_risk: str = "READ_ONLY"


class FakeManager:
    def __init__(self, actor):
        self.actor = actor
        self.get_calls_inside_actor = []
        self.handle = SessionHandle(
            session_id="s_1",
            page_id="p_1",
            principal_alias="fixture-a",
            profile_id="parabank-native-v1",
            origin="http://127.0.0.1:8080",
            browser_version="1.60.0",
            auth_generation=4,
        )

    async def get_state(self, session_id):
        assert session_id == "s_1"
        return SimpleNamespace(
            actor=self.actor,
            state="ACTIVE",
            profile_id="parabank-native-v1",
            origin="http://127.0.0.1:8080",
            auth_generation=4,
        )

    async def get(self, session_id):
        self.get_calls_inside_actor.append(self.actor._command_lock.locked())
        assert session_id == "s_1"
        return SimpleNamespace(handle=self.handle, actor=self.actor, state="ACTIVE")


class FakeSurface:
    def __init__(self, *, views=None, duplicate_control=False, lost_receipt=False, resolved_overrides=None):
        self.views = list(views or (_page("obs_before", "AUTHENTICATED_HOME", "home"),
                                    _page("obs_after", "OVERVIEW_READY", "accounts_overview")))
        self.duplicate_control = duplicate_control
        self.lost_receipt = lost_receipt
        self.resolved_overrides = dict(resolved_overrides or {})
        self.observe_calls = 0
        self.resolve_calls = 0
        self.execute_calls = 0
        self.side_effects = 0
        self.effect_completed = False
        self.post_observe_calls = 0

    async def observe(self, session_id, *, bindings=None):
        self.observe_calls += 1
        if self.effect_completed and len(self.views) > 1:
            index = min(1 + self.post_observe_calls, len(self.views) - 1)
            self.post_observe_calls += 1
        else:
            index = 0
        observation, view = self.views[index]
        return observation, view

    async def resolve_target(self, session_id, observation_id, target, bindings):
        self.resolve_calls += 1
        values = dict(
            target_ref=target.ref,
            locator_key=target.locator,
            allowed_operations=tuple(target.allowed_operations),
            binding_ref=target.binding_ref,
            observation_id=observation_id,
            control_ref="c_1" if "CLICK" in target.allowed_operations else None,
            page_id="p_1",
            document_generation=1,
            authentication_generation=4,
            session_id=session_id,
            destination_route=(
                "account_details"
                if target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT"
                else "accounts_overview"
            ),
            runtime_risk="READ_ONLY",
        )
        values.update(self.resolved_overrides)
        return FakeResolvedTarget(**values)

    async def current_observation(self, session_id, expected_observation_id):
        for observation, view in self.views:
            if observation.id == expected_observation_id:
                return observation, view
        from cua.surface.playwright_surface import SurfaceError
        raise SurfaceError("STALE_OBSERVATION")

    async def resolve_observed(self, session_id, decision, *, frame_ref="f_main"):
        observation, _ = next(
            (pair for pair in self.views if pair[0].id == decision.observation_id),
            (None, None),
        )
        if observation is None:
            from cua.surface.playwright_surface import SurfaceError
            raise SurfaceError("STALE_OBSERVATION")
        control = next(item for item in observation.controls if item.ref == decision.control_ref)
        locator = (
            "ROLE_LINK_ACCOUNTS_OVERVIEW"
            if control.safe_name == "Accounts Overview"
            else "TABLE_ACCOUNT_LINK_BY_INPUT"
        )
        values = dict(
            target_ref="observed_target",
            locator_key=locator,
            allowed_operations=("CLICK",),
            binding_ref="inputs.account_id" if locator == "TABLE_ACCOUNT_LINK_BY_INPUT" else None,
            observation_id=decision.observation_id,
            control_ref=decision.control_ref,
            frame_ref=frame_ref,
            session_id=session_id,
            destination_route="accounts_overview" if locator == "ROLE_LINK_ACCOUNTS_OVERVIEW" else "account_details",
        )
        values.update(self.resolved_overrides)
        return FakeResolvedTarget(**values)

    async def execute(self, session_id, decision, resolved):
        self.execute_calls += 1
        assert isinstance(decision, ClickDecision)
        self.side_effects += 1
        self.effect_completed = True
        if self.lost_receipt:
            raise RuntimeError("receipt lost after browser side effect")


def _page(observation_id, page_state, route, *, controls=None, auth_generation=4):
    if controls is None:
        controls = (
            ObservedControl(
                ref="c_1",
                frame_ref="f_main",
                role="link",
                safe_name="Accounts Overview",
                enabled=True,
                visible=True,
                allowed_operations=("CLICK",),
            ),
        )
    observation = Observation(
        id=observation_id,
        session_id="s_1",
        page_id="p_1",
        document_generation=1,
        frame_generations={"f_main": 1},
        captured_monotonic_ms=100,
        safe_route=route,
        state_tags=(page_state,),
        controls=tuple(controls),
        safe_text=("Welcome Customer Secret",),
        fingerprint="fingerprint",
    )
    view = FakeView(
        observation_id=observation_id,
        session_id="s_1",
        authentication_generation=auth_generation,
        profile_id="parabank-native-v1",
        origin="http://127.0.0.1:8080",
        safe_route=route,
        page_state=page_state,
        membership_validity={"inputs.account_id": True},
    )
    return observation, view


def _bundle(*, kind="CLICK", postcondition="OVERVIEW_READY", recovery=False):
    fingerprint = current_runtime_fingerprint()
    step_fields = {}
    if kind == "TYPE_TEXT":
        step_fields["input_ref"] = "inputs.account_id"
    return CapabilityBundle(
        schema_version="1",
        capability=CapabilityIdentity(name="get_savings_balance", version="1.0.0"),
        compatibility=Compatibility(
            profile="parabank-native-v1",
            runtime_contract="cua-v1",
            profile_sha256=fingerprint.profile_sha256,
            condition_runtime_sha256=fingerprint.condition_sha256,
        ),
        contract=CapabilityContract(
            inputs=(
                InputContract(
                    name="account_id",
                    value_type="string",
                    pattern=r"^[0-9]+$",
                    sensitive=True,
                ),
            ),
            outputs=(
                OutputContract(name="available_balance", value_type="decimal_string", sensitive=True),
                OutputContract(name="currency", value_type="string", enum=("USD",)),
            ),
        ),
        steps=(
            BundleStep(
                id="open_overview",
                kind=kind,
                target_ref="overview_nav",
                preconditions=(PageStateCondition(kind="page_state", value="AUTHENTICATED_HOME"),),
                postconditions=(PageStateCondition(kind="page_state", value=postcondition),),
                recovery_ref="overview_recovery" if recovery else None,
                source=StepSource(type="declared"),
                **step_fields,
            ),
        ),
        targets=(
            TargetDefinition(
                ref="overview_nav",
                locator="ROLE_LINK_ACCOUNTS_OVERVIEW",
                allowed_operations=("CLICK",),
            ),
        ),
        recoveries=(
            RecoveryDefinition(
                ref="overview_recovery",
                anchor_target_ref="overview_nav",
                max_attempts=2,
            ),
        ) if recovery else (),
        provenance=TraceProvenance(
            trace_id="trace_1",
            verified=True,
            completion_proof_id="proof_1",
        ),
    )


def _extract_bundle():
    base = _bundle()
    data = base.model_dump(mode="python")
    data["steps"] = (
        BundleStep(
            id="read_balance",
            kind="EXTRACT",
            target_ref="available_balance",
            preconditions=(PageStateCondition(kind="page_state", value="DETAIL_READY"),),
            postconditions=(
                ParseableCondition(
                    kind="parseable",
                    target_ref="available_balance",
                    parser_id="USD_DECIMAL_V1",
                    parser_version="1",
                ),
            ),
            output_ref="available_balance",
            parser_id="USD_DECIMAL_V1",
            parser_version="1",
            source=StepSource(type="declared"),
        ),
    )
    data["targets"] = (
        TargetDefinition(
            ref="available_balance",
            locator="PROFILE_AVAILABLE_BALANCE",
            allowed_operations=("READ",),
        ),
    )
    return CapabilityBundle.model_validate(data)


def _context(run_alias, actor, policy_updates=None, expected_epoch=0):
    policy = PolicyContext(
        deployment_origins=("http://127.0.0.1:8080",),
        capability_origins=("http://127.0.0.1:8080",),
        run_origins=("http://127.0.0.1:8080",),
        deployment_routes=("home", "accounts_overview", "account_details"),
        capability_routes=("home", "accounts_overview", "account_details"),
        run_routes=("home", "accounts_overview", "account_details"),
        deployment_operations=("CLICK", "READ"),
        capability_operations=("CLICK", "READ"),
        run_operations=("CLICK", "READ"),
    )
    if policy_updates:
        policy = policy.model_copy(update=policy_updates)
    return ExecutionContext(
        run_alias=run_alias,
        session_id="s_1",
        expected_epoch=expected_epoch,
        authentication_generation=4,
        target_origin="http://127.0.0.1:8080",
        profile_id="parabank-native-v1",
        policy_context=policy,
        input_bindings={"inputs.account_id": SecretStr("100001")},
        membership_proof=None,
    )


def _membership_proof(run_alias):
    return MembershipProof(
        proof_ref="proof_test",
        run_ref=run_alias,
        session_ref="s_1",
        authentication_generation=4,
        account_binding_ref="inputs.account_id",
        account_binding_value=SecretStr("100001"),
        overview_observation_ref="obs_overview",
        overview_complete=True,
        account_present=True,
        verified_monotonic_ms=100,
    )


def _evidence(tmp_path):
    sink = EvidenceSink(tmp_path / "evidence")
    run_alias = sink.register_run(
        RunMetadata(
            mode=RunMode.REPLAY,
            capability_name="get_savings_balance",
            capability_version="1.0.0",
            bundle_digest="a" * 64,
            profile_id="parabank-native-v1",
            target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
            browser_version="1.60.0",
        )
    )
    return sink, run_alias


@pytest.mark.parametrize("denial", ["policy", "stale", "ambiguous", "precondition"])
def test_gateway_denials_are_not_dispatched_and_have_no_side_effect(tmp_path, denial):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface()
        manager = FakeManager(actor)
        context = _context(
            run_alias,
            actor,
            policy_updates={"run_operations": ("READ",)} if denial == "policy" else None,
            expected_epoch=1 if denial == "stale" else 0,
        )
        bundle = _bundle(
            postcondition="OVERVIEW_READY",
        )
        if denial == "ambiguous":
            observation, view = surface.views[0]
            duplicate = observation.controls[0].model_copy(update={"ref": "c_1"})
            surface.views[0] = (observation.model_copy(update={"controls": (observation.controls[0], duplicate)}), view)
        if denial == "precondition":
            bundle = _bundle()
            observation, view = surface.views[0]
            surface.views[0] = (
                observation.model_copy(update={"state_tags": ("UNKNOWN",)}),
                FakeView(**{**view.__dict__, "page_state": "UNKNOWN"}),
            )
        gateway = ExecutionGateway(manager, surface, PolicyEngine(), sink)
        result = await gateway.dispatch(context, bundle, "open_overview")
        assert result.effect_state is EffectState.NOT_DISPATCHED
        assert surface.side_effects == 0
        assert surface.execute_calls == 0
        return result, surface, manager

    result, surface, manager = asyncio.run(scenario())
    assert result.reason_code is not None
    assert manager.get_calls_inside_actor == ([] if denial == "stale" else [True])
    if denial in {"stale", "precondition"}:
        assert surface.resolve_calls == 0


def test_gateway_validates_postcondition_and_serializes_manager_access(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface()
        manager = FakeManager(actor)
        gateway = ExecutionGateway(manager, surface, PolicyEngine(), sink)

        result = await gateway.dispatch(_context(run_alias, actor), _bundle(), "open_overview")
        return result, surface, manager

    result, surface, manager = asyncio.run(scenario())
    assert result.effect_state is EffectState.VERIFIED
    assert result.reason_code == "AUTHORIZED"
    assert surface.side_effects == 1
    assert surface.observe_calls == 2
    assert manager.get_calls_inside_actor == [True]


def test_gateway_waits_for_transient_post_click_readiness_without_redispatch(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        initial = _page("obs_before", "AUTHENTICATED_HOME", "home")
        loading = _page("obs_loading", "OVERVIEW_LOADING", "accounts_overview")
        loading = (
            loading[0],
            FakeView(**{**loading[1].__dict__, "overview_complete": False}),
        )
        ready = _page("obs_ready", "OVERVIEW_READY", "accounts_overview")
        surface = FakeSurface(views=(initial, loading, ready))
        result = await ExecutionGateway(
            FakeManager(actor), surface, PolicyEngine(), sink
        ).dispatch(_context(run_alias, actor), _bundle(), "open_overview")
        return result, surface

    result, surface = asyncio.run(scenario())
    assert result.effect_state is EffectState.VERIFIED
    assert result.observation_id == "obs_ready"
    assert surface.execute_calls == surface.side_effects == 1
    assert surface.observe_calls == 3


def test_gateway_exhausts_post_click_observation_deadline_without_retry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cua.execution.gateway._POST_EFFECT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("cua.execution.gateway._POST_EFFECT_POLL_INTERVAL_SECONDS", 0.001)

    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        initial = _page("obs_before", "AUTHENTICATED_HOME", "home")
        loading = _page("obs_loading", "OVERVIEW_LOADING", "accounts_overview")
        loading = (
            loading[0],
            FakeView(**{**loading[1].__dict__, "overview_complete": False}),
        )
        surface = FakeSurface(views=(initial, loading))
        result = await ExecutionGateway(
            FakeManager(actor), surface, PolicyEngine(), sink
        ).dispatch(_context(run_alias, actor), _bundle(), "open_overview")
        return result, surface

    result, surface = asyncio.run(scenario())
    assert result.effect_state is EffectState.OUTCOME_UNKNOWN
    assert result.reason_code == "POSTCONDITION_UNKNOWN"
    assert surface.execute_calls == surface.side_effects == 1




def test_gateway_marks_lost_click_receipt_unknown_without_retry(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface(lost_receipt=True)
        gateway = ExecutionGateway(FakeManager(actor), surface, PolicyEngine(), sink)

        result = await gateway.dispatch(_context(run_alias, actor), _bundle(), "open_overview")
        return result, surface

    result, surface = asyncio.run(scenario())
    assert result.effect_state is EffectState.OUTCOME_UNKNOWN
    assert surface.side_effects == surface.execute_calls == 1
    assert surface.observe_calls == 1


def test_gateway_rejects_unsupported_action_before_any_surface_call(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface()
        gateway = ExecutionGateway(FakeManager(actor), surface, PolicyEngine(), sink)

        result = await gateway.dispatch(
            _context(run_alias, actor),
            _bundle(kind="TYPE_TEXT"),
            "open_overview",
        )
        return result, surface

    result, surface = asyncio.run(scenario())
    assert result.effect_state is EffectState.NOT_DISPATCHED
    assert result.reason_code == "ACTION_NOT_SUPPORTED"
    assert surface.observe_calls == surface.resolve_calls == surface.execute_calls == 0


def test_gateway_rejects_expired_run_deadline_before_session_or_browser_access(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface()
        manager = FakeManager(actor)
        context = _context(run_alias, actor).model_copy(
            update={"deadline_monotonic": time.monotonic() - 1}
        )
        result = await ExecutionGateway(
            manager, surface, PolicyEngine(), sink
        ).dispatch(context, _bundle(), "open_overview")
        return result, surface, manager

    result, surface, manager = asyncio.run(scenario())
    assert result.effect_state is EffectState.NOT_DISPATCHED
    assert result.reason_code == "RECOVERY_EXHAUSTED"
    assert surface.observe_calls == surface.resolve_calls == surface.execute_calls == 0
    assert manager.get_calls_inside_actor == []


def test_gateway_reconciles_lost_receipt_by_observing_effect_without_redispatch(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface(lost_receipt=True)
        context = _context(run_alias, actor)
        gateway = ExecutionGateway(FakeManager(actor), surface, PolicyEngine(), sink)
        dispatched = await gateway.dispatch(context, _bundle(), "open_overview")
        reconciled = await gateway.reconcile_unknown(
            context, _bundle(), "open_overview", dispatched.membership_proof
        )
        return dispatched, reconciled, surface

    dispatched, reconciled, surface = asyncio.run(scenario())
    assert dispatched.effect_state is EffectState.OUTCOME_UNKNOWN
    assert reconciled.effect_state is EffectState.VERIFIED
    assert reconciled.observation_id == "obs_after"
    assert surface.observe_calls == 2
    assert surface.execute_calls == surface.side_effects == 1


def test_gateway_marks_unknown_effect_retry_safe_only_after_safe_source_observation(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        initial = _page("obs_before", "AUTHENTICATED_HOME", "home")
        still_home = _page("obs_after", "AUTHENTICATED_HOME", "home")
        surface = FakeSurface(views=(initial, still_home), lost_receipt=True)
        context = _context(run_alias, actor)
        bundle = _bundle(recovery=True)
        gateway = ExecutionGateway(FakeManager(actor), surface, PolicyEngine(), sink)
        dispatched = await gateway.dispatch(context, bundle, "open_overview")
        reconciled = await gateway.reconcile_unknown(
            context, bundle, "open_overview", dispatched.membership_proof
        )
        return dispatched, reconciled, surface

    dispatched, reconciled, surface = asyncio.run(scenario())
    assert dispatched.effect_state is EffectState.OUTCOME_UNKNOWN
    assert reconciled.effect_state is EffectState.OUTCOME_UNKNOWN
    assert reconciled.retry_safe is True
    assert reconciled.observation_id == "obs_after"
    assert surface.execute_calls == surface.side_effects == 1


def test_invalid_bound_input_is_rejected_before_browser_observation(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface()
        manager = FakeManager(actor)
        context = _context(run_alias, actor).model_copy(
            update={"input_bindings": {"inputs.account_id": SecretStr("not-an-id")}}
        )
        result = await ExecutionGateway(manager, surface, PolicyEngine(), sink).dispatch(
            context, _bundle(), "open_overview"
        )
        return result, surface, manager

    result, surface, manager = asyncio.run(scenario())
    assert result.effect_state is EffectState.NOT_DISPATCHED
    assert result.reason_code == "INPUT_INVALID"
    assert surface.observe_calls == surface.resolve_calls == surface.execute_calls == 0
    assert manager.get_calls_inside_actor == []


@pytest.mark.parametrize(
    ("resolved_updates", "expected_reason"),
    [
        ({"destination_origin": "https://evil.example"}, "ORIGIN_NOT_ALLOWED"),
        ({"destination_route": "transfer"}, "ROUTE_NOT_ALLOWED"),
        ({"runtime_risk": "WRITE"}, "RISK_NOT_READ_ONLY"),
    ],
)
def test_live_click_destination_is_classified_independently_of_artifact_claim(
    tmp_path, resolved_updates, expected_reason
):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface(resolved_overrides=resolved_updates)
        context = _context(run_alias, actor)
        # Even an artifact-side READ_ONLY claim cannot override the live href classification.
        context = context.model_copy(
            update={
                "policy_context": context.policy_context.model_copy(
                    update={"artifact_risk_label": Risk.READ_ONLY}
                )
            }
        )
        result = await ExecutionGateway(
            FakeManager(actor), surface, PolicyEngine(), sink
        ).dispatch(context, _bundle(), "open_overview")
        return result, surface

    result, surface = asyncio.run(scenario())
    assert result.effect_state is EffectState.NOT_DISPATCHED
    assert result.reason_code == expected_reason
    assert surface.side_effects == surface.execute_calls == 0


def test_read_extract_uses_pinned_field_and_returns_secret_without_click(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        observation, view = _page("obs_detail", "DETAIL_READY", "account_details")
        view = FakeView(
            **{
                **view.__dict__,
                "field_values": {
                    "PROFILE_AVAILABLE_BALANCE": (SecretStr("$0.00"),),
                },
                "field_relations": {"PROFILE_AVAILABLE_BALANCE": True},
            }
        )
        surface = FakeSurface(views=((observation, view),))
        manager = FakeManager(actor)
        context = _context(run_alias, actor)
        context = context.model_copy(
            update={
                "membership_proof": _membership_proof(run_alias),
            }
        )
        result = await ExecutionGateway(
            manager, surface, PolicyEngine(), sink
        ).dispatch(context, _extract_bundle(), "read_balance")
        return result, surface, manager

    result, surface, manager = asyncio.run(scenario())
    assert result.effect_state is EffectState.VERIFIED
    assert result.value.get_secret_value() == "$0.00"
    assert "$0.00" not in repr(result)
    assert surface.observe_calls == 1
    assert surface.resolve_calls == 1
    assert surface.execute_calls == surface.side_effects == 0
    assert manager.get_calls_inside_actor == [True]


def test_discovery_dispatch_uses_stored_fresh_observation_and_shared_click_path(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface()
        manager = FakeManager(actor)
        decision = ClickDecision(
            observation_id="obs_before",
            reason_code="MODEL_PROPOSED",
            rationale="Use visible overview navigation.",
            operation="CLICK",
            control_ref="c_1",
        )
        result = await ExecutionGateway(
            manager, surface, PolicyEngine(), sink
        ).dispatch_observed(_context(run_alias, actor), decision)
        return result, surface, manager

    result, surface, manager = asyncio.run(scenario())
    assert result.effect_state is EffectState.VERIFIED
    assert result.reason_code == "AUTHORIZED"
    assert surface.observe_calls == 1  # only the post-click verification mints a new ID
    assert surface.resolve_calls == 0
    assert surface.execute_calls == surface.side_effects == 1
    assert manager.get_calls_inside_actor == [True]


def test_discovery_dispatch_rejects_stale_stored_observation_before_click(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface()
        manager = FakeManager(actor)
        decision = ClickDecision(
            observation_id="expired_observation",
            reason_code="MODEL_PROPOSED",
            rationale="This stale reference must not resolve.",
            operation="CLICK",
            control_ref="c_1",
        )
        result = await ExecutionGateway(
            manager, surface, PolicyEngine(), sink
        ).dispatch_observed(_context(run_alias, actor), decision)
        return result, surface

    result, surface = asyncio.run(scenario())
    assert result.effect_state is EffectState.NOT_DISPATCHED
    assert result.reason_code == "STALE_OBSERVATION"
    assert surface.execute_calls == surface.side_effects == 0


def test_discovery_dispatch_rejects_model_non_click_before_surface(tmp_path):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        surface = FakeSurface()
        decision = WaitDecision(
            observation_id="obs_before",
            reason_code="MODEL_PROPOSED",
            rationale="Wait is not supported in this discovery action slice.",
            operation="WAIT",
            timeout_ms=10,
        )
        result = await ExecutionGateway(
            FakeManager(actor), surface, PolicyEngine(), sink
        ).dispatch_observed(_context(run_alias, actor), decision)
        return result, surface

    result, surface = asyncio.run(scenario())
    assert result.effect_state is EffectState.NOT_DISPATCHED
    assert result.reason_code == "ACTION_NOT_SUPPORTED"
    assert surface.observe_calls == surface.resolve_calls == surface.execute_calls == 0


@pytest.mark.parametrize(
    ("identity_match", "expected_reason"),
    [(False, "SUBJECT_MISMATCH"), (None, "DETAIL_NOT_READY")],
)
def test_extract_refuses_detail_fields_without_verified_account_identity(
    tmp_path, identity_match, expected_reason
):
    async def scenario():
        actor = SessionActor()
        sink, run_alias = _evidence(tmp_path)
        await actor.begin_run(run_alias)
        observation, view = _page("obs_detail", "DETAIL_READY", "account_details")
        view = FakeView(
            **{
                **view.__dict__,
                "detail_identity_match": identity_match,
                "field_values": {"PROFILE_AVAILABLE_BALANCE": (SecretStr("$0.00"),)},
                "field_relations": {"PROFILE_AVAILABLE_BALANCE": True},
            }
        )
        surface = FakeSurface(views=((observation, view),))
        context = _context(run_alias, actor).model_copy(
            update={"membership_proof": _membership_proof(run_alias)}
        )
        result = await ExecutionGateway(
            FakeManager(actor), surface, PolicyEngine(), sink
        ).dispatch(context, _extract_bundle(), "read_balance")
        return result, surface

    result, surface = asyncio.run(scenario())
    assert result.effect_state is EffectState.NOT_DISPATCHED
    assert result.reason_code == expected_reason
    assert result.value is None
    assert surface.resolve_calls == 0
    assert surface.execute_calls == surface.side_effects == 0
