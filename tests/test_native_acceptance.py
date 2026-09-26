"""Opt-in native acceptance checks that do not require a live model or a person.

These tests use a temporary, scripted capability only to exercise the pinned
ParaBank UI. They are native-target checks, not live-model discovery evidence.
Fault-injection assertions remain labelled in the test names.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets

import pytest
from pydantic import SecretStr

from cua.evidence import EvidenceSink, RunMetadata, RunMode, RunState
from cua.evidence.models import SafeReasonCode
from cua.llm.decisions import OpenAIResponsesDecisionBackend
from cua.profiles.parabank import PROFILE_ID
from cua.sessions import PrincipalSpec, SessionManager
from cua.surface import PlaywrightSurface
from testbed.evaluator import assert_result_matches_backend
from testbed.parabank import DEFAULT_ORIGIN, UPSTREAM_COMMIT
from testbed.seed import seed

from tests.test_discovery_native import (
    _backend_account,
    _principal_specs,
    _run_native_discovery,
    _run_native_replays,
    _run_native_validation,
)
from cua.models.qualification import ValidationQualification
from cua.registry import BundleRegistry
from cua.registry.runtime_fingerprint import current_runtime_fingerprint


LIVE_MARK = pytest.mark.skipif(
    os.environ.get("CUA_PARABANK_LIVE_TEST") != "1",
    reason="requires the explicitly enabled loopback-only synthetic ParaBank target",
)


def _temporary_credentials() -> dict[str, str]:
    return {
        "PARABANK_DEMO_ALPHA_USERNAME": "native_a_" + secrets.token_hex(3),
        "PARABANK_DEMO_ALPHA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_BETA_USERNAME": "native_b_" + secrets.token_hex(3),
        "PARABANK_DEMO_BETA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_GAMMA_USERNAME": "native_g_" + secrets.token_hex(3),
        "PARABANK_DEMO_GAMMA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_DELTA_USERNAME": "native_d_" + secrets.token_hex(3),
        "PARABANK_DEMO_DELTA_PASSWORD": secrets.token_hex(8),
    }


@pytest.fixture(scope="module")
def native_capability(tmp_path_factory):
    credentials = _temporary_credentials()
    env_names = tuple(credentials) + ("PARABANK_ORIGIN", "OPENAI_API_KEY", "LLM_API_KEY")
    previous = {name: os.environ.get(name) for name in env_names}
    os.environ["PARABANK_ORIGIN"] = DEFAULT_ORIGIN
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("LLM_API_KEY", None)
    os.environ.update(credentials)
    root = tmp_path_factory.mktemp("native-acceptance")
    try:
        seed_path = seed(manifest_path=root / "seed_manifest.json")
        manifest = json.loads(seed_path.read_text(encoding="utf-8"))
        alpha = next(item for item in manifest["principals"] if item["alias"] == "alpha")
        alpha_savings = next(
            item["account_id"] for item in alpha["accounts"] if item["type"] == "SAVINGS"
        )
        outcome, bundle, _, _ = asyncio.run(
            _run_native_discovery(
                account_id=alpha_savings,
                origin=DEFAULT_ORIGIN,
                target_revision=UPSTREAM_COMMIT,
                deployment_root=root / "discovery-evidence",
            )
        )
        assert outcome.status.value == "SUCCESS"
        registry = BundleRegistry(root / "registry")
        # The helper already compiles the pinned blueprint; the temporary registry
        # stores that exact draft for the no-model replay checks.
        reference = registry.put_draft(bundle)
        validation, browser_version = asyncio.run(
            _run_native_validation(
                registry=registry,
                reference=reference,
                account_id=next(
                    item["account_id"]
                    for item in next(
                        item for item in manifest["principals"] if item["alias"] == "beta"
                    )["accounts"]
                    if item["type"] == "SAVINGS"
                ),
                origin=DEFAULT_ORIGIN,
                target_revision=UPSTREAM_COMMIT,
                evidence_root=root / "validation-evidence",
            )
        )
        assert validation.status.value == "SUCCESS"
        qualification = ValidationQualification(
            bundle_digest=reference.digest,
            validation_run_ref=validation.run_id,
            runtime_fingerprint=current_runtime_fingerprint(),
            browser_version=browser_version,
            target_revision=UPSTREAM_COMMIT,
            replay_passed=True,
            independent_oracle_passed=True,
        )
        registry.validate(reference, qualification)
        registry.approve(reference, reviewer_ref="reviewer_native_acceptance", reviewer_type="independent_reviewer")
        yield {
            "root": root,
            "registry": registry,
            "reference": reference,
            "manifest": manifest,
        }
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@LIVE_MARK
def test_native_v3_zero_model_replay_matches_independent_oracle(native_capability, monkeypatch):
    """Run approved UI replays with provider credentials removed and a trap backend."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    async def provider_trap(*args, **kwargs):
        raise AssertionError("native no-model replay called the provider")

    monkeypatch.setattr(OpenAIResponsesDecisionBackend, "choose", provider_trap)
    savings_by_alias = {
        item["alias"]: next(
            account["account_id"]
            for account in item["accounts"]
            if account["type"] == "SAVINGS"
        )
        for item in native_capability["manifest"]["principals"]
        if item["alias"] in {"gamma", "delta"}
    }
    counts, samples = asyncio.run(
        _run_native_replays(
            registry=native_capability["registry"],
            reference=native_capability["reference"],
            account_ids=savings_by_alias,
            origin=DEFAULT_ORIGIN,
            target_revision=UPSTREAM_COMMIT,
            evidence_root=native_capability["root"] / "v3-evidence",
            repetitions=1,
        )
    )
    assert counts == {"gamma": 1, "delta": 1}
    for alias, account_id in savings_by_alias.items():
        assert samples[alias]
        assert_result_matches_backend(
            {"status": "SUCCESS", "outputs": {"available_balance": samples[alias][0].get_secret_value(), "currency": "USD"}},
            requested_account_id=account_id,
            backend_account=_backend_account(DEFAULT_ORIGIN, account_id),
        )


@LIVE_MARK
def test_native_v10_safe_snapshot_excludes_protected_binding(native_capability):
    """Capture a native safe snapshot and verify account bindings never persist."""
    account_id = next(
        account["account_id"]
        for principal in native_capability["manifest"]["principals"]
        if principal["alias"] == "alpha"
        for account in principal["accounts"]
        if account["type"] == "SAVINGS"
    )

    async def scenario():
        manager = SessionManager(_principal_specs(), origin=DEFAULT_ORIGIN, headless=True)
        surface = PlaywrightSurface(manager, sample_interval_seconds=0.05)
        evidence = EvidenceSink(native_capability["root"] / "v10-evidence")
        handle = await manager.prepare("alpha")
        state = await manager.get_state(handle.session_id)
        run_id = evidence.register_run(
            RunMetadata(
                mode=RunMode.REPLAY,
                capability_name="get_savings_balance",
                capability_version="1.0.0",
                bundle_digest=native_capability["reference"].digest,
                profile_id=PROFILE_ID,
                target_revision=UPSTREAM_COMMIT,
                browser_version=handle.browser_version,
                model_id=None,
            )
        )
        evidence.transition_run(run_id, RunState.RUNNING)
        await state.actor.begin_run(run_id)
        try:
            observation, view = await state.actor.submit(
                expected_epoch=state.actor.epoch,
                run_id=run_id,
                operation=lambda: surface.observe(
                    handle.session_id,
                    bindings={"inputs.account_id": SecretStr(account_id)},
                ),
            )
            evidence.capture_safe(run_id, view, observation=observation)
            evidence.transition_run(run_id, RunState.ABORTED, outcome_code=SafeReasonCode.REPLAY_FAILED)
        finally:
            await state.actor.finish_run(run_id)
            await manager.close_all()
        for path in (native_capability["root"] / "v10-evidence").rglob("*"):
            if path.is_file():
                assert account_id.encode("utf-8") not in path.read_bytes()

    asyncio.run(scenario())
