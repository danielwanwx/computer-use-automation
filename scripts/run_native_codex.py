#!/usr/bin/env python3
"""Run the opt-in live-Codex native ParaBank lifecycle.

The command is intentionally fail-closed.  It requires ``CUA_NATIVE_CODEX_LIVE=1``
and a healthy pinned loopback target, uses the saved Codex login through
``CodexDecisionBackend``, and writes committed artifacts/evidence only after the
full discovery, validation, approval, replay, and value-scan sequence succeeds.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pydantic import SecretStr

from cua.llm import CodexDecisionBackend, OpenAIResponsesDecisionBackend
from cua.models.qualification import ValidationQualification
from cua.registry import BundleRegistry
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from cua.execution.contracts import InvocationStatus
from testbed.evaluator import assert_result_matches_backend, expected_available_balance
from testbed.parabank import DEFAULT_ORIGIN, UPSTREAM_COMMIT, check_health
from testbed.seed import deposit_savings_for_test, seed

from scripts.native_lifecycle_common import (
    ARTIFACT_RELATIVE_PATH,
    EVIDENCE_RELATIVE_PATH,
    export_draft_artifact,
    export_evidence_manifest,
    install_credentials,
    private_values,
    restore_environment,
    runtime_fingerprint_payload,
    safe_bundle_bytes,
    safe_trace_summary,
    target_payload,
    temporary_credentials,
)
from tests.test_discovery_native import (
    _backend_account,
    _principal_specs,
    _record_invocation_terminal,
    _record_discovery_terminal,
    _run_native_discovery,
    _run_native_replays,
    _run_native_validation,
    _result_for_oracle,
)


def _require_explicit_opt_in() -> None:
    if os.environ.get("CUA_NATIVE_CODEX_LIVE") != "1":
        raise RuntimeError(
            "set CUA_NATIVE_CODEX_LIVE=1 to run the live Codex/native lifecycle"
        )


@contextmanager
def _provider_call_trap() -> Iterator[list[int]]:
    """Trap accidental provider use during no-model replay in this process."""

    calls: list[int] = []
    original_codex = CodexDecisionBackend.choose
    original_openai = OpenAIResponsesDecisionBackend.choose

    async def trap(self, *args, **kwargs):
        calls.append(1)
        raise AssertionError("no-model replay called CodexDecisionBackend")

    CodexDecisionBackend.choose = trap
    OpenAIResponsesDecisionBackend.choose = trap
    try:
        yield calls
    finally:
        CodexDecisionBackend.choose = original_codex
        OpenAIResponsesDecisionBackend.choose = original_openai


def _account_ids(manifest: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for principal in manifest.get("principals", ()):
        if not isinstance(principal, dict):
            continue
        savings = [
            str(account["account_id"])
            for account in principal.get("accounts", ())
            if isinstance(account, dict) and account.get("type") == "SAVINGS"
        ]
        if savings:
            result[str(principal["alias"])] = savings[0]
    required = {"alpha", "beta", "gamma", "delta"}
    if not required.issubset(result):
        raise RuntimeError("synthetic seed did not provide all required savings fixtures")
    return result


def _assert_oracle(result, *, account_id: str, origin: str, backend_account: dict) -> None:
    assert_result_matches_backend(
        _result_for_oracle(result),
        requested_account_id=account_id,
        backend_account=backend_account,
    )


def _run_lifecycle() -> dict:
    _require_explicit_opt_in()
    if not check_health():
        raise RuntimeError("pinned loopback ParaBank is not healthy; start it first")
    target = target_payload()
    credentials = temporary_credentials()
    previous = install_credentials(credentials)
    try:
        with tempfile.TemporaryDirectory(prefix="cua-native-codex-") as temporary:
            work_root = Path(temporary)
            seed_path = seed(manifest_path=work_root / "seed_manifest.json")
            manifest = json.loads(seed_path.read_text(encoding="utf-8"))
            account_ids = _account_ids(manifest)
            forbidden = private_values(credentials, manifest)

            backend = CodexDecisionBackend(max_retries=0, max_timeout_seconds=180.0)
            provider_call_count = [0]
            original_choose = backend.choose

            async def counted_choose(request, *, timeout_seconds):
                provider_call_count[0] += 1
                return await original_choose(request, timeout_seconds=timeout_seconds)

            # Instance-level wrapping keeps an exact live-call count without
            # changing the provider implementation or persisting request data.
            backend.choose = counted_choose
            outcome, bundle, backend_account, provider_calls = asyncio.run(
                _run_native_discovery(
                    account_id=account_ids["alpha"],
                    origin=DEFAULT_ORIGIN,
                    target_revision=UPSTREAM_COMMIT,
                    deployment_root=work_root / "discovery-evidence",
                    decision_backend=backend,
                    reviewer_ref="reviewer_native_codex",
                )
            )
            if outcome.status.value != "SUCCESS" or outcome.trace is None:
                raise RuntimeError("live Codex discovery did not produce a verified trace")
            if outcome.decisions_used <= 0 or provider_call_count[0] <= 0:
                raise RuntimeError("live Codex discovery reported no provider decisions")
            assert_result_matches_backend(
                {
                    "status": outcome.status.value,
                    "outputs": {
                        key: value.get_secret_value()
                        for key, value in outcome.outputs.items()
                    },
                },
                requested_account_id=account_ids["alpha"],
                backend_account=backend_account,
            )

            registry = BundleRegistry(work_root / "registry")
            reference = registry.put_draft(bundle)
            validation, browser_version = asyncio.run(
                _run_native_validation(
                    registry=registry,
                    reference=reference,
                    account_id=account_ids["beta"],
                    origin=DEFAULT_ORIGIN,
                    target_revision=UPSTREAM_COMMIT,
                    evidence_root=work_root / "validation-evidence",
                )
            )
            if validation.status is not InvocationStatus.SUCCESS:
                raise RuntimeError("isolated beta draft validation did not succeed")
            _assert_oracle(
                validation,
                account_id=account_ids["beta"],
                origin=DEFAULT_ORIGIN,
                backend_account=_backend_account(DEFAULT_ORIGIN, account_ids["beta"]),
            )
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
            approval = registry.approve(
                reference,
                reviewer_ref="reviewer_live_codex_native",
                reviewer_type="independent_reviewer",
            )

            before_backend = {
                alias: _backend_account(DEFAULT_ORIGIN, account_ids[alias])
                for alias in ("gamma", "delta")
            }
            with _provider_call_trap() as provider_trap:
                before_counts, before_samples = asyncio.run(
                    _run_native_replays(
                        registry=registry,
                        reference=reference,
                        account_ids={
                            "gamma": account_ids["gamma"],
                            "delta": account_ids["delta"],
                        },
                        origin=DEFAULT_ORIGIN,
                        target_revision=UPSTREAM_COMMIT,
                        evidence_root=work_root / "before-replay-evidence",
                        repetitions=2,
                    )
                )
                before_available = expected_available_balance(before_backend["gamma"]["balance"])
                deposit_savings_for_test(
                    account_ids["gamma"], "5.00", manifest_path=seed_path
                )
                after_backend = {
                    alias: _backend_account(DEFAULT_ORIGIN, account_ids[alias])
                    for alias in ("gamma", "delta")
                }
                after_available = expected_available_balance(after_backend["gamma"]["balance"])
                if before_available == after_available:
                    raise RuntimeError("gamma deposit did not change the independent oracle balance")
                after_counts, after_samples = asyncio.run(
                    _run_native_replays(
                        registry=registry,
                        reference=reference,
                        account_ids={
                            "gamma": account_ids["gamma"],
                            "delta": account_ids["delta"],
                        },
                        origin=DEFAULT_ORIGIN,
                        target_revision=UPSTREAM_COMMIT,
                        evidence_root=work_root / "after-replay-evidence",
                        repetitions=3,
                    )
                )
            if provider_trap:
                raise RuntimeError("Codex provider was called during no-model replay")
            if before_counts != {"gamma": 2, "delta": 2} or after_counts != {"gamma": 3, "delta": 3}:
                raise RuntimeError("native replay counts did not cover five runs per unseen customer")
            for alias, outputs in before_samples.items():
                for output in outputs:
                    assert_result_matches_backend(
                        {"status": "SUCCESS", "outputs": {"available_balance": output.get_secret_value(), "currency": "USD"}},
                        requested_account_id=account_ids[alias],
                        backend_account=before_backend[alias],
                    )
            for alias, outputs in after_samples.items():
                for output in outputs:
                    assert_result_matches_backend(
                        {"status": "SUCCESS", "outputs": {"available_balance": output.get_secret_value(), "currency": "USD"}},
                        requested_account_id=account_ids[alias],
                        backend_account=after_backend[alias],
                    )
            if before_samples["gamma"][0].get_secret_value() == after_samples["gamma"][0].get_secret_value():
                raise RuntimeError("gamma replay did not observe the changed balance")

            trace_summary = safe_trace_summary(
                outcome.trace,
                decision_count=outcome.decisions_used,
                model_id=outcome.model_id or backend.model_id,
            )
            canonical, payload = safe_bundle_bytes(bundle, forbidden_values=forbidden)
            if payload.get("provenance", {}).get("verified") is not True:
                raise RuntimeError("compiled artifact is not marked as verified")
            # Check all temporary evidence before exporting any committed files.
            evidence_roots = (
                work_root / "discovery-evidence",
                work_root / "validation-evidence",
                work_root / "before-replay-evidence",
                work_root / "after-replay-evidence",
            )
            for evidence_root in evidence_roots:
                for path in evidence_root.rglob("*") if evidence_root.exists() else ():
                    if path.is_file() and any(value and value in path.read_bytes() for value in forbidden):
                        raise RuntimeError(
                            "private synthetic value reached temporary evidence file "
                            + str(path.relative_to(work_root))
                        )
            qualification_payload = {
                "validation_run_ref": validation.run_id,
                "browser_version": browser_version,
                "runtime_fingerprint": runtime_fingerprint_payload(),
                "independent_oracle_match": True,
            }
            replay_payload = {
                "customers": 2,
                "runs_per_customer": 5,
                "successful_replays": 10,
                "provider_calls": 0,
                "credentials": "removed",
                "independent_oracle_match": True,
                "balance_change_observed": True,
            }
            exported = export_draft_artifact(
                bundle,
                forbidden_values=forbidden,
                trace_summary=trace_summary,
                target=target,
                qualification=qualification_payload,
                replay=replay_payload,
            )
            evidence_payload = {
                "schema_version": 1,
                "case": "live_codex_native_lifecycle",
                "status": "PASS",
                "target": target,
                "provider": {
                    "mode": "codex_saved_login",
                    "model_id": backend.model_id,
                    "discovery_decisions": provider_call_count[0],
                    "replay_provider_calls": 0,
                    "credentials": "removed_from_provider_child",
                },
                "trace": trace_summary,
                "artifact": {
                    "reference": f"capability/{exported.name}@{exported.version}",
                    "digest": f"sha256:{exported.digest}",
                    "path": ARTIFACT_RELATIVE_PATH,
                    "provenance_types": sorted({step.source.type for step in bundle.steps}),
                },
                "validation": qualification_payload | {"status": "SUCCESS"},
                "approval": {
                    "status": "APPROVED_IN_TEMPORARY_REGISTRY",
                    "digest": f"sha256:{approval.digest}",
                    "sidecar_committed": False,
                },
                "replay": replay_payload,
                "evidence_path": EVIDENCE_RELATIVE_PATH,
                "runtime_fingerprint": runtime_fingerprint_payload(),
            }
            export_evidence_manifest(evidence_payload)
            return {
                "status": "PASS",
                "artifact": evidence_payload["artifact"],
                "trace": {"event_count": trace_summary["event_count"], "decisions_used": trace_summary["decisions_used"]},
                "validation": "SUCCESS",
                "approval": "APPROVED_IN_TEMPORARY_REGISTRY",
                "replay": replay_payload,
            }
    finally:
        restore_environment(previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a compact safe JSON result")
    args = parser.parse_args(argv)
    try:
        result = _run_lifecycle()
    except Exception as error:
        # Exception text may include a provider/target diagnostic.  Do not echo
        # subprocess output or request values; the safe command result is enough.
        print(f"native Codex lifecycle blocked: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
