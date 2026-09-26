#!/usr/bin/env python3
"""Review and replay the committed native draft without a model provider.

This command imports the committed value-safe bundle into a fresh temporary
registry, seeds disposable local fixtures, validates on beta, approves the draft
in that temporary registry, and replays gamma/delta.  It never constructs a
model backend and refuses to run without explicit ``CUA_NATIVE_REVIEW=1``.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cua.execution.contracts import InvocationStatus
from cua.llm import CodexDecisionBackend, OpenAIResponsesDecisionBackend
from cua.models.bundles import BundleReference, CapabilityBundle
from cua.models.qualification import ValidationQualification
from cua.registry import BundleRegistry
from cua.registry.bundle_registry import canonical_bundle_json
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from testbed.evaluator import assert_result_matches_backend
from testbed.parabank import DEFAULT_ORIGIN, UPSTREAM_COMMIT, check_health
from testbed.seed import seed

from scripts.native_lifecycle_common import (
    ARTIFACT_RELATIVE_PATH,
    contains_private_value,
    install_credentials,
    private_values,
    restore_environment,
    temporary_credentials,
    target_payload,
)
from tests.test_discovery_native import (
    _backend_account,
    _run_native_replays,
    _run_native_validation,
)


@contextmanager
def _provider_call_trap():
    """Prove validation and replay do not silently call either provider."""

    calls: list[int] = []
    original_codex = CodexDecisionBackend.choose
    original_openai = OpenAIResponsesDecisionBackend.choose

    async def trap(self, *args, **kwargs):
        calls.append(1)
        raise AssertionError("native reviewer must not call a model provider")

    CodexDecisionBackend.choose = trap
    OpenAIResponsesDecisionBackend.choose = trap
    try:
        yield calls
    finally:
        CodexDecisionBackend.choose = original_codex
        OpenAIResponsesDecisionBackend.choose = original_openai


def _load_bundle(path: Path) -> tuple[CapabilityBundle, BundleReference]:
    try:
        raw = path.read_bytes()
        bundle = CapabilityBundle.model_validate_json(raw)
    except (OSError, UnicodeError, ValueError) as error:
        raise RuntimeError("committed native bundle cannot be loaded") from error
    canonical = canonical_bundle_json(bundle)
    if raw != canonical:
        raise RuntimeError("committed native bundle is not canonical JSON")
    digest = hashlib.sha256(canonical).hexdigest()
    return bundle, BundleReference(
        name=bundle.capability.name,
        version=bundle.capability.version,
        digest=digest,
    )


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
    required = {"beta", "gamma", "delta"}
    if not required.issubset(result):
        raise RuntimeError("fresh seed did not provide required review fixtures")
    return result


def _run(path: Path) -> dict:
    if os.environ.get("CUA_NATIVE_REVIEW") != "1":
        raise RuntimeError("set CUA_NATIVE_REVIEW=1 to run the native reviewer demo")
    if not check_health():
        raise RuntimeError("pinned loopback ParaBank is not healthy; start it first")
    target = target_payload()
    bundle, _ = _load_bundle(path)
    credentials = temporary_credentials()
    previous = install_credentials(credentials)
    try:
        with tempfile.TemporaryDirectory(prefix="cua-native-review-") as temporary:
            root = Path(temporary)
            seed_path = seed(manifest_path=root / "seed_manifest.json")
            manifest = json.loads(seed_path.read_text(encoding="utf-8"))
            account_ids = _account_ids(manifest)
            forbidden = private_values(credentials, manifest)
            if contains_private_value(canonical_bundle_json(bundle), forbidden):
                raise RuntimeError("committed bundle contains a synthetic private value")

            registry = BundleRegistry(root / "registry")
            reference = registry.put_draft(bundle)
            with _provider_call_trap() as provider_calls:
                validation, browser_version = asyncio.run(
                    _run_native_validation(
                        registry=registry,
                        reference=reference,
                        account_id=account_ids["beta"],
                        origin=DEFAULT_ORIGIN,
                        target_revision=UPSTREAM_COMMIT,
                        evidence_root=root / "validation-evidence",
                    )
                )
                if validation.status is not InvocationStatus.SUCCESS:
                    raise RuntimeError("committed draft failed native beta validation")
                beta_backend = _backend_account(DEFAULT_ORIGIN, account_ids["beta"])
                assert_result_matches_backend(
                    {
                        "status": validation.status.value,
                        "outputs": {
                            key: value.get_secret_value()
                            for key, value in (validation.outputs or {}).items()
                        },
                    },
                    requested_account_id=account_ids["beta"],
                    backend_account=beta_backend,
                )
                qualification = registry.validate(
                    reference,
                    ValidationQualification(
                        bundle_digest=reference.digest,
                        validation_run_ref=validation.run_id,
                        runtime_fingerprint=current_runtime_fingerprint(),
                        browser_version=browser_version,
                        target_revision=UPSTREAM_COMMIT,
                        replay_passed=True,
                        independent_oracle_passed=True,
                    ),
                )
                approval = registry.approve(
                    reference,
                    reviewer_ref="reviewer_committed_bundle_demo",
                    reviewer_type="independent_reviewer",
                )
                counts, samples = asyncio.run(
                    _run_native_replays(
                        registry=registry,
                        reference=reference,
                        account_ids={"gamma": account_ids["gamma"], "delta": account_ids["delta"]},
                        origin=DEFAULT_ORIGIN,
                        target_revision=UPSTREAM_COMMIT,
                        evidence_root=root / "replay-evidence",
                        repetitions=1,
                    )
                )
                for alias, outputs in samples.items():
                    account = _backend_account(DEFAULT_ORIGIN, account_ids[alias])
                    for output in outputs:
                        assert_result_matches_backend(
                            {
                                "status": "SUCCESS",
                                "outputs": {"available_balance": output.get_secret_value(), "currency": "USD"},
                            },
                            requested_account_id=account_ids[alias],
                            backend_account=account,
                        )
            if provider_calls:
                raise RuntimeError("native reviewer called a model provider")
            return {
                "status": "PASS",
                "artifact": f"capability/{reference.name}@{reference.version}",
                "digest": f"sha256:{reference.digest}",
                "validation": "SUCCESS",
                "approval": "APPROVED_IN_TEMPORARY_REGISTRY",
                "replay_counts": counts,
                "provider_calls": len(provider_calls),
                "credentials": "removed",
                "qualification_browser": qualification.browser_version,
                "approval_digest": f"sha256:{approval.digest}",
                "target": target,
                "qualification": qualification.model_dump(mode="json"),
                "approval_record": {
                    "status": "APPROVED",
                    "scope": "temporary_registry",
                    "fingerprint_checked": True,
                    "digest": f"sha256:{approval.digest}",
                    "reviewer_type": approval.reviewer_type,
                },
                "independent_oracle_match": True,
            }
    finally:
        restore_environment(previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact",
        type=Path,
        default=ROOT / ARTIFACT_RELATIVE_PATH,
        help="value-safe committed capability bundle",
    )
    args = parser.parse_args(argv)
    try:
        result = _run(args.artifact)
    except Exception as error:
        print(f"native reviewer demo blocked: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
