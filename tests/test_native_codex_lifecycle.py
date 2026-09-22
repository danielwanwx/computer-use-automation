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


def _successful_native_review_result():
    digest = "62e5c052f54f110d9960a66e1f4d7bf91175f48fa8c455af6c7f06a9082e9495"
    revision = "ee82474be5f58bea3ddc8be0fd831072b00201cb"
    return {
        "status": "PASS",
        "artifact": "capability/get_savings_balance@1.0.0",
        "digest": f"sha256:{digest}",
        "validation": "SUCCESS",
        "replay_counts": {"gamma": 1, "delta": 1},
        "provider_calls": 0,
        "credentials": "removed",
        "target": {
            "kind": "parabank",
            "origin": "http://127.0.0.1:8080/parabank",
            "upstream_commit": revision,
            "loopback": True,
        },
        "qualification": {
            "bundle_digest": digest,
            "validation_run_ref": "run_native_review",
            "runtime_fingerprint": {
                "source_sha256": "72ca730a2b8cf7a3f90ce3d52f3c53175360f0398ea16625d1fd3a70f7b5c17d",
                "parser_sha256": "da9a21e25a4ee0ae53b185ca51f297b971d28762eae1ac0a631455c401aac82d",
                "condition_sha256": "090eec64ea44650c393010a3d259a4d12be9ac5634c1ed5bb473a3466364230a",
                "profile_sha256": "7bc859316a875aa33e1f1569cf2c944e357e28cb3d9e8a5e6daa12a8d80f3294",
            },
            "browser_version": "153.0.8010.53",
            "target_revision": revision,
            "replay_passed": True,
            "independent_oracle_passed": True,
        },
        "approval_record": {
            "status": "APPROVED",
            "scope": "temporary_registry",
            "fingerprint_checked": True,
        },
        "independent_oracle_match": True,
        "command": "CUA_NATIVE_REVIEW=1 .venv/bin/python -B scripts/review_native_bundle.py --artifact artifacts/get_savings_balance-1.0.0.json",
    }


def test_reviewer_evidence_emits_only_after_success(monkeypatch, tmp_path):
    output = tmp_path / "v11-native-review.json"
    artifact = review_native_bundle.ROOT / "artifacts/get_savings_balance-1.0.0.json"
    monkeypatch.setattr(review_native_bundle, "_run", lambda path: (_ for _ in ()).throw(RuntimeError("review failed")))

    assert review_native_bundle.main(["--artifact", str(artifact), "--evidence-out", str(output)]) == 1
    assert not output.exists()


def test_reviewer_evidence_contains_file_backed_safe_facts(monkeypatch, tmp_path):
    output = tmp_path / "v11-native-review.json"
    artifact = review_native_bundle.ROOT / "artifacts/get_savings_balance-1.0.0.json"
    monkeypatch.setattr(review_native_bundle, "_run", lambda path: _successful_native_review_result())
    monkeypatch.setattr(
        review_native_bundle,
        "_git_metadata",
        lambda: ("5ab604b4c12a8d151d28691a747ae76057088e26", True),
    )
    monkeypatch.setattr(
        review_native_bundle,
        "_source_fingerprint",
        lambda: "d8ed20b770366a61d87a314d5a10aca27649eb2e96cb12808e8c427c2193edf9",
    )

    assert review_native_bundle.main(["--artifact", str(artifact), "--evidence-out", str(output)]) == 0
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert output.read_bytes() == json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert evidence["case_id"] == "V11"
    assert evidence["status"] == "PASS"
    assert evidence["repository_commit"] == "5ab604b4c12a8d151d28691a747ae76057088e26"
    assert evidence["checkout"]["fresh_clone"] is True
    assert evidence["checkout"]["clean_worktree"] is True
    assert evidence["native_target"]["pinned_revision"] == "ee82474be5f58bea3ddc8be0fd831072b00201cb"
    assert evidence["native_target"]["browser_version"] == "153.0.8010.53"
    assert evidence["approved_artifact"]["path"] == "artifacts/get_savings_balance-1.0.0.json"
    assert evidence["approved_artifact"]["canonical"] is True
    assert evidence["approved_artifact"]["sidecar_committed"] is False
    assert evidence["no_model_replay"]["replay_counts"] == {"gamma": 1, "delta": 1}
    assert evidence["no_model_replay"]["provider_calls"] == 0
    assert evidence["no_model_replay"]["credentials"] == "removed"


def test_safe_trace_summary_contains_no_outputs():
    from cua.models.traces import VerifiedDiscoveryTrace

    trace = VerifiedDiscoveryTrace(trace_id="trace_safe", success=True, events=())
    summary = safe_trace_summary(trace, decision_count=0, model_id="codex-cli")
    assert summary["verified"] is False
    assert "outputs" not in summary
    assert "account" not in json.dumps(summary).lower()
