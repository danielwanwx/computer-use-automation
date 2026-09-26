#!/usr/bin/env python3
"""Run the full live path: LLM discovery -> capability artifact -> no-model replay.

1. Seed four synthetic customers on the local ParaBank target.
2. Discovery: the LLM drives the real UI (observe -> decide -> act) until the
   goal "get the available balance of savings account <id>" is verified.
3. Compile the verified trace into ``get_savings_balance@1.0.0`` (DRAFT).
4. Validate the draft on a different customer against an independent oracle,
   then approve it (temporary registry).
5. Replay with the model removed: 10 runs across two unseen customers, with a
   deposit in between to prove values are read live, plus four error cases
   (another customer's account, unknown account, checking account, bad input).
6. Export value-safe evidence (``--no-export`` prints the results instead).

Requires ``CUA_LIVE=1`` and a healthy loopback target. ``--provider auto`` (default)
uses ``OPENAI_API_KEY`` if set, otherwise a signed-in Claude Code, Codex, or Cursor
CLI on this machine, so no API key is needed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


from cua.llm import (
    PROVIDER_MODES,
    DecisionProviderError,
    OpenAIResponsesDecisionBackend,
    resolve_decision_backend,
)
from cua.models.qualification import ValidationQualification
from cua.registry import BundleRegistry
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from cua.evidence.models import RunMetadata, RunMode, RunState
from cua.evidence.sink import EvidenceSink
from cua.execution.contracts import InvocationStatus
from cua.execution.gateway import ExecutionGateway
from cua.policy.engine import PolicyEngine
from cua.replay import ReplayRuntime
from cua.sessions import SessionManager
from cua.surface import PlaywrightSurface
from cua.verification import CompletionVerifier
from testbed.evaluator import assert_result_matches_backend, expected_available_balance
from testbed.parabank import DEFAULT_ORIGIN, UPSTREAM_COMMIT, check_health
from testbed.seed import deposit_savings_for_test, seed

from scripts.native_lifecycle_common import (
    provider_call_trap,
    contains_private_value,
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
from scripts.export_safe_evidence import export_records
from tests.test_discovery_native import (
    _backend_account,
    _execution_context,
    _principal_specs,
    _record_invocation_terminal,
    _run_native_discovery,
    _run_native_replays,
    _run_native_validation,
    _result_for_oracle,
)


def _require_explicit_opt_in() -> None:
    if os.environ.get("CUA_LIVE") != "1":
        raise RuntimeError(
            "set CUA_LIVE=1 to run the live discovery lifecycle (it reseeds the local target)"
        )


def _provider_call_trap():
    return provider_call_trap("no-model replay called a decision backend")


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


_NONEXISTENT_ACCOUNT_ID = "99999999"
_PROVIDER_EVIDENCE_MODES = {
    "openai": "openai_responses_api",
    "claude-code": "claude_code_cli_login",
    "codex": "codex_cli_login",
    "cursor": "cursor_agent_cli_login",
}
_REDACT_DIGITS = re.compile(r"[0-9]{3,}")

# Each replay case the caller can hit, with the result class it must produce.
# Business outcomes are legitimate answers; hard failures stop with a step ID.
# All cases run in order in one reused session, bracketed by normal replays.
_EXPECTED_OUTCOMES = {
    "own_savings_account_first": ("SUCCESS", None),
    "account_of_another_customer": ("BUSINESS_OUTCOME", "ACCOUNT_NOT_FOUND"),
    "account_does_not_exist": ("BUSINESS_OUTCOME", "ACCOUNT_NOT_FOUND"),
    "checking_not_savings": ("FAILURE", "PRECONDITION_FAILED"),
    "malformed_account_id": ("FAILURE", "INPUT_INVALID"),
    "own_savings_account_again": ("SUCCESS", None),
}


def _decision_record(number: int, request, reply, *, error: Exception | None = None) -> dict:
    """Keep what the model saw (already value-free) and what it chose, and why."""
    observation = request.observation
    observed = {
        "route": observation.route,
        "page_state": observation.page_state,
        "offered_controls": [
            {"control_ref": c.control_ref, "role": c.role, "safe_name": c.safe_name}
            for c in observation.controls
        ],
        "signals": request.signals.model_dump(mode="json"),
        "recent_history": [item.model_dump(mode="json") for item in request.recent],
    }
    if reply is None:
        return {
            "decision": number,
            "observed": observed,
            "rejected": {"code": getattr(error, "code", type(error).__name__)},
        }
    decision = reply.decision
    chosen = next(
        (
            control.safe_name
            for control in observation.controls
            if control.control_ref == getattr(decision, "control_ref", None)
        ),
        None,
    )
    return {
        "decision": number,
        "model_id": reply.model_id,
        "observed": observed,
        "chose": {
            "operation": decision.operation,
            "control_ref": getattr(decision, "control_ref", None),
            "control_safe_name": chosen,
            "reason_code": decision.reason_code,
            # Rationale is model text; digits are masked in case it echoes a value.
            "rationale": _REDACT_DIGITS.sub("<n>", decision.rationale),
        },
        "usage": {"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens},
    }


def _checking_account_id(manifest: dict, alias: str) -> str:
    for principal in manifest.get("principals", ()):
        if isinstance(principal, dict) and principal.get("alias") == alias:
            for account in principal.get("accounts", ()):
                if isinstance(account, dict) and account.get("type") == "CHECKING":
                    return str(account["account_id"])
    raise RuntimeError(f"synthetic seed has no checking account for {alias}")


async def _run_outcome_replays(
    *, registry, reference, cases, origin, target_revision, evidence_root
) -> list[dict]:
    """Replay the approved artifact with inputs that must not produce a balance."""
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
    results: list[dict] = []
    sessions: dict[str, tuple] = {}
    try:
        for case_name, principal_alias, account_id in cases:
            if principal_alias not in sessions:
                handle = await manager.prepare(principal_alias)
                sessions[principal_alias] = (handle, (await manager.get_state(handle.session_id)).actor)
            handle, actor = sessions[principal_alias]
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
            try:
                result = await runtime.run(
                    reference,
                    _execution_context(handle, actor, run_id, account_id),
                )
            finally:
                if actor.active_run_id == run_id:
                    await actor.finish_run(run_id)
            _record_invocation_terminal(evidence, run_id, result)
            failure = result.failure
            results.append(
                {
                    "case": case_name,
                    "run_alias": run_id,
                    "status": result.status.value,
                    "code": result.code.value if result.code is not None else None,
                    "failure": None
                    if failure is None
                    else {
                        "reason_code": failure.reason_code.value,
                        "step_id": failure.step_id,
                        "effect_state": failure.effect_state.value
                        if failure.effect_state is not None
                        else None,
                    },
                    "outputs_returned": bool(result.outputs),
                    "session_reused": len(results) > 0,
                }
            )
    finally:
        await manager.close_all()
    return results


def _assert_expected_outcomes(results: list[dict]) -> None:
    for result in results:
        status, code = _EXPECTED_OUTCOMES[result["case"]]
        observed_code = (result["failure"] or {}).get("reason_code") or result["code"]
        if result["status"] != status or result["outputs_returned"] != (status == "SUCCESS"):
            raise RuntimeError(f"outcome replay mismatch: {json.dumps(results, sort_keys=True)}")
        if code is not None and observed_code != code:
            raise RuntimeError(f"outcome replay mismatch: {json.dumps(results, sort_keys=True)}")


def _artifact_paths(version: str) -> tuple[str, str, str]:
    """Return the artifact, lifecycle summary, and run-log paths for one live run."""
    if version != "1.0.0":
        # ReplayRuntime pins the supported contract to 1.0.0; a new version needs
        # a reviewed runtime change, not a flag.
        raise ValueError("this runtime executes only get_savings_balance@1.0.0")
    return (
        f"artifacts/get_savings_balance-{version}.json",
        "evidence/live_lifecycle.json",
        "evidence/live_run",
    )


def _run_lifecycle(
    *,
    artifact_version: str | None = None,
    provider: str = "auto",
    model: str | None = None,
    export: bool = True,
) -> dict:
    _require_explicit_opt_in()
    artifact_version = artifact_version or "1.0.0"
    artifact_relative_path, evidence_relative_path, event_records_relative_path = _artifact_paths(artifact_version)
    if not check_health():
        raise RuntimeError("pinned loopback ParaBank is not healthy; start it first")
    target = target_payload()
    # Resolve the backend before the environment is scrubbed. An API key is read
    # once and held only by the discovery backend; replay runs with no key set.
    try:
        backend, provider = resolve_decision_backend(provider, model=model)
    except DecisionProviderError:
        raise RuntimeError(
            "no decision backend: set OPENAI_API_KEY or install and sign in to "
            "Claude Code (claude), Codex (codex), or Cursor (cursor-agent)"
        ) from None
    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("--provider openai requires OPENAI_API_KEY")
        backend = OpenAIResponsesDecisionBackend(backend.model_id, credential_provider=lambda: api_key)
    credentials = temporary_credentials()
    previous = install_credentials(credentials)
    try:
        with tempfile.TemporaryDirectory(prefix="cua-live-") as temporary:
            work_root = Path(temporary)
            seed_path = seed(manifest_path=work_root / "seed_manifest.json")
            manifest = json.loads(seed_path.read_text(encoding="utf-8"))
            account_ids = _account_ids(manifest)
            forbidden = private_values(credentials, manifest)

            provider_call_count = [0]
            original_choose = backend.choose

            decision_log: list[dict] = []

            async def counted_choose(request, *, timeout_seconds):
                provider_call_count[0] += 1
                try:
                    reply = await original_choose(request, timeout_seconds=timeout_seconds)
                except Exception as error:
                    # Rejected replies are part of the record: the loop discards
                    # them without acting and asks again.
                    decision_log.append(
                        _decision_record(len(decision_log) + 1, request, None, error=error)
                    )
                    raise
                decision_log.append(_decision_record(len(decision_log) + 1, request, reply))
                return reply

            # Instance-level wrapping keeps an exact live-call count and records
            # the value-free request the model saw plus its typed decision.
            backend.choose = counted_choose
            outcome, bundle, backend_account, provider_calls = asyncio.run(
                _run_native_discovery(
                    account_id=account_ids["alpha"],
                    origin=DEFAULT_ORIGIN,
                    target_revision=UPSTREAM_COMMIT,
                    deployment_root=work_root / "discovery-evidence",
                    decision_backend=backend,
                    reviewer_ref="reviewer_blueprint",
                )
            )
            if outcome.status.value != "SUCCESS" or outcome.trace is None:
                raise RuntimeError("live discovery did not produce a verified trace")
            if outcome.decisions_used <= 0 or provider_call_count[0] <= 0:
                raise RuntimeError("live discovery reported no provider decisions")
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
            bundle = bundle.model_copy(
                update={
                    "capability": bundle.capability.model_copy(
                        update={"version": artifact_version}
                    )
                }
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
                failure = validation.failure
                raise RuntimeError(
                    "isolated beta draft validation did not succeed: "
                    f"{validation.status.value} {validation.code} "
                    f"{failure.reason_code if failure else None} at {failure.step_id if failure else None}"
                )
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
                reviewer_ref="reviewer_live_lifecycle",
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
                outcome_cases = (
                    ("own_savings_account_first", "gamma", account_ids["gamma"]),
                    ("account_of_another_customer", "gamma", account_ids["delta"]),
                    ("account_does_not_exist", "gamma", _NONEXISTENT_ACCOUNT_ID),
                    ("checking_not_savings", "gamma", _checking_account_id(manifest, "gamma")),
                    ("malformed_account_id", "gamma", "12a45"),
                    ("own_savings_account_again", "gamma", account_ids["gamma"]),
                )
                outcome_results = asyncio.run(
                    _run_outcome_replays(
                        registry=registry,
                        reference=reference,
                        cases=outcome_cases,
                        origin=DEFAULT_ORIGIN,
                        target_revision=UPSTREAM_COMMIT,
                        evidence_root=work_root / "outcome-replay-evidence",
                    )
                )
            if provider_trap:
                raise RuntimeError("a model provider was called during no-model replay")
            _assert_expected_outcomes(outcome_results)
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
                work_root / "outcome-replay-evidence",
            )
            for evidence_root in evidence_roots:
                for path in evidence_root.rglob("*") if evidence_root.exists() else ():
                    if path.is_file() and contains_private_value(path.read_bytes(), forbidden):
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
            if not export:
                preview = {
                    "status": "PASS",
                    "exported": False,
                    "provider": {"mode": provider, "model_id": backend.model_id},
                    "artifact": {
                        "reference": f"capability/{reference.name}@{reference.version}",
                        "digest": f"sha256:{reference.digest}",
                        "steps": [
                            {"id": step.id, "kind": step.kind, "source": step.source.type}
                            for step in bundle.steps
                        ],
                    },
                    "discovery_decisions": decision_log,
                    "trace": {"decisions_used": trace_summary["decisions_used"], "step_ids": trace_summary["step_ids"]},
                    "validation": "SUCCESS",
                    "replay": replay_payload,
                    "outcome_replays": outcome_results,
                }
                if contains_private_value(json.dumps(preview).encode("utf-8"), forbidden):
                    raise RuntimeError("private synthetic value reached the printed summary")
                return preview
            exported = export_draft_artifact(
                bundle,
                forbidden_values=forbidden,
                trace_summary=trace_summary,
                target=target,
                qualification=qualification_payload,
                replay=replay_payload,
                artifact_relative_path=artifact_relative_path,
            )
            from scripts.native_lifecycle_common import source_fingerprint

            decisions_bytes = json.dumps(
                {"schema_version": 1, "trace_id": trace_summary["trace_id"], "decisions": decision_log},
                indent=2,
                sort_keys=True,
            ).encode("utf-8") + b"\n"
            if contains_private_value(decisions_bytes, forbidden):
                raise RuntimeError("private synthetic value reached the discovery decision log")

            event_records = export_records(
                {
                    "discovery": work_root / "discovery-evidence",
                    "validation": work_root / "validation-evidence",
                    "replay_before": work_root / "before-replay-evidence",
                    "replay_after": work_root / "after-replay-evidence",
                    "replay_outcomes": work_root / "outcome-replay-evidence",
                },
                ROOT / event_records_relative_path,
                source_kind="native_target",
                source_fingerprint=source_fingerprint(ROOT),
                artifact_digest=f"sha256:{exported.digest}",
                trace_binding={
                    "trace_id": trace_summary["trace_id"],
                    "event_ids": trace_summary["event_ids"],
                    "step_ids": trace_summary["step_ids"],
                },
                forbidden_values=forbidden,
            )
            decisions_path = ROOT / event_records_relative_path / "discovery_decisions.json"
            decisions_path.write_bytes(decisions_bytes)
            decisions_export = {
                "path": f"{event_records_relative_path}/discovery_decisions.json",
                "sha256": hashlib.sha256(decisions_bytes).hexdigest(),
                "count": len(decision_log),
            }
            evidence_payload = {
                "schema_version": 1,
                "case": "live_discovery_lifecycle",
                "status": "PASS",
                "target": target,
                "provider": {
                    "mode": _PROVIDER_EVIDENCE_MODES[provider],
                    "model_id": backend.model_id,
                    "discovery_decisions": provider_call_count[0],
                    "replay_provider_calls": 0,
                    "credentials": "discovery_only_removed_before_replay"
                    if provider == "openai"
                    else "local_cli_login_no_key_in_child",
                },
                "trace": trace_summary,
                "artifact": {
                    "reference": f"capability/{exported.name}@{exported.version}",
                    "digest": f"sha256:{exported.digest}",
                    "path": artifact_relative_path,
                    "provenance_types": sorted({step.source.type for step in bundle.steps}),
                },
                "validation": qualification_payload | {"status": "SUCCESS"},
                "approval": {
                    "status": "APPROVED_IN_TEMPORARY_REGISTRY",
                    "digest": f"sha256:{approval.digest}",
                    "sidecar_committed": False,
                },
                "replay": replay_payload,
                "evidence_path": evidence_relative_path,
                "event_records": {
                    "index_path": f"{event_records['path']}/index.json",
                    "index_sha256": event_records["sha256"],
                    "record_count": event_records["record_count"],
                },
                "discovery_decisions": decisions_export,
                "outcome_replays": outcome_results,
                "runtime_fingerprint": runtime_fingerprint_payload(),
            }
            export_evidence_manifest(evidence_payload, relative_path=evidence_relative_path)
            return {
                "status": "PASS",
                "artifact": evidence_payload["artifact"],
                "trace": {"event_count": trace_summary["event_count"], "decisions_used": trace_summary["decisions_used"]},
                "validation": "SUCCESS",
                "approval": "APPROVED_IN_TEMPORARY_REGISTRY",
                "replay": replay_payload,
                "outcome_replays": outcome_results,
                "discovery_decisions": decisions_export,
                "artifact_version": artifact_version,
            }
    finally:
        restore_environment(previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit a compact safe JSON result")
    parser.add_argument(
        "--no-export",
        action="store_true",
        help="run everything but print results instead of writing artifacts/ and evidence/",
    )
    parser.add_argument(
        "--artifact-version",
        help="version for this lifecycle artifact (only 1.0.0 is executable by this runtime)",
    )
    parser.add_argument(
        "--provider",
        choices=PROVIDER_MODES,
        default="auto",
        help="discovery backend. auto: OPENAI_API_KEY if set, else the first signed-in "
        "local agent CLI found (claude, codex, cursor-agent)",
    )
    parser.add_argument(
        "--model",
        help="model for an explicitly named provider (default: the provider's own default; "
        "OpenAI uses a pinned snapshot)",
    )
    args = parser.parse_args(argv)
    try:
        result = _run_lifecycle(
            artifact_version=args.artifact_version,
            provider=args.provider,
            model=args.model,
            export=not args.no_export,
        )
    except Exception as error:
        # Exception text may include a provider/target diagnostic.  Do not echo
        # subprocess output or request values; the safe command result is enough.
        print(f"live lifecycle blocked: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
