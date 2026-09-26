from __future__ import annotations

import ast
import asyncio
import inspect
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
import time

from pydantic import SecretStr
import pytest

import cua.llm.decisions as llm_decisions
from cua.llm.decisions import OpenAIResponsesDecisionBackend
from cua.evidence import EvidenceSink, RunMetadata, RunMode
from cua.evidence.models import SafeReasonCode
from cua.execution import ExecutionContext, ExecutionGateway, InvocationStatus
from cua.handoff.contracts import (
    HandoffResult,
    HandoffState,
    ReconciliationContext,
    ReconciliationDisposition,
)
from cua.models.bundles import (
    BundleReference,
    BundleStep,
    CapabilityBundle,
    CapabilityContract,
    CapabilityIdentity,
    Compatibility,
    InputContract,
    OutputContract,
    ParserPin,
    RecoveryDefinition,
    StepSource,
    TargetDefinition,
    TraceProvenance,
)
from cua.models.verification import MembershipProof
from cua.models.conditions import (
    AccountPresentCondition,
    AllCondition,
    OverviewCompleteCondition,
    PrincipalMatchesCondition,
)
from cua.models.observations import Observation, ObservedControl
from cua.policy import PolicyContext, PolicyEngine
from cua.registry import BundleRegistry, ImmutableRevisionError
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from cua.replay.runtime import (
    HandoffRequest,
    ReplayResumeContext,
    ReplayRuntime,
    _apply_resume_context,
    _supported_runtime_contract,
)
from cua.sessions import SessionActor, SessionHandle, ValidationSessionBinding
from cua.surface import NormalizedView, ResolvedTarget
from cua.verification import CompletionVerifier


RUN = "run_0123456789abcdef"
SESSION = "s_0123456789abcdef"
ORIGIN = "http://127.0.0.1:8080"
PROFILE = "parabank-native-v1"
ACCOUNT = "100001"
TARGET_REVISION = "ee82474be5f58bea3ddc8be0fd831072b00201cb"


def test_runtime_contract_allows_access_denied_and_keeps_version_pin():
    access_denied = _bundle(business_outcomes=("ACCESS_DENIED",))
    revised = _bundle().model_copy(
        update={"capability": CapabilityIdentity(name="get_savings_balance", version="1.0.1")}
    )
    unsupported_outcome = _bundle(business_outcomes=("APP_ERROR",))

    assert _supported_runtime_contract(access_denied)
    assert not _supported_runtime_contract(revised)
    assert not _supported_runtime_contract(unsupported_outcome)


def _policy_context() -> PolicyContext:
    return PolicyContext(
        deployment_origins=(ORIGIN,),
        capability_origins=(ORIGIN,),
        run_origins=(ORIGIN,),
        deployment_routes=("home", "accounts_overview", "account_details"),
        capability_routes=("home", "accounts_overview", "account_details"),
        run_routes=("home", "accounts_overview", "account_details"),
        deployment_operations=("CLICK", "READ"),
        capability_operations=("CLICK", "READ"),
        run_operations=("CLICK", "READ"),
    )


def _context(run_alias: str = RUN) -> ExecutionContext:
    return ExecutionContext(
        run_alias=run_alias,
        session_id=SESSION,
        expected_epoch=0,
        authentication_generation=4,
        target_origin=ORIGIN,
        profile_id=PROFILE,
        input_bindings={"inputs.account_id": SecretStr(ACCOUNT)},
        policy_context=_policy_context(),
    )


def _contract(*, business_outcomes=(), input_pattern=r"^[0-9]+$"):
    return CapabilityContract(
        inputs=(
            InputContract(
                name="account_id",
                value_type="string",
                pattern=input_pattern,
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
        business_outcomes=business_outcomes,
    )


def _bundle(*, business_outcomes=(), input_pattern=r"^[0-9]+$") -> CapabilityBundle:
    fingerprint = current_runtime_fingerprint()
    return CapabilityBundle(
        schema_version="1",
        capability=CapabilityIdentity(name="get_savings_balance", version="1.0.0"),
        compatibility=Compatibility(
            profile=PROFILE,
            runtime_contract="cua-v1",
            profile_sha256=fingerprint.profile_sha256,
            condition_runtime_sha256=fingerprint.condition_sha256,
        ),
        contract=_contract(
            business_outcomes=business_outcomes,
            input_pattern=input_pattern,
        ),
        steps=(
            BundleStep(
                id="verify_membership",
                kind="ASSERT",
                preconditions=(
                    AllCondition(
                        kind="all",
                        conditions=(
                            PrincipalMatchesCondition(kind="principal_matches"),
                            OverviewCompleteCondition(kind="overview_complete"),
                            AccountPresentCondition(
                                kind="account_present", input_ref="inputs.account_id"
                            ),
                        ),
                    ),
                ),
                source=StepSource(type="declared"),
            ),
            BundleStep(
                id="verify_completion",
                kind="VERIFY",
                source=StepSource(type="declared"),
            ),
        ),
        provenance=TraceProvenance(
            trace_id="trace_synthetic_test_only",
            verified=True,
            completion_proof_id="proof_synthetic_test_only",
        ),
    )


def _action_bundle(*, business_outcomes=(), recovery: bool = False) -> CapabilityBundle:
    fingerprint = current_runtime_fingerprint()
    click_step = BundleStep(
        id="open_account",
        kind="CLICK",
        target_ref="requested_account",
        recovery_ref="account_recovery" if recovery else None,
        source=StepSource(type="observed", event_ids=("e_click",)),
    )
    steps = [click_step]
    if recovery:
        steps.insert(
            0,
            BundleStep(
                id="review_overview_anchor",
                kind="CLICK",
                target_ref="overview_anchor",
                source=StepSource(type="reviewer_added", reviewer_ref="review.anchor"),
            ),
        )
        steps.insert(
            1,
            BundleStep(
                id="verify_membership",
                kind="ASSERT",
                preconditions=(
                    AllCondition(
                        kind="all",
                        conditions=(
                            PrincipalMatchesCondition(kind="principal_matches"),
                            OverviewCompleteCondition(kind="overview_complete"),
                            AccountPresentCondition(
                                kind="account_present", input_ref="inputs.account_id"
                            ),
                        ),
                    ),
                ),
                source=StepSource(type="declared"),
            ),
        )
    steps.extend(
        (
            BundleStep(
                id="extract_balance",
                kind="EXTRACT",
                target_ref="available_balance",
                output_ref="available_balance",
                parser_id="USD_DECIMAL_V1",
                parser_version="1",
                source=StepSource(type="observed", event_ids=("e_extract",)),
            ),
            BundleStep(
                id="verify_completion",
                kind="VERIFY",
                source=StepSource(type="declared"),
            ),
        )
    )
    targets = [
        TargetDefinition(
            ref="requested_account",
            locator="TABLE_ACCOUNT_LINK_BY_INPUT",
            binding_ref="inputs.account_id",
            allowed_operations=("CLICK",),
        ),
        TargetDefinition(
            ref="available_balance",
            locator="PROFILE_AVAILABLE_BALANCE",
            allowed_operations=("READ",),
        ),
    ]
    recoveries = ()
    if recovery:
        targets.append(
            TargetDefinition(
                ref="overview_anchor",
                locator="ROLE_LINK_ACCOUNTS_OVERVIEW",
                allowed_operations=("CLICK",),
            )
        )
        recoveries = (
            RecoveryDefinition(
                ref="account_recovery",
                anchor_target_ref="overview_anchor",
                max_attempts=2,
            ),
        )
    return CapabilityBundle(
        schema_version="1",
        capability=CapabilityIdentity(name="get_savings_balance", version="1.0.0"),
        compatibility=Compatibility(
            profile=PROFILE,
            runtime_contract="cua-v1",
            profile_sha256=fingerprint.profile_sha256,
            condition_runtime_sha256=fingerprint.condition_sha256,
        ),
        contract=_contract(business_outcomes=business_outcomes),
        steps=tuple(steps),
        targets=tuple(targets),
        recoveries=recoveries,
        parsers=(
            ParserPin(
                parser_id="USD_DECIMAL_V1",
                version="1",
                implementation_sha256=fingerprint.parser_sha256,
            ),
        ),
        provenance=TraceProvenance(
            trace_id="trace_synthetic_test_only",
            verified=True,
            completion_proof_id="proof_synthetic_test_only",
        ),
    )


def _pair(observation_id: str, *, overview: bool) -> tuple[Observation, NormalizedView]:
    route = "accounts_overview" if overview else "account_details"
    state = "OVERVIEW_READY" if overview else "DETAIL_READY"
    observation = Observation(
        id=observation_id,
        session_id=SESSION,
        page_id="p_0123456789ab",
        document_generation=1,
        frame_generations={"f_main": 1},
        captured_monotonic_ms=100,
        safe_route=route,
        state_tags=(state,),
        controls=(),
        safe_text=(),
        fingerprint="fingerprint_012345",
    )
    fields = {} if overview else {
        "PROFILE_ACCOUNT_NUMBER": (SecretStr(ACCOUNT),),
        "PROFILE_ACCOUNT_TYPE": (SecretStr("SAVINGS"),),
        "PROFILE_AVAILABLE_BALANCE": (SecretStr("$321.45"),),
    }
    relations = {} if overview else {
        "PROFILE_ACCOUNT_NUMBER": True,
        "PROFILE_ACCOUNT_TYPE": True,
        "PROFILE_AVAILABLE_BALANCE": True,
    }
    view = NormalizedView(
        observation_id=observation_id,
        session_id=SESSION,
        authentication_generation=4,
        profile_id=PROFILE,
        origin=ORIGIN,
        safe_route=route,
        page_state=state,
        principal_matches=True,
        overview_complete=True if overview else None,
        membership_validity={"inputs.account_id": True} if overview else {},
        account_ids=frozenset({ACCOUNT}) if overview else frozenset(),
        field_values=fields,
        field_relations=relations,
        parseable_fields=frozenset() if overview else frozenset({"PROFILE_AVAILABLE_BALANCE"}),
        detail_identity_match=None if overview else True,
        captured_monotonic_ms=100,
    )
    return observation, view


class _FakeRegistry:
    def __init__(self, bundle: CapabilityBundle, *, reject: bool = False) -> None:
        self.bundle = bundle
        self.reject = reject
        self.calls = 0

    def prepare_execution(self, reference, **qualification):
        self.calls += 1
        if self.reject:
            raise ImmutableRevisionError("not approved")
        assert reference.digest == "a" * 64
        assert qualification["browser_version"] == "1.60.0"
        assert qualification["target_revision"] == TARGET_REVISION
        return self.bundle


class _FakeSessions:
    def __init__(self, actor: SessionActor, *, validation_enabled: bool = False) -> None:
        self.actor = actor
        self.handle = SessionHandle(
            session_id=SESSION,
            page_id="p_0123456789ab",
            principal_alias="synthetic_alpha",
            profile_id=PROFILE,
            origin=ORIGIN,
            browser_version="1.60.0",
            auth_generation=4,
        )
        self.get_calls = 0
        self._validation_enabled = validation_enabled
        self._validation_binding = None
        self._validation_consumed = False

    def issue_validation_binding(self):
        if not self._validation_enabled:
            return None
        binding = ValidationSessionBinding(
            session_id=SESSION,
            principal_alias=self.handle.principal_alias,
            authentication_generation=self.handle.auth_generation,
            _token="unit-test-binding-token",
        )
        self._validation_binding = binding
        return binding

    def consume_validation_session_binding(self, binding, session_id):
        if (
            not isinstance(binding, ValidationSessionBinding)
            or not self._validation_enabled
            or self._validation_consumed
            or session_id != SESSION
            or binding != self._validation_binding
            or binding.principal_alias != self.handle.principal_alias
            or binding.authentication_generation != self.handle.auth_generation
        ):
            return False
        self._validation_consumed = True
        return True

    async def get_state(self, session_id):
        assert session_id == SESSION
        return SimpleNamespace(
            session_id=SESSION,
            principal_alias="synthetic_alpha",
            state="ACTIVE",
            auth_generation=4,
            profile_id=PROFILE,
            origin=ORIGIN,
            browser_version="1.60.0",
            actor=self.actor,
        )

    async def get(self, session_id):
        assert session_id == SESSION
        assert self.actor._command_lock.locked()
        self.get_calls += 1
        return SimpleNamespace(
            handle=self.handle,
            actor=self.actor,
            state="ACTIVE",
        )


class _FakeSurface:
    def __init__(self) -> None:
        self.views = [
            _pair("obs_overview_1", overview=True),
            _pair("obs_details_1", overview=False),
        ]
        self.observe_calls = 0

    async def observe(self, session_id, *, bindings=None):
        assert session_id == SESSION
        value = self.views[min(self.observe_calls, len(self.views) - 1)]
        self.observe_calls += 1
        return value


class _ActionSurface:
    def __init__(
        self,
        actor: SessionActor,
        *,
        account_ids=(ACCOUNT,),
        account_type="SAVINGS",
        lose_receipt=False,
        no_effect_account_clicks=0,
        lose_receipt_account_clicks=0,
        delay_seconds=0.0,
        transient_observations=0,
        overview_complete=True,
    ) -> None:
        self.actor = actor
        self.account_ids = frozenset(account_ids)
        self.account_type = account_type
        self.lose_receipt = lose_receipt
        self.no_effect_account_clicks = no_effect_account_clicks
        self.lose_receipt_account_clicks = lose_receipt_account_clicks
        self.delay_seconds = delay_seconds
        self.transient_observations = transient_observations
        self.overview_complete = overview_complete
        self.observe_calls = 0
        self.side_effects = 0
        self.account_clicks = 0
        self.current_details = False
        self._pair_id = 0

    async def observe(self, session_id, *, bindings=None):
        assert self.actor._command_lock.locked()
        self.observe_calls += 1
        self._pair_id += 1
        details = self.current_details and self.transient_observations == 0
        if self.current_details and self.transient_observations > 0:
            self.transient_observations -= 1
        return _action_pair(
            f"obs_action_{self._pair_id}",
            details=details,
            account_ids=self.account_ids,
            account_type=self.account_type,
            overview_complete=self.overview_complete,
        )

    async def resolve_target(self, session_id, observation_id, target, bindings):
        assert self.actor._command_lock.locked()
        click = target.locator in {
            "TABLE_ACCOUNT_LINK_BY_INPUT",
            "ROLE_LINK_ACCOUNTS_OVERVIEW",
        }
        return ResolvedTarget(
            target_ref=target.ref,
            locator_key=target.locator,
            allowed_operations=tuple(target.allowed_operations),
            binding_ref=target.binding_ref,
            observation_id=observation_id,
            control_ref=(
                "c_overview"
                if target.locator == "ROLE_LINK_ACCOUNTS_OVERVIEW"
                else "c_account"
                if click
                else None
            ),
            frame_ref="f_main",
            page_id="p_0123456789ab",
            document_generation=1,
            authentication_generation=4,
            session_id=session_id,
            destination_origin=ORIGIN,
            destination_route=(
                "account_details"
                if target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT"
                else "accounts_overview"
                if target.locator == "ROLE_LINK_ACCOUNTS_OVERVIEW"
                else "account_details"
            ),
            runtime_risk="READ_ONLY",
            unique=True,
            visible=True,
            enabled=True,
            token="opaque_test_token",
            binding_value=bindings.get("inputs.account_id") if target.binding_ref else None,
        )

    async def execute(self, session_id, decision, resolved):
        assert self.actor._command_lock.locked()
        self.side_effects += 1
        if resolved.locator_key == "TABLE_ACCOUNT_LINK_BY_INPUT":
            self.account_clicks += 1
            if self.account_clicks > self.no_effect_account_clicks:
                self.current_details = True
            lose_receipt = self.account_clicks <= self.lose_receipt_account_clicks
        elif resolved.locator_key == "ROLE_LINK_ACCOUNTS_OVERVIEW":
            self.current_details = False
            lose_receipt = self.lose_receipt
        else:
            lose_receipt = False
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if lose_receipt or self.lose_receipt:
            raise RuntimeError("test receipt was lost")


def _action_pair(
    observation_id: str,
    *,
    details: bool,
    account_ids=(ACCOUNT,),
    account_type="SAVINGS",
    overview_complete=True,
):
    route = "account_details" if details else "accounts_overview"
    state = "DETAIL_READY" if details else "OVERVIEW_READY"
    controls = () if details else (
        ObservedControl(
            ref="c_account",
            frame_ref="f_main",
            role="link",
            safe_name="<requested_account>",
            enabled=True,
            visible=True,
            allowed_operations=("CLICK",),
            binding_ref="inputs.account_id",
        ),
        ObservedControl(
            ref="c_overview",
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
        session_id=SESSION,
        page_id="p_0123456789ab",
        document_generation=1,
        frame_generations={"f_main": 1},
        captured_monotonic_ms=100,
        safe_route=route,
        state_tags=(state,),
        controls=controls,
        safe_text=(),
        fingerprint="fingerprint_012345",
    )
    fields = {} if not details else {
        "PROFILE_ACCOUNT_NUMBER": (SecretStr(ACCOUNT),),
        "PROFILE_ACCOUNT_TYPE": (SecretStr(account_type),),
        "PROFILE_AVAILABLE_BALANCE": (SecretStr("$321.45"),),
    }
    relations = {} if not details else {
        "PROFILE_ACCOUNT_NUMBER": True,
        "PROFILE_ACCOUNT_TYPE": True,
        "PROFILE_AVAILABLE_BALANCE": True,
    }
    view = NormalizedView(
        observation_id=observation_id,
        session_id=SESSION,
        authentication_generation=4,
        profile_id=PROFILE,
        origin=ORIGIN,
        safe_route=route,
        page_state=state,
        principal_matches=True,
        overview_complete=None if details else overview_complete,
        membership_validity={} if details else {"inputs.account_id": True},
        account_ids=frozenset() if details else frozenset(account_ids),
        field_values=fields,
        field_relations=relations,
        parseable_fields=frozenset() if not details else frozenset({"PROFILE_AVAILABLE_BALANCE"}),
        detail_identity_match=True if details else None,
        captured_monotonic_ms=100,
    )
    return observation, view


def _sink(tmp_path) -> tuple[EvidenceSink, str]:
    sink = EvidenceSink(tmp_path / "evidence")
    run_alias = sink.register_run(
        RunMetadata(
            mode=RunMode.REPLAY,
            capability_name="get_savings_balance",
            capability_version="1.0.0",
            bundle_digest="a" * 64,
            profile_id=PROFILE,
            target_revision=TARGET_REVISION,
            browser_version="1.60.0",
        )
    )
    return sink, run_alias


def _runtime(
    tmp_path,
    actor,
    *,
    bundle=None,
    reject=False,
    surface=None,
    run_timeout_seconds=180.0,
    handoff_notifier=None,
):
    sink, run_alias = _sink(tmp_path)
    sessions = _FakeSessions(actor)
    surface = surface or _FakeSurface()
    gateway = ExecutionGateway(sessions, surface, PolicyEngine(), sink)
    runtime = ReplayRuntime(
        _FakeRegistry(bundle or _bundle(), reject=reject),
        sessions,
        surface,
        gateway,
        CompletionVerifier(),
        sink,
        target_revision=TARGET_REVISION,
        run_timeout_seconds=run_timeout_seconds,
        handoff_notifier=handoff_notifier,
    )
    return runtime, sessions, surface, run_alias


def _validation_runtime(tmp_path, *, validation_enabled=True):
    actor = SessionActor()
    registry = BundleRegistry(tmp_path / "bundles")
    reference = registry.put_draft(_action_bundle())
    sink, run_alias = _sink(tmp_path)
    sessions = _FakeSessions(actor, validation_enabled=validation_enabled)
    surface = _ActionSurface(actor)
    gateway = ExecutionGateway(sessions, surface, PolicyEngine(), sink)
    runtime = ReplayRuntime(
        registry,
        sessions,
        surface,
        gateway,
        CompletionVerifier(),
        sink,
        target_revision=TARGET_REVISION,
    )
    return runtime, sessions, surface, actor, run_alias, reference


async def _run_action_case(
    tmp_path,
    *,
    account_ids=(ACCOUNT,),
    account_type="SAVINGS",
    lose_receipt=False,
    no_effect_account_clicks=0,
    lose_receipt_account_clicks=0,
    transient_observations=0,
    delay_seconds=0.0,
    run_timeout_seconds=180.0,
    recovery=False,
    business_outcomes=(),
    overview_complete=True,
):
    actor = SessionActor()
    surface = _ActionSurface(
        actor,
        account_ids=account_ids,
        account_type=account_type,
        lose_receipt=lose_receipt,
        no_effect_account_clicks=no_effect_account_clicks,
        lose_receipt_account_clicks=lose_receipt_account_clicks,
        transient_observations=transient_observations,
        delay_seconds=delay_seconds,
        overview_complete=overview_complete,
    )
    runtime, sessions, surface, run_alias = _runtime(
        tmp_path,
        actor,
        bundle=_action_bundle(business_outcomes=business_outcomes, recovery=recovery),
        surface=surface,
        run_timeout_seconds=run_timeout_seconds,
    )
    await actor.begin_run(run_alias)
    reference = BundleReference(name="get_savings_balance", version="1.0.0", digest="a" * 64)
    result = await runtime.run(reference, _context(run_alias))
    return result, sessions, surface


def test_synthetic_bundle_replay_returns_outputs_only_after_membership_and_final_verify(tmp_path):
    async def scenario():
        actor = SessionActor()
        runtime, sessions, surface, run_alias = _runtime(tmp_path, actor)
        await actor.begin_run(run_alias)
        reference = BundleReference(
            name="get_savings_balance",
            version="1.0.0",
            digest="a" * 64,
        )

        result = await runtime.run(reference, _context(run_alias))

        assert result.status is InvocationStatus.SUCCESS, (result.failure, sessions.get_calls, surface.observe_calls)
        assert result.outputs["available_balance"].get_secret_value() == "321.45"
        assert result.outputs["currency"].get_secret_value() == "USD"
        assert surface.observe_calls == 2
        assert sessions.get_calls == 3

    asyncio.run(scenario())


def test_unapproved_bundle_is_rejected_before_ui_authentication_read(tmp_path):
    async def scenario():
        actor = SessionActor()
        runtime, sessions, surface, run_alias = _runtime(tmp_path, actor, reject=True)
        await actor.begin_run(run_alias)
        reference = BundleReference(
            name="get_savings_balance",
            version="1.0.0",
            digest="a" * 64,
        )

        result = await runtime.run(reference, _context(run_alias))

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.INVALID_BUNDLE
        assert sessions.get_calls == 0
        assert surface.observe_calls == 0

    asyncio.run(scenario())


def test_ordinary_replay_rejects_real_draft_before_ui_access(tmp_path):
    async def scenario():
        runtime, sessions, surface, actor, run_alias, reference = _validation_runtime(tmp_path)
        await actor.begin_run(run_alias)

        result = await runtime.run(reference, _context(run_alias))

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.INVALID_BUNDLE
        assert sessions.get_calls == 0
        assert surface.observe_calls == 0
        assert surface.side_effects == 0

    asyncio.run(scenario())


def test_designated_draft_validation_uses_the_normal_gateway_and_verifier_path(tmp_path):
    async def scenario():
        runtime, sessions, surface, actor, run_alias, reference = _validation_runtime(
            tmp_path,
            validation_enabled=True,
        )
        binding = sessions.issue_validation_binding()
        await actor.begin_run(run_alias)

        result = await runtime.run_validation(reference, _context(run_alias), binding)

        assert result.status is InvocationStatus.SUCCESS, result.failure
        assert result.outputs["available_balance"].get_secret_value() == "321.45"
        assert result.outputs["currency"].get_secret_value() == "USD"
        assert surface.account_clicks == 1
        assert surface.side_effects == 1
        assert sessions.get_calls >= 3
        assert sessions._validation_consumed is True

    asyncio.run(scenario())


@pytest.mark.parametrize("binding_kind", ("wrong_session", "non_test_session"))
def test_invalid_validation_binding_is_rejected_before_ui_access(tmp_path, binding_kind):
    async def scenario():
        runtime, sessions, surface, actor, run_alias, reference = _validation_runtime(
            tmp_path,
            validation_enabled=(binding_kind == "wrong_session"),
        )
        if binding_kind == "wrong_session":
            valid = sessions.issue_validation_binding()
            binding = replace(valid, session_id="s_another_test_session")
        else:
            binding = ValidationSessionBinding(
                session_id=SESSION,
                principal_alias="synthetic_alpha",
                authentication_generation=4,
                _token="unissued-for-non-test-session",
            )
        await actor.begin_run(run_alias)

        result = await runtime.run_validation(reference, _context(run_alias), binding)

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.INVALID_BUNDLE
        assert sessions.get_calls == 0
        assert surface.observe_calls == 0
        assert surface.side_effects == 0

    asyncio.run(scenario())


def test_reused_validation_binding_is_rejected_before_second_ui_access(tmp_path):
    async def scenario():
        runtime, sessions, surface, actor, run_alias, reference = _validation_runtime(
            tmp_path,
            validation_enabled=True,
        )
        binding = sessions.issue_validation_binding()
        await actor.begin_run(run_alias)

        first = await runtime.run_validation(reference, _context(run_alias), binding)
        before = (sessions.get_calls, surface.observe_calls, surface.side_effects)
        second = await runtime.run_validation(reference, _context(run_alias), binding)

        assert first.status is InvocationStatus.SUCCESS
        assert second.status is InvocationStatus.FAILURE
        assert second.failure.reason_code is SafeReasonCode.INVALID_BUNDLE
        assert (sessions.get_calls, surface.observe_calls, surface.side_effects) == before

    asyncio.run(scenario())


def test_hostile_contract_regex_is_rejected_without_evaluation_or_ui_access(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        actor = SessionActor()
        bundle = _bundle(input_pattern=r"^(a+)+$")
        runtime, sessions, surface, run_alias = _runtime(tmp_path, actor, bundle=bundle)
        await actor.begin_run(run_alias)
        import cua.replay.runtime as replay_module

        original_fullmatch = replay_module.re.fullmatch

        def guard(pattern, string, flags=0):
            if pattern == r"^(a+)+$":
                pytest.fail("artifact-controlled regular expression was evaluated")
            return original_fullmatch(pattern, string, flags)

        monkeypatch.setattr(replay_module.re, "fullmatch", guard)
        reference = BundleReference(
            name="get_savings_balance",
            version="1.0.0",
            digest="a" * 64,
        )

        result = await runtime.run(reference, _context(run_alias))

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.INVALID_BUNDLE
        assert sessions.get_calls == 0
        assert surface.observe_calls == 0

    asyncio.run(scenario())


def test_unimplemented_browser_operation_is_rejected_before_ui_access(tmp_path):
    async def scenario():
        actor = SessionActor()
        base = _action_bundle()
        data = base.model_dump(mode="python")
        data["targets"] = (
            *data["targets"],
            TargetDefinition(
                ref="unsupported_text_target",
                locator="ROLE_LINK_ACCOUNTS_OVERVIEW",
                allowed_operations=("TYPE_TEXT",),
            ),
        )
        data["steps"] = (
            *data["steps"][:-1],
            BundleStep(
                id="unsupported_type",
                kind="TYPE_TEXT",
                target_ref="unsupported_text_target",
                input_ref="inputs.account_id",
                source=StepSource(type="declared"),
            ),
            data["steps"][-1],
        )
        bundle = CapabilityBundle.model_validate(data)
        runtime, sessions, surface, run_alias = _runtime(tmp_path, actor, bundle=bundle)
        await actor.begin_run(run_alias)
        reference = BundleReference(
            name="get_savings_balance",
            version="1.0.0",
            digest="a" * 64,
        )

        result = await runtime.run(reference, _context(run_alias))

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.ACTION_NOT_SUPPORTED
        assert sessions.get_calls == 0
        assert surface.observe_calls == 0

    asyncio.run(scenario())


def test_replay_dispatches_account_click_and_extracts_through_the_gateway(tmp_path):
    async def scenario():
        result, sessions, surface = await _run_action_case(tmp_path)

        assert result.status is InvocationStatus.SUCCESS
        assert result.outputs["available_balance"].get_secret_value() == "321.45"
        assert result.outputs["currency"].get_secret_value() == "USD"
        assert surface.account_clicks == 1
        assert surface.side_effects == 1
        assert sessions.get_calls >= 3

    asyncio.run(scenario())


def test_replay_reports_account_not_found_only_from_a_complete_overview(tmp_path):
    async def scenario():
        result, _, surface = await _run_action_case(
            tmp_path,
            account_ids=(),
            business_outcomes=("ACCOUNT_NOT_FOUND",),
        )

        assert result.status is InvocationStatus.BUSINESS_OUTCOME
        assert result.code is SafeReasonCode.ACCOUNT_NOT_FOUND
        assert result.outputs is None
        assert surface.side_effects == 0

    asyncio.run(scenario())


def test_membership_assert_reports_account_not_found_as_business_outcome(tmp_path):
    # Mirrors the compiled artifact: the declared membership ASSERT runs before
    # the account click, so absence must surface there, not as a hard failure.
    async def scenario():
        result, _, surface = await _run_action_case(
            tmp_path,
            account_ids=(),
            recovery=True,
            business_outcomes=("ACCOUNT_NOT_FOUND",),
        )

        assert result.status is InvocationStatus.BUSINESS_OUTCOME
        assert result.code is SafeReasonCode.ACCOUNT_NOT_FOUND
        assert result.outputs is None
        assert surface.account_clicks == 0

    asyncio.run(scenario())


def test_incomplete_overview_never_becomes_account_not_found(tmp_path):
    async def scenario():
        result, _, surface = await _run_action_case(
            tmp_path,
            account_ids=(),
            overview_complete=False,
            business_outcomes=("ACCOUNT_NOT_FOUND",),
            run_timeout_seconds=0.02,
        )

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.PRECONDITION_UNKNOWN
        assert surface.side_effects == 0

    asyncio.run(scenario())


def test_final_verifier_rejects_wrong_account_type_without_outputs(tmp_path):
    async def scenario():
        result, _, surface = await _run_action_case(tmp_path, account_type="CHECKING")

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.ACCOUNT_TYPE_MISMATCH
        assert result.outputs is None
        assert surface.account_clicks == 1

    asyncio.run(scenario())


def test_click_waits_through_transient_page_state_without_redispatch(tmp_path):
    async def scenario():
        result, _, surface = await _run_action_case(tmp_path, transient_observations=2)

        assert result.status is InvocationStatus.SUCCESS
        assert result.outputs["currency"].get_secret_value() == "USD"
        assert surface.account_clicks == 1
        assert surface.observe_calls >= 5

    asyncio.run(scenario())


def test_lost_click_receipt_reconciles_visible_effect_without_duplicate(tmp_path):
    async def scenario():
        result, _, surface = await _run_action_case(tmp_path, lose_receipt=True)

        assert result.status is InvocationStatus.SUCCESS
        assert result.outputs["available_balance"].get_secret_value() == "321.45"
        assert surface.account_clicks == 1

    asyncio.run(scenario())


def test_expired_run_deadline_stops_after_one_unknown_click(tmp_path):
    async def scenario():
        result, _, surface = await _run_action_case(
            tmp_path,
            delay_seconds=0.08,
            run_timeout_seconds=0.02,
        )

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.RECOVERY_EXHAUSTED
        assert result.outputs is None
        assert surface.account_clicks == 1

    asyncio.run(scenario())


def test_effect_aware_recovery_retries_at_most_two_times_with_approved_anchor(tmp_path):
    async def scenario():
        result, _, surface = await _run_action_case(
            tmp_path,
            lose_receipt_account_clicks=3,
            no_effect_account_clicks=3,
            recovery=True,
        )

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.RECOVERY_EXHAUSTED
        assert surface.account_clicks == 3
        assert surface.side_effects == 6  # Initial anchor plus two approved recovery anchors.

    asyncio.run(scenario())


def test_effect_aware_recovery_retries_only_after_fresh_no_effect_proof(tmp_path):
    async def scenario():
        result, _, surface = await _run_action_case(
            tmp_path,
            lose_receipt_account_clicks=1,
            no_effect_account_clicks=1,
            recovery=True,
        )

        assert result.status is InvocationStatus.SUCCESS
        assert result.outputs["available_balance"].get_secret_value() == "321.45"
        assert surface.account_clicks == 2
        assert surface.side_effects == 4

    asyncio.run(scenario())


def test_replay_has_no_model_backend_import_or_constructor_parameter():
    source_path = Path(inspect.getfile(ReplayRuntime))
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(module and module.startswith(("cua.llm", "cua.discovery")) for module in imported)
    assert "backend" not in inspect.signature(ReplayRuntime).parameters


def test_normal_and_recovery_replay_never_read_provider_key_or_call_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []

    async def provider_trap(*args, **kwargs):
        calls.append("choose")
        raise AssertionError("replay attempted to call the model backend")

    def transport_trap(*args, **kwargs):
        calls.append("_post_json")
        raise AssertionError("replay attempted provider HTTP")

    monkeypatch.setattr(OpenAIResponsesDecisionBackend, "choose", provider_trap)
    monkeypatch.setattr(llm_decisions, "_post_json", transport_trap)

    async def scenario():
        normal, _, _ = await _run_action_case(tmp_path / "normal")
        recovery, _, _ = await _run_action_case(
            tmp_path / "recovery",
            recovery=True,
            no_effect_account_clicks=1,
            lose_receipt_account_clicks=1,
        )

        assert normal.status is InvocationStatus.SUCCESS
        assert recovery.status is InvocationStatus.SUCCESS
        assert calls == []

    asyncio.run(scenario())


class _FakeHandoffNotifier:
    """Private coordinator seam for replay protocol tests only."""

    def __init__(self, dispositions, *, on_request=None):
        self.dispositions = list(dispositions)
        self.on_request = on_request
        self.requests = []
        self.deadlines = []

    async def require_human(self, request, *, trusted):
        self.requests.append(request)
        self.deadlines.append(trusted.deadline_monotonic)
        if self.on_request is not None:
            self.on_request(request, trusted)
        disposition = self.dispositions.pop(0)
        return HandoffResult(
            intervention_id="iv_0123456789abcdef01234567",
            session_id=request.session_id,
            run_id=request.run_id,
            state=HandoffState.RUNNING,
            epoch=request.ownership_epoch,
            disposition=disposition,
        )


def test_handoff_next_advances_exact_failed_step_without_redispatch(tmp_path):
    async def scenario():
        actor = SessionActor()
        surface = _ActionSurface(actor, no_effect_account_clicks=1)

        def complete_manually(_request, _trusted):
            # Simulate a reviewed postcondition established by the operator.
            surface.current_details = True

        notifier = _FakeHandoffNotifier(
            [ReconciliationDisposition.NEXT],
            on_request=complete_manually,
        )
        runtime, sessions, surface, run_alias = _runtime(
            tmp_path,
            actor,
            bundle=_action_bundle(),
            surface=surface,
            handoff_notifier=notifier,
        )
        await actor.begin_run(run_alias)
        result = await runtime.run(
            BundleReference(name="get_savings_balance", version="1.0.0", digest="a" * 64),
            _context(run_alias),
        )

        assert result.status is InvocationStatus.SUCCESS
        assert notifier.requests[0].step_id == "open_account"
        assert surface.account_clicks == 1
        assert result.outputs["currency"].get_secret_value() == "USD"

    asyncio.run(scenario())


def test_unclassified_dialog_escalates_to_human_instead_of_failing_hard(tmp_path):
    class DialogSurface(_ActionSurface):
        blocked = True

        async def observe(self, session_id, *, bindings=None):
            observation, view = await super().observe(session_id, bindings=bindings)
            if not self.blocked:
                return observation, view
            return (
                observation.model_copy(update={"state_tags": ("UNKNOWN",)}),
                replace(view, page_state="UNKNOWN", unknown_blocker=True),
            )

    async def scenario():
        actor = SessionActor()
        surface = DialogSurface(actor)

        def dismiss_dialog(_request, _trusted):
            surface.blocked = False

        notifier = _FakeHandoffNotifier(
            [ReconciliationDisposition.RETRY_SAFE],
            on_request=dismiss_dialog,
        )
        runtime, _, surface, run_alias = _runtime(
            tmp_path,
            actor,
            bundle=_action_bundle(recovery=True),
            surface=surface,
            handoff_notifier=notifier,
        )
        await actor.begin_run(run_alias)
        result = await runtime.run(
            BundleReference(name="get_savings_balance", version="1.0.0", digest="a" * 64),
            _context(run_alias),
        )

        assert [request.reason_code for request in notifier.requests] == [
            SafeReasonCode.UNKNOWN_BLOCKER
        ]
        assert result.status is InvocationStatus.SUCCESS

    asyncio.run(scenario())


def test_handoff_retry_safe_retries_same_step_with_shared_budget_and_deadline(tmp_path):
    async def scenario():
        actor = SessionActor()
        surface = _ActionSurface(actor, no_effect_account_clicks=3)
        notifier = _FakeHandoffNotifier(
            [
                ReconciliationDisposition.RETRY_SAFE,
                ReconciliationDisposition.RETRY_SAFE,
            ]
        )
        runtime, _, surface, run_alias = _runtime(
            tmp_path,
            actor,
            bundle=_action_bundle(),
            surface=surface,
            run_timeout_seconds=10.0,
            handoff_notifier=notifier,
        )
        await actor.begin_run(run_alias)
        result = await runtime.run(
            BundleReference(name="get_savings_balance", version="1.0.0", digest="a" * 64),
            _context(run_alias),
        )

        assert result.status is InvocationStatus.FAILURE
        assert result.failure.reason_code is SafeReasonCode.RECOVERY_EXHAUSTED
        assert [request.step_id for request in notifier.requests] == [
            "open_account",
            "open_account",
        ]
        assert notifier.deadlines[0] == notifier.deadlines[1]
        assert surface.account_clicks == 2

    asyncio.run(scenario())


def test_handoff_aborted_maps_to_aborted_result(tmp_path):
    async def scenario():
        actor = SessionActor()
        surface = _ActionSurface(actor, no_effect_account_clicks=1)

        class AbortNotifier(_FakeHandoffNotifier):
            async def require_human(self, request, *, trusted):
                self.requests.append(request)
                self.deadlines.append(trusted.deadline_monotonic)
                return HandoffResult(
                    intervention_id="iv_0123456789abcdef01234567",
                    session_id=request.session_id,
                    run_id=request.run_id,
                    state=HandoffState.TIMED_OUT,
                    epoch=request.ownership_epoch,
                    reason=SafeReasonCode.UNKNOWN_BLOCKER,
                )

        notifier = AbortNotifier([])
        runtime, _, _, run_alias = _runtime(
            tmp_path,
            actor,
            bundle=_action_bundle(),
            surface=surface,
            handoff_notifier=notifier,
        )
        await actor.begin_run(run_alias)
        result = await runtime.run(
            BundleReference(name="get_savings_balance", version="1.0.0", digest="a" * 64),
            _context(run_alias),
        )

        assert result.status is InvocationStatus.ABORTED
        assert result.failure.reason_code is SafeReasonCode.UNKNOWN_BLOCKER

    asyncio.run(scenario())


def test_handoff_request_is_value_free_and_model_trap_stays_outside_replay(tmp_path):
    request = HandoffRequest(
        run_id=RUN,
        session_id=SESSION,
        step_id="open_account",
        reason_code=SafeReasonCode.UNKNOWN_BLOCKER,
        ownership_epoch=0,
    )
    assert {item.name for item in fields(HandoffRequest)} == {
        "run_id",
        "session_id",
        "step_id",
        "reason_code",
        "ownership_epoch",
    }
    assert ACCOUNT not in repr(request)
    assert not hasattr(request, "actor")
    assert not hasattr(request, "reconciler")


def _resume_result(context, *, epoch=3):
    return HandoffResult(
        intervention_id="iv_0123456789abcdef01234567",
        session_id=context.session_id,
        run_id=context.run_alias,
        state=HandoffState.RUNNING,
        epoch=epoch,
        disposition=ReconciliationDisposition.NEXT,
        context=ReplayResumeContext(context),
    )


def _resume_proof(*, run_alias=RUN, session_id=SESSION, generation=5, account=ACCOUNT):
    return MembershipProof(
        proof_ref="proof_resume",
        run_ref=run_alias,
        session_ref=session_id,
        authentication_generation=generation,
        account_binding_ref="inputs.account_id",
        account_binding_value=SecretStr(account),
        overview_observation_ref="overview_resume",
        overview_complete=True,
        account_present=True,
        verified_monotonic_ms=100,
    )


def test_valid_replay_resume_context_applies_fresh_auth_and_membership_proof():
    current = _context().model_copy(update={"deadline_monotonic": 90.0})
    updated = current.model_copy(
        update={
            "expected_epoch": 3,
            "authentication_generation": 5,
            "membership_proof": _resume_proof(),
        }
    )

    applied, reason = _apply_resume_context(current, _resume_result(updated))

    assert reason is None
    assert applied is updated
    assert applied.expected_epoch == 3
    assert applied.authentication_generation == 5
    assert applied.membership_proof is updated.membership_proof


@pytest.mark.parametrize(
    ("update", "expected_reason"),
    (
        ({"session_id": "s_forged"}, SafeReasonCode.SESSION_LOST),
        (
            {"input_bindings": {"inputs.account_id": SecretStr("999999")}},
            SafeReasonCode.INVALID_INPUT,
        ),
        ({"deadline_monotonic": 91.0}, SafeReasonCode.INVALID_INPUT),
    ),
)
def test_forged_replay_resume_envelope_is_rejected_without_applying(update, expected_reason):
    current = _context().model_copy(update={"deadline_monotonic": 90.0})
    updated = current.model_copy(update={"expected_epoch": 3, **update})

    applied, reason = _apply_resume_context(current, _resume_result(updated))

    assert applied is None
    assert reason is expected_reason
    assert current.expected_epoch == 0
    assert current.input_bindings["inputs.account_id"].get_secret_value() == ACCOUNT


def test_forged_membership_proof_is_rejected_without_retry_or_context_change():
    current = _context().model_copy(update={"deadline_monotonic": 90.0})
    updated = current.model_copy(
        update={
            "expected_epoch": 3,
            "authentication_generation": 5,
            "membership_proof": _resume_proof(account="999999"),
        }
    )

    applied, reason = _apply_resume_context(current, _resume_result(updated))

    assert applied is None
    assert reason is SafeReasonCode.MEMBERSHIP_PROOF_INVALID
    assert current.membership_proof is None


def test_replay_resume_context_and_handoff_result_repr_hide_protected_values():
    current = _context().model_copy(update={"deadline_monotonic": 90.0})
    updated = current.model_copy(
        update={
            "expected_epoch": 3,
            "authentication_generation": 5,
            "membership_proof": _resume_proof(),
        }
    )
    protected = ReplayResumeContext(updated)
    result = _resume_result(updated)

    assert "100001" not in repr(protected)
    assert "100001" not in repr(result)
    assert "protected" in repr(protected)
    assert "context" not in repr(result)


async def _enter_resuming(actor, run_alias):
    await actor.begin_run(run_alias)
    pausing = await actor.pause_and_drain(expected_epoch=0)
    waiting = await actor.wait_for_human(expected_epoch=pausing.epoch)
    claimed = await actor.claim_human(expected_epoch=waiting.epoch)
    return await actor.begin_resume(expected_epoch=claimed.epoch)


def _reconciliation_context(run_alias, actor, epoch):
    return ReconciliationContext(
        intervention_id="iv_0123456789abcdef01234567",
        session_id=SESSION,
        run_id=run_alias,
        epoch=epoch,
        actor=actor,
    )


def test_post_human_manual_completion_is_verified_next_without_nested_actor_submit(tmp_path):
    async def scenario():
        actor = SessionActor()
        runtime, sessions, surface, run_alias = _runtime(
            tmp_path,
            actor,
            bundle=_action_bundle(),
            surface=_ActionSurface(actor),
        )
        resuming = await _enter_resuming(actor, run_alias)
        surface.current_details = True
        proof = _resume_proof(generation=4)
        context = _context(run_alias).model_copy(
            update={"expected_epoch": resuming.epoch, "membership_proof": proof}
        )

        result = await asyncio.wait_for(
            runtime._reconcile_after_handoff(
                _reconciliation_context(run_alias, actor, resuming.epoch),
                context=context,
                bundle=_action_bundle(),
                step=_action_bundle().steps[0],
                membership_proof=proof,
                actor=actor,
                page_id="p_0123456789ab",
                deadline=time.monotonic() + 2,
            ),
            timeout=1,
        )

        assert result.disposition is ReconciliationDisposition.NEXT
        assert result.context.context.expected_epoch == resuming.epoch + 1
        assert result.context.context.membership_proof is proof
        assert actor.snapshot.owner == "RESUMING"

    asyncio.run(scenario())


def test_expired_same_principal_reauthenticates_in_place_and_rebuilds_membership(tmp_path):
    class ExpiredSessions(_FakeSessions):
        def __init__(self, actor):
            super().__init__(actor)
            self.state_name = "SESSION_EXPIRED"
            self.reauth_calls = 0

        async def get_state(self, session_id):
            state = await super().get_state(session_id)
            values = vars(state).copy()
            values.update(state=self.state_name, auth_generation=self.handle.auth_generation)
            return SimpleNamespace(**values)

        async def reauthenticate_existing(self, session_id, expected_epoch):
            assert self.actor.snapshot.owner == "RESUMING"
            assert expected_epoch == self.actor.snapshot.epoch
            self.reauth_calls += 1
            self.handle = replace(self.handle, auth_generation=5)
            self.state_name = "ACTIVE"
            return self.handle

    class AuthGenerationSurface(_ActionSurface):
        async def observe(self, session_id, *, bindings=None):
            observation, view = await super().observe(session_id, bindings=bindings)
            if view.safe_route == "account_details":
                overview_observation, _ = _action_pair(
                    observation.id,
                    details=False,
                )
                observation = observation.model_copy(
                    update={"controls": overview_observation.controls}
                )
            return observation, replace(view, authentication_generation=5)

        async def resolve_target(self, session_id, observation_id, target, bindings):
            resolved = await super().resolve_target(session_id, observation_id, target, bindings)
            return replace(resolved, authentication_generation=5)

    async def scenario():
        actor = SessionActor()
        sessions = ExpiredSessions(actor)
        surface = AuthGenerationSurface(actor)
        surface.current_details = True
        sink, run_alias = _sink(tmp_path)
        bundle = _action_bundle(recovery=True)
        gateway = ExecutionGateway(sessions, surface, PolicyEngine(), sink)
        runtime = ReplayRuntime(
            _FakeRegistry(bundle),
            sessions,
            surface,
            gateway,
            CompletionVerifier(),
            sink,
            target_revision=TARGET_REVISION,
        )
        resuming = await _enter_resuming(actor, run_alias)
        context = _context(run_alias).model_copy(update={"expected_epoch": resuming.epoch})

        result = await asyncio.wait_for(
            runtime._reconcile_after_handoff(
                _reconciliation_context(run_alias, actor, resuming.epoch),
                context=context,
                bundle=bundle,
                step=bundle.steps[2],
                membership_proof=None,
                actor=actor,
                page_id="p_0123456789ab",
                deadline=time.monotonic() + 3,
            ),
            timeout=2,
        )

        assert sessions.reauth_calls == 1
        assert result.disposition is ReconciliationDisposition.RETRY_SAFE
        resumed = result.context.context
        assert resumed.authentication_generation == 5
        assert resumed.membership_proof is not None
        assert resumed.membership_proof.authentication_generation == 5
        assert actor.snapshot.owner == "RESUMING"

    asyncio.run(scenario())


def test_wrong_principal_stays_paused_without_logout_or_login(tmp_path):
    class WrongPrincipalSessions(_FakeSessions):
        async def get_state(self, session_id):
            state = await super().get_state(session_id)
            values = vars(state).copy()
            values["state"] = "SUBJECT_MISMATCH"
            return SimpleNamespace(**values)

    async def scenario():
        actor = SessionActor()
        sessions = WrongPrincipalSessions(actor)
        surface = _ActionSurface(actor)
        sink, run_alias = _sink(tmp_path)
        bundle = _action_bundle()
        gateway = ExecutionGateway(sessions, surface, PolicyEngine(), sink)
        runtime = ReplayRuntime(
            _FakeRegistry(bundle),
            sessions,
            surface,
            gateway,
            CompletionVerifier(),
            sink,
            target_revision=TARGET_REVISION,
        )
        resuming = await _enter_resuming(actor, run_alias)
        result = await runtime._reconcile_after_handoff(
            _reconciliation_context(run_alias, actor, resuming.epoch),
            context=_context(run_alias).model_copy(update={"expected_epoch": resuming.epoch}),
            bundle=bundle,
            step=bundle.steps[0],
            membership_proof=None,
            actor=actor,
            page_id="p_0123456789ab",
            deadline=time.monotonic() + 2,
        )

        assert result.disposition is ReconciliationDisposition.REMAIN_PAUSED
        assert result.reason is SafeReasonCode.SUBJECT_MISMATCH
        assert actor.snapshot.owner == "RESUMING"

    asyncio.run(scenario())


def test_unknown_visible_state_stays_paused_without_dispatch(tmp_path):
    class UnknownSurface(_ActionSurface):
        async def observe(self, session_id, *, bindings=None):
            observation, view = await super().observe(session_id, bindings=bindings)
            return (
                observation.model_copy(update={"state_tags": ("UNKNOWN",)}),
                replace(view, page_state="UNKNOWN", unknown_blocker=True),
            )

    async def scenario():
        actor = SessionActor()
        sessions = _FakeSessions(actor)
        surface = UnknownSurface(actor)
        sink, run_alias = _sink(tmp_path)
        bundle = _action_bundle()
        gateway = ExecutionGateway(sessions, surface, PolicyEngine(), sink)
        runtime = ReplayRuntime(
            _FakeRegistry(bundle),
            sessions,
            surface,
            gateway,
            CompletionVerifier(),
            sink,
            target_revision=TARGET_REVISION,
        )
        resuming = await _enter_resuming(actor, run_alias)
        result = await asyncio.wait_for(
            runtime._reconcile_after_handoff(
                _reconciliation_context(run_alias, actor, resuming.epoch),
                context=_context(run_alias).model_copy(update={"expected_epoch": resuming.epoch}),
                bundle=bundle,
                step=bundle.steps[0],
                membership_proof=None,
                actor=actor,
                page_id="p_0123456789ab",
                deadline=time.monotonic() + 2,
            ),
            timeout=1,
        )

        assert result.disposition is ReconciliationDisposition.REMAIN_PAUSED
        assert result.reason is SafeReasonCode.UNKNOWN_BLOCKER
        assert surface.side_effects == 0
        assert actor.snapshot.owner == "RESUMING"

    asyncio.run(scenario())
