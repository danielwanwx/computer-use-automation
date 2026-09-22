"""Offline guards for the explicit live-Codex native lifecycle commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
import asyncio

import pytest

from cua.models.bundles import StepSource
from scripts.native_lifecycle_common import safe_bundle_bytes, safe_trace_summary
from scripts.native_lifecycle_common import install_credentials, restore_environment
from scripts import review_native_bundle, run_native_codex
from cua.llm import CodexDecisionBackend, OpenAIResponsesDecisionBackend
from tests.test_bundle_registry import _bundle


def test_safe_bundle_export_requires_observed_declared_and_reviewer_provenance():
    bundle = _bundle()
    with pytest.raises(ValueError, match="provenance"):
        safe_bundle_bytes(bundle)

    reviewer_step = bundle.steps[0].model_copy(
        update={"source": StepSource(type="reviewer_added", reviewer_ref="reviewer_test")}
    )
    declared_step = bundle.steps[0].model_copy(
        update={"id": "declared_check", "source": StepSource(type="declared")}
    )
    complete = bundle.model_copy(update={"steps": (reviewer_step, declared_step, bundle.steps[1])})
    canonical, payload = safe_bundle_bytes(complete, forbidden_values=(b"secret-fixture",))
    assert json.loads(canonical) == payload
    assert {step["source"]["type"] for step in payload["steps"]} == {
        "observed",
        "declared",
        "reviewer_added",
    }


def test_safe_bundle_export_rejects_private_value():
    bundle = _bundle()
    reviewer_step = bundle.steps[0].model_copy(
        update={"source": StepSource(type="reviewer_added", reviewer_ref="reviewer_test")}
    )
    declared_step = bundle.steps[0].model_copy(
        update={"id": "declared_check", "source": StepSource(type="declared")}
    )
    complete = bundle.model_copy(update={"steps": (reviewer_step, declared_step, bundle.steps[1])})
    with pytest.raises(ValueError, match="private synthetic value"):
        safe_bundle_bytes(complete, forbidden_values=(b"get_savings_balance",))


def test_live_runner_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("CUA_NATIVE_CODEX_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="CUA_NATIVE_CODEX_LIVE"):
        run_native_codex._require_explicit_opt_in()


def test_reviewer_requires_explicit_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("CUA_NATIVE_REVIEW", raising=False)
    with pytest.raises(RuntimeError, match="CUA_NATIVE_REVIEW"):
        review_native_bundle._run(tmp_path / "missing.json")


def test_reviewer_demo_has_no_model_backend_import():
    source = Path(review_native_bundle.__file__).read_text(encoding="utf-8")
    assert "CodexDecisionBackend(" not in source
    assert "OpenAIResponsesDecisionBackend(" not in source
    assert "_provider_call_trap" in source


def test_native_runner_scrubs_ambient_provider_credentials(monkeypatch):
    names = (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_FEDERATION_RULE_ID",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "LLM_API_KEY",
    )
    for name in names:
        monkeypatch.setenv(name, "ambient-secret")
    previous = install_credentials({"PARABANK_DEMO_ALPHA_USERNAME": "synthetic-user"})
    try:
        assert all(name not in os.environ for name in names)
    finally:
        restore_environment(previous)


def test_reviewer_provider_trap_catches_both_backends():
    async def invoke(backend):
        with pytest.raises(AssertionError, match="model provider"):
            await backend.choose(object(), timeout_seconds=1)

    with review_native_bundle._provider_call_trap() as calls:
        asyncio.run(invoke(CodexDecisionBackend))
        asyncio.run(invoke(OpenAIResponsesDecisionBackend))
    assert calls == [1, 1]


def test_safe_trace_summary_contains_no_outputs():
    from cua.models.traces import VerifiedDiscoveryTrace

    trace = VerifiedDiscoveryTrace(trace_id="trace_safe", success=True, events=())
    summary = safe_trace_summary(trace, decision_count=0, model_id="codex-cli")
    assert summary["verified"] is False
    assert "outputs" not in summary
    assert "account" not in json.dumps(summary).lower()
