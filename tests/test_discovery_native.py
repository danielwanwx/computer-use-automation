"""Opt-in synthetic native lifecycle check with an offline scripted test backend.

It exercises discovery, isolated draft validation, independent backend evaluation,
approval, and repeated cross-customer replay against the pinned loopback target. It
never calls a model provider and is not real-model or V1 qualification evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from pydantic import SecretStr

from cua.compiler import CapabilityCompiler
from cua.discovery.blueprint import parabank_savings_balance_blueprint
from cua.discovery.goals import GoalBinder
from cua.discovery.runtime import DiscoveryRuntime, DiscoveryStatus
from cua.evidence.models import RunMetadata, RunMode, RunState, SafeReasonCode
from cua.evidence.sink import EvidenceSink
from cua.execution.contracts import ExecutionContext, InvocationResult, InvocationStatus
from cua.execution.gateway import ExecutionGateway
from cua.llm.decisions import OpenAIResponsesDecisionBackend
from cua.llm.decisions import (
    DecisionReply,
    SafeDecisionRequest,
)
from cua.models.actions import BlockedDecision, ClickDecision, DoneDecision, WaitDecision
from cua.models.qualification import ValidationQualification
from cua.policy.engine import PolicyContext, PolicyEngine
from cua.profiles.parabank import PROFILE_ID
from cua.registry import BundleRegistry
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from cua.replay import ReplayRuntime
from cua.sessions import PrincipalSpec, SessionManager
from cua.surface import PlaywrightSurface
from cua.verification import CompletionVerifier
from testbed.evaluator import assert_result_matches_backend, expected_available_balance
from testbed.parabank import DEFAULT_ORIGIN, UPSTREAM_COMMIT
from testbed.seed import deposit_savings_for_test, seed


@pytest.mark.skipif(
    os.environ.get("CUA_PARABANK_LIVE_TEST") != "1",
    reason="requires an explicitly enabled pinned loopback ParaBank testbed",
)
def test_native_offline_discovery_validation_approval_and_cross_client_replay(
    tmp_path, monkeypatch
):
    __tracebackhide__ = True
    # A provider call is a hard test failure. Remove ambient credentials without
    # reading their values; this test uses only the explicitly injected test backend.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    async def reject_provider_call(*args, **kwargs):
        raise AssertionError("native offline test must not call the model provider")

    monkeypatch.setattr(OpenAIResponsesDecisionBackend, "choose", reject_provider_call)
    credentials = {
        "PARABANK_DEMO_ALPHA_USERNAME": "cua_alpha_" + secrets.token_hex(4),
        "PARABANK_DEMO_ALPHA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_BETA_USERNAME": "cua_beta_" + secrets.token_hex(4),
        "PARABANK_DEMO_BETA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_GAMMA_USERNAME": "cua_gamma_" + secrets.token_hex(4),
        "PARABANK_DEMO_GAMMA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_DELTA_USERNAME": "cua_delta_" + secrets.token_hex(4),
        "PARABANK_DEMO_DELTA_PASSWORD": secrets.token_hex(8),
    }
    monkeypatch.setenv("PARABANK_ORIGIN", DEFAULT_ORIGIN)
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)

    project_root = Path(__file__).resolve().parents[1]
    deployment_path = project_root / "testbed/.cache/deployment_manifest.json"
    if not deployment_path.is_file():
        raise AssertionError("pinned deployment manifest is required for this native check")
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    runtime = deployment.get("runtime", {})
    if (
        deployment.get("source", {}).get("immutable_commit") != UPSTREAM_COMMIT
        or runtime.get("http_connector") != "127.0.0.1:8080"
        or runtime.get("activemq_connector") != "127.0.0.1:61616"
        or runtime.get("hsqldb_bind_address") != "127.0.0.1"
    ):
        raise AssertionError("local ParaBank deployment does not match the pinned target")

    seed_path = seed(manifest_path=tmp_path / "synthetic_seed_manifest.json")
    manifest = json.loads(seed_path.read_text(encoding="utf-8"))
    if (
        manifest.get("upstream_commit") != UPSTREAM_COMMIT
        or manifest.get("origin") != DEFAULT_ORIGIN
    ):
        raise AssertionError("synthetic test fixture is not bound to the pinned loopback target")
    alpha = next(item for item in manifest["principals"] if item["alias"] == "alpha")
    savings = tuple(
        item["account_id"] for item in alpha["accounts"] if item["type"] == "SAVINGS"
    )
    if len(savings) < 2:
        raise AssertionError("synthetic discovery principal must have two savings accounts")

    principals = {item["alias"]: item for item in manifest["principals"]}
    account_ids = {
        alias: tuple(
            account["account_id"]
            for account in principal["accounts"]
            if account["type"] == "SAVINGS"
        )
        for alias, principal in principals.items()
    }
    if not all(account_ids.get(alias) for alias in ("beta", "gamma", "delta")):
        raise AssertionError("validation and unseen replay fixtures must each have savings accounts")

    evidence_root = tmp_path / "evidence"
    outcome, bundle, backend_account, test_backend_calls = asyncio.run(
        _run_native_discovery(
            account_id=savings[0],
            origin=manifest["origin"],
            target_revision=UPSTREAM_COMMIT,
            deployment_root=evidence_root,
        )
    )
    if outcome.status is not DiscoveryStatus.SUCCESS or outcome.trace is None:
        raise AssertionError("offline-scripted native discovery did not verify successfully")
    if outcome.verified_action_count != len(outcome.trace.events) or not outcome.trace.events:
        raise AssertionError("compiled trace must contain the actual verified native action events")
    if test_backend_calls < 1:
        raise AssertionError("offline scripted backend was not used for discovery decisions")

    outputs = {
        key: value.get_secret_value()
        for key, value in outcome.outputs.items()
    }
    assert_result_matches_backend(
        {"status": outcome.status.value, "outputs": outputs},
        requested_account_id=savings[0],
        backend_account=backend_account,
    )
    observed_steps = tuple(step for step in bundle.steps if step.source.type == "observed")
    if tuple(step.source.event_ids for step in observed_steps) != tuple(
        (event.event_id,) for event in outcome.trace.events
    ):
        raise AssertionError("compiler invented or removed an observed discovery event")
    if any(event.effect_state != "VERIFIED" for event in outcome.trace.events):
        raise AssertionError("only verified effects may enter the compiled draft")
    if not observed_steps or not any(
        step.target_ref == "requested_account_link" for step in observed_steps
    ):
        raise AssertionError("native discovery did not observe opening the requested account")
    if bundle.steps[0].id != "ensure_overview" or bundle.steps[0].source.type != "reviewer_added":
        raise AssertionError("reviewer-added overview anchor was not compiled first")
    if bundle.steps[1].kind != "ASSERT" or bundle.steps[1].source.type != "declared":
        raise AssertionError("declared membership assertion must precede account use")
    if bundle.steps[-2].kind != "EXTRACT" or bundle.steps[-2].source.type != "declared":
        raise AssertionError("available balance extraction must be a declared checkpoint")
    if bundle.steps[-1].kind != "VERIFY" or bundle.steps[-1].source.type != "declared":
        raise AssertionError("declared runtime verification must remain the final bundle step")
    if not bundle.recoveries or bundle.recoveries[0].ref != "readonly_overview_anchor":
        raise AssertionError("the account-click recovery must use the declared overview anchor")
    account_clicks = tuple(
        step for step in observed_steps if step.target_ref == "requested_account_link"
    )
    if len(account_clicks) != 1 or account_clicks[0].recovery_ref != "readonly_overview_anchor":
        raise AssertionError("only the observed account click may carry the bounded read-only recovery")

    registry = BundleRegistry(tmp_path / "registry")
    reference = registry.put_draft(bundle)

    validation, validation_browser = asyncio.run(
        _run_native_validation(
            registry=registry,
            reference=reference,
            account_id=account_ids["beta"][0],
            origin=manifest["origin"],
            target_revision=UPSTREAM_COMMIT,
            evidence_root=evidence_root,
        )
    )
    if validation.status is not InvocationStatus.SUCCESS:
        raise AssertionError("isolated synthetic DRAFT validation failed")
    assert_result_matches_backend(
        _result_for_oracle(validation),
        requested_account_id=account_ids["beta"][0],
        backend_account=_backend_account(manifest["origin"], account_ids["beta"][0]),
    )

    qualification = ValidationQualification(
        bundle_digest=reference.digest,
        validation_run_ref=validation.run_id,
        runtime_fingerprint=current_runtime_fingerprint(),
        browser_version=validation_browser,
        target_revision=UPSTREAM_COMMIT,
        replay_passed=True,
        independent_oracle_passed=True,
    )
    stored_qualification = registry.validate(reference, qualification)
    if stored_qualification.bundle_digest != reference.digest:
        raise AssertionError("draft validation was not pinned to the compiled digest")
    approval = registry.approve(
        reference,
        reviewer_ref="reviewer_native_test_fixture",
        reviewer_type="independent_reviewer",
    )
    if approval.digest != reference.digest or approval.validation_run_ref != validation.run_id:
        raise AssertionError("approval does not close over the validated draft and run")

    gamma_account = account_ids["gamma"][0]
    before_counts, before_outputs = asyncio.run(
        _run_native_replays(
            registry=registry,
            reference=reference,
            account_ids={"gamma": gamma_account, "delta": account_ids["delta"][0]},
            origin=manifest["origin"],
            target_revision=UPSTREAM_COMMIT,
            evidence_root=evidence_root,
            repetitions=2,
        )
    )
    if before_counts != {"gamma": 2, "delta": 2}:
        raise AssertionError("first approved replay batch did not cover two runs per unseen client")

    # The first batch releases both sessions before the testbed-only mutation.
    # The recovery declaration is policy metadata; this run does not claim its
    # recovery branch was observed or executed.
    balance_before_deposit = _backend_account(manifest["origin"], gamma_account)
    deposit_savings_for_test(gamma_account, "5.00", manifest_path=seed_path)
    balance_after_deposit = _backend_account(manifest["origin"], gamma_account)
    old_available = expected_available_balance(balance_before_deposit["balance"])
    new_available = expected_available_balance(balance_after_deposit["balance"])
    if new_available == old_available:
        raise AssertionError("testbed deposit did not create an observable available-balance change")

    after_counts, after_outputs = asyncio.run(
        _run_native_replays(
            registry=registry,
            reference=reference,
            account_ids={"gamma": gamma_account, "delta": account_ids["delta"][0]},
            origin=manifest["origin"],
            target_revision=UPSTREAM_COMMIT,
            evidence_root=evidence_root,
            repetitions=3,
        )
    )
    if after_counts != {"gamma": 3, "delta": 3}:
        raise AssertionError("second approved replay batch did not cover three runs per unseen client")
    replay_counts = {
        alias: before_counts[alias] + after_counts[alias]
        for alias in ("gamma", "delta")
    }
    if replay_counts != {"gamma": 5, "delta": 5}:
        raise AssertionError("approved replay count did not cover five runs for each unseen client")
    if not before_outputs["gamma"] or not after_outputs["gamma"]:
        raise AssertionError("gamma replay output samples are missing")
    if before_outputs["gamma"][0].get_secret_value() == after_outputs["gamma"][0].get_secret_value():
        raise AssertionError("approved replay returned a stale gamma balance after testbed deposit")

    private_values = {
        value.encode("utf-8")
        for value in credentials.values()
    }
    for principal in manifest["principals"]:
        private_values.add(str(principal["customer_id"]).encode("utf-8"))
        private_values.update(
            str(account["account_id"]).encode("utf-8")
            for account in principal["accounts"]
        )
    for path in evidence_root.rglob("*"):
        if path.is_file():
            encoded = path.read_bytes()
            if any(value in encoded for value in private_values):
                raise AssertionError("a synthetic account ID or credential reached persisted evidence")


async def _run_native_discovery(
    *,
    account_id,
    origin,
    target_revision,
    deployment_root,
    decision_backend=None,
    reviewer_ref="reviewer_native_test_fixture",
):
    __tracebackhide__ = True
    manager = SessionManager(
        (
            PrincipalSpec(
                "alpha",
                "PARABANK_DEMO_ALPHA_USERNAME",
                "PARABANK_DEMO_ALPHA_PASSWORD",
                "Synthetic Alpha",
            ),
        ),
        origin=origin,
        headless=True,
    )
    surface = PlaywrightSurface(manager, sample_interval_seconds=0.05)
    handle = None
    actor = None
    run_id = None
    evidence = EvidenceSink(deployment_root)
    try:
        handle = await manager.prepare("alpha")
        state = await manager.get_state(handle.session_id)
        actor = state.actor
        backend = decision_backend or _NativeScriptedTestBackend()
        run_id = evidence.register_run(
            RunMetadata(
                mode=RunMode.DISCOVERY,
                capability_name="get_savings_balance",
                capability_version="1.0.0",
                bundle_digest=None,
                profile_id=PROFILE_ID,
                target_revision=target_revision,
                browser_version=handle.browser_version,
                model_id=backend.model_id,
            )
        )
        evidence.transition_run(run_id, RunState.RUNNING)
        await actor.begin_run(run_id)
        policy = _policy(handle.origin)
        context = ExecutionContext(
            run_alias=run_id,
            session_id=handle.session_id,
            expected_epoch=actor.epoch,
            authentication_generation=handle.auth_generation,
            target_origin=handle.origin,
            profile_id=handle.profile_id,
            input_bindings={"inputs.account_id": SecretStr(account_id)},
            policy_context=policy,
        )
        blueprint = parabank_savings_balance_blueprint(
            reviewer_ref=reviewer_ref,
            capability_version="1.0.0",
        )
        gateway = ExecutionGateway(manager, surface, PolicyEngine(), evidence)
        runtime = DiscoveryRuntime(manager, surface, gateway, backend=backend)
        intent = GoalBinder().bind(
            f"Get the available balance for savings account {account_id}"
        )
        outcome = await runtime.run(context, intent, blueprint.contract)
        _record_discovery_terminal(evidence, run_id, outcome)
        if outcome.status is not DiscoveryStatus.SUCCESS or outcome.trace is None:
            raise AssertionError("native discovery did not produce a verified completion trace")

        oracle = build_opener(ProxyHandler({}))
        request = Request(
            origin.rstrip("/") + "/services/bank/accounts/" + account_id,
            headers={"Accept": "application/json"},
        )
        with oracle.open(request, timeout=5.0) as response:
            body = response.read()
        try:
            backend_account = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AssertionError("local backend oracle response is not JSON") from None
        if not isinstance(backend_account, dict):
            raise AssertionError("local backend oracle record has an invalid shape")
        bundle = CapabilityCompiler().compile(outcome.trace, blueprint)
        return outcome, bundle, backend_account, getattr(backend, "calls", None)
    finally:
        if actor is not None and run_id is not None and actor.active_run_id == run_id:
            await actor.finish_run(run_id)
        await manager.close_all()


async def _run_native_validation(
    *, registry, reference, account_id, origin, target_revision, evidence_root
):
    __tracebackhide__ = True
    manager = SessionManager(
        _principal_specs(),
        origin=origin,
        headless=True,
        validation_principal_aliases=("beta",),
    )
    surface = PlaywrightSurface(manager, sample_interval_seconds=0.05)
    evidence = EvidenceSink(evidence_root)
    handle = None
    actor = None
    run_id = None
    try:
        handle, validation_binding = await manager.prepare_validation_session("beta")
        actor = (await manager.get_state(handle.session_id)).actor
        run_id = evidence.register_run(
            RunMetadata(
                mode=RunMode.VALIDATION,
                capability_name=reference.name,
                capability_version=reference.version,
                bundle_digest=reference.digest,
                profile_id=handle.profile_id,
                target_revision=target_revision,
                browser_version=handle.browser_version,
                model_id=None,
            )
        )
        evidence.transition_run(run_id, RunState.RUNNING)
        await actor.begin_run(run_id)
        gateway = ExecutionGateway(manager, surface, PolicyEngine(), evidence)
        runtime = ReplayRuntime(
            registry,
            manager,
            surface,
            gateway,
            CompletionVerifier(),
            evidence,
            target_revision=target_revision,
        )
        result = await runtime.run_validation(
            reference,
            _execution_context(handle, actor, run_id, account_id),
            validation_binding,
        )
        await actor.finish_run(run_id)
        _record_invocation_terminal(evidence, run_id, result)
        return result, handle.browser_version
    finally:
        if actor is not None and run_id is not None and actor.active_run_id == run_id:
            await actor.finish_run(run_id)
        await manager.close_all()


def _record_discovery_terminal(evidence, run_id, outcome):
    if outcome.status is DiscoveryStatus.SUCCESS:
        evidence.transition_run(
            run_id,
            RunState.SUCCESS,
            outcome_code=SafeReasonCode.DISCOVERY_COMPLETE,
        )
        return
    if outcome.status is DiscoveryStatus.BUSINESS_OUTCOME:
        state = RunState.BUSINESS_OUTCOME
    else:
        state = RunState.FAILURE
    try:
        reason = SafeReasonCode(outcome.reason_code)
    except ValueError:
        reason = SafeReasonCode.INTERNAL_ERROR
    evidence.transition_run(run_id, state, outcome_code=reason)


async def _run_native_replays(
    *, registry, reference, account_ids, origin, target_revision, evidence_root, repetitions
):
    __tracebackhide__ = True
    manager = SessionManager(_principal_specs(), origin=origin, headless=True)
    surface = PlaywrightSurface(manager, sample_interval_seconds=0.05)
    evidence = EvidenceSink(evidence_root)
    runtime = ReplayRuntime(
        registry,
        manager,
        surface,
        ExecutionGateway(manager, surface, PolicyEngine(), evidence),
        CompletionVerifier(),
        evidence,
        target_revision=target_revision,
    )
    counts = {}
    output_samples = {principal_alias: [] for principal_alias in account_ids}
    try:
        for principal_alias, account_id in account_ids.items():
            handle = await manager.prepare(principal_alias)
            actor = (await manager.get_state(handle.session_id)).actor
            for _ in range(repetitions):
                run_id = evidence.register_run(
                    RunMetadata(
                        mode=RunMode.REPLAY,
                        capability_name=reference.name,
                        capability_version=reference.version,
                        bundle_digest=reference.digest,
                        profile_id=handle.profile_id,
                        target_revision=target_revision,
                        browser_version=handle.browser_version,
                        model_id=None,
                    )
                )
                evidence.transition_run(run_id, RunState.RUNNING)
                await actor.begin_run(run_id)
                result = await runtime.run(
                    reference,
                    _execution_context(handle, actor, run_id, account_id),
                )
                await actor.finish_run(run_id)
                _record_invocation_terminal(evidence, run_id, result)
                if result.status is not InvocationStatus.SUCCESS:
                    raise AssertionError("approved synthetic replay did not succeed")
                assert_result_matches_backend(
                    _result_for_oracle(result),
                    requested_account_id=account_id,
                    backend_account=await asyncio.to_thread(
                        _backend_account, origin, account_id
                    ),
                )
                counts[principal_alias] = counts.get(principal_alias, 0) + 1
                output_samples[principal_alias].append(result.outputs["available_balance"])
        return counts, output_samples
    finally:
        await manager.close_all()


def _principal_specs():
    return tuple(
        PrincipalSpec(
            alias,
            "PARABANK_DEMO_{}_USERNAME".format(alias.upper()),
            "PARABANK_DEMO_{}_PASSWORD".format(alias.upper()),
            "Synthetic {}".format(alias.title()),
        )
        for alias in ("alpha", "beta", "gamma", "delta")
    )


def _execution_context(handle, actor, run_id, account_id):
    __tracebackhide__ = True
    return ExecutionContext(
        run_alias=run_id,
        session_id=handle.session_id,
        expected_epoch=actor.epoch,
        authentication_generation=handle.auth_generation,
        target_origin=handle.origin,
        profile_id=handle.profile_id,
        input_bindings={"inputs.account_id": SecretStr(account_id)},
        policy_context=_policy(handle.origin),
    )


def _record_invocation_terminal(evidence, run_id, result: InvocationResult):
    if result.status is InvocationStatus.SUCCESS:
        state, code = RunState.SUCCESS, SafeReasonCode.REPLAY_COMPLETE
    elif result.status is InvocationStatus.BUSINESS_OUTCOME:
        state, code = RunState.BUSINESS_OUTCOME, result.code
    elif result.status is InvocationStatus.ABORTED:
        state, code = RunState.ABORTED, result.code or SafeReasonCode.REPLAY_FAILED
    else:
        state = RunState.FAILURE
        code = result.failure.reason_code if result.failure else SafeReasonCode.REPLAY_FAILED
    evidence.transition_run(run_id, state, outcome_code=code)


def _result_for_oracle(result: InvocationResult):
    return {
        "status": result.status.value,
        "outputs": {
            key: value.get_secret_value()
            for key, value in (result.outputs or {}).items()
        },
    }


def _backend_account(origin: str, account_id: str):
    __tracebackhide__ = True
    opener = build_opener(ProxyHandler({}))
    request = Request(
        origin.rstrip("/") + "/services/bank/accounts/" + account_id,
        headers={"Accept": "application/json"},
    )
    with opener.open(request, timeout=5.0) as response:
        body = response.read()
    try:
        account = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AssertionError("local backend oracle response is not JSON") from None
    if not isinstance(account, dict):
        raise AssertionError("local backend oracle record has an invalid shape")
    return account


class _NativeScriptedTestBackend:
    """Deterministic test DI over safe summaries; never selected by runtime defaults."""

    model_id = "offline-scripted-native-test"

    def __init__(self):
        self.calls = 0

    async def choose(self, request: SafeDecisionRequest, *, timeout_seconds: float):
        self.calls += 1
        observation = request.observation
        if observation.route == "home":
            wanted = "Accounts Overview"
        elif observation.route == "accounts_overview":
            if request.signals.overview_complete is not True:
                return self._reply(WaitDecision(
                    observation_id=observation.observation_id,
                    operation="WAIT",
                    reason_code="TEST_WAIT",
                    rationale="Wait for a complete overview.",
                    timeout_ms=100,
                ))
            if request.signals.requested_account_present is not True:
                return self._reply(BlockedDecision(
                    observation_id=observation.observation_id,
                    operation="BLOCKED",
                    reason_code="TEST_BLOCKED",
                    rationale="The requested account is not available.",
                ))
            wanted = "<requested_account>"
        elif observation.route == "account_details":
            signals = request.signals
            if (
                signals.membership_valid
                and signals.requested_account_matches is True
                and signals.account_type_is_savings is True
                and signals.available_balance_parseable
            ):
                return self._reply(DoneDecision(
                    observation_id=observation.observation_id,
                    operation="DONE",
                    reason_code="TEST_COMPLETE",
                    rationale="The bound savings account fields are ready for verification.",
                ))
            return self._reply(WaitDecision(
                observation_id=observation.observation_id,
                operation="WAIT",
                reason_code="TEST_WAIT",
                rationale="Wait for bound account details.",
                timeout_ms=100,
            ))
        else:
            return self._reply(BlockedDecision(
                observation_id=observation.observation_id,
                operation="BLOCKED",
                reason_code="TEST_BLOCKED",
                rationale="The page is outside this test's supported routes.",
            ))

        control = next(
            (item for item in observation.controls if item.safe_name == wanted),
            None,
        )
        if control is None:
            return self._reply(BlockedDecision(
                observation_id=observation.observation_id,
                operation="BLOCKED",
                reason_code="TEST_BLOCKED",
                rationale="The required safe control is not available.",
            ))
        return self._reply(ClickDecision(
            observation_id=observation.observation_id,
            operation="CLICK",
            reason_code="TEST_CLICK",
            rationale="Use the currently observed safe control.",
            control_ref=control.control_ref,
        ))

    def _reply(self, decision):
        return DecisionReply(decision, self.model_id, None, None)


def _policy(origin: str) -> PolicyContext:
    routes = ("home", "accounts_overview", "account_details")
    operations = ("CLICK", "READ")
    return PolicyContext(
        deployment_origins=(origin,),
        capability_origins=(origin,),
        run_origins=(origin,),
        deployment_routes=routes,
        capability_routes=routes,
        run_routes=routes,
        deployment_operations=operations,
        capability_operations=operations,
        run_operations=operations,
    )
