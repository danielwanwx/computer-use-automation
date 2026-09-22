#!/usr/bin/env python3
"""Emit the honest V1–V12 release matrix for this checkout.

The script runs only the offline automated suite by default.  Native target,
provider, clean-checkout, and real-person cases stay NOT_RUN until their
required evidence exists; the script never promotes those cases from a unit
test result to PASS.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from collections import defaultdict
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]

_OFFLINE_CASES = {"V3", "V4", "V5", "V6", "V7", "V8", "V10"}
_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "V1",
        "title": "real discovery and trace source",
        "required": "live provider-backed discovery with a trace-derived capability",
        "status": "NOT_RUN",
        "reason": "A Codex saved-login lifecycle diagnostic exists, but its temporary DRAFT artifact and uncommitted approval sidecar are not release evidence.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_discovery_native.py",
    },
    {
        "id": "V2",
        "title": "new inputs and changed balance",
        "required": "two native customers, five replay runs each, and an observed balance change",
        "status": "NOT_RUN",
        "reason": "The required native replay evidence is recorded under V2; provider-backed discovery remains separate.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_discovery_native.py",
    },
    {
        "id": "V3",
        "title": "zero-model replay",
        "required": "native replay and recovery with model credentials removed",
        "status": "NOT_RUN",
        "reason": "The live/native criterion remains pending; offline model-trap checks are reported separately.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_replay.py",
    },
    {
        "id": "V4",
        "title": "business and input boundaries",
        "required": "native account-not-found, access-denied, and missing-input outcomes",
        "status": "NOT_RUN",
        "reason": "Native target boundary cases have not been rerun for the release source.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_replay.py tests/test_completion_verifier.py",
    },
    {
        "id": "V5",
        "title": "wrong-success protection",
        "required": "native wrong-member, wrong-account, wrong-type, duplicate-target, and evaluator cases",
        "status": "NOT_RUN",
        "reason": "Native/evaluator release evidence is pending; offline verifier checks are reported separately.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_completion_verifier.py tests/test_replay.py",
    },
    {
        "id": "V6",
        "title": "recovery and stopping",
        "required": "native transient recovery, exhaustion, and lost-receipt behavior",
        "status": "NOT_RUN",
        "reason": "No final native fault-injection run is recorded.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_replay.py tests/test_execution_gateway.py",
    },
    {
        "id": "V7",
        "title": "control and capability validity",
        "required": "native stale/frame/condition checks and post-approval dependency invalidation",
        "status": "NOT_RUN",
        "reason": "The release matrix keeps native qualification separate from offline registry checks.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_bundle_registry.py tests/test_compiler.py",
    },
    {
        "id": "V8",
        "title": "takeover protocol",
        "required": "actor handoff races, stale epochs, and wrong-principal resume",
        "status": "NOT_RUN",
        "reason": "The application handoff protocol and UI are wired; native same-session takeover and manual evidence remain pending.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_handoff_service.py tests/test_sessions_actor.py",
    },
    {
        "id": "V9",
        "title": "real human takeover",
        "required": "a person operates the same browser session and resumes successfully",
        "status": "NOT_RUN",
        "reason": "Manual intervention was not performed.",
        "command": "manual same-session operator test",
    },
    {
        "id": "V10",
        "title": "policy and data protection",
        "required": "zero forbidden side effects, zero canary leakage, and usable safe evidence",
        "status": "NOT_RUN",
        "reason": "Native canary and forbidden-target release evidence is pending.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_policy.py tests/test_evidence_sink.py",
    },
    {
        "id": "V11",
        "title": "clean environment reproduction",
        "required": "fresh checkout setup and no-model replay without development state",
        "status": "NOT_RUN",
        "reason": "A clean-checkout/offline diagnostic is recorded, but native no-model replay from an approved real artifact is still missing.",
        "command": "uv sync --locked && .venv/bin/python -B -m pytest -p no:cacheprovider -q",
    },
    {
        "id": "V12",
        "title": "operator page",
        "required": "browser UI from discovery through approval, replay, result, and takeover",
        "status": "NOT_RUN",
        "reason": "HTTP contract checks pass, but browser end-to-end and takeover UI evidence are not recorded.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_web_app.py tests/test_cli.py",
    },
)


def _files_for_fingerprint(root: Path) -> list[Path]:
    paths = [root / "pyproject.toml", root / "uv.lock"]
    for directory in (root / "src", root / "testbed"):
        if not directory.exists():
            continue
        paths.extend(
            path
            for path in directory.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return sorted(path for path in paths if path.is_file())


def source_fingerprint(root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    for path in _files_for_fingerprint(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def runtime_fingerprint(root: Path = ROOT) -> dict[str, Any]:
    source_path = str(root / "src")
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    try:
        from cua.registry.runtime_fingerprint import current_runtime_fingerprint

        return {
            "status": "AVAILABLE",
            **current_runtime_fingerprint().model_dump(mode="json"),
        }
    except Exception:
        return {"status": "UNAVAILABLE", "reason": "runtime fingerprint import failed"}


def offline_test(root: Path = ROOT, *, run: bool = True) -> dict[str, Any]:
    python = root / ".venv" / "bin" / "python"
    executable = str(python if python.is_file() else Path(sys.executable))
    command = [
        executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
    ]
    if not run:
        return {"status": "NOT_RUN", "command": " ".join(command), "exit_code": None}
    environment = dict(__import__("os").environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
            check=False,
        )
        return {
            "status": "PASS" if completed.returncode == 0 else "FAIL",
            "command": " ".join(command),
            "exit_code": completed.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "FAIL", "command": " ".join(command), "exit_code": "TIMEOUT"}
    except OSError:
        return {"status": "FAIL", "command": " ".join(command), "exit_code": "UNAVAILABLE"}


def _path_status(root: Path, relative: str) -> dict[str, Any]:
    return {"path": relative, "exists": (root / relative).exists()}


def _release_evidence(root: Path) -> dict[str, Any]:
    path = root / "evidence/index.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"schema_version": 1, "entries": []}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return {"schema_version": 1, "entries": []}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return {"schema_version": 1, "entries": []}
    return {**payload, "entries": [entry for entry in entries if isinstance(entry, dict)]}


def _case_evidence_is_current(
    case_id: str,
    evidence: dict[str, Any],
    current_source_fingerprint: str,
    *,
    root: Path = ROOT,
) -> bool:
    """Accept only a recognized, complete case record bound to this source tree."""
    validator = _CASE_EVIDENCE_VALIDATORS.get(case_id)
    if validator is None:
        return False
    if case_id == "V1":
        return _validate_v1(evidence, current_source_fingerprint, root=root)
    if case_id == "V11":
        return _validate_v11(evidence, current_source_fingerprint, root=root)
    return validator(evidence, current_source_fingerprint)


def _common_evidence_is_current(
    evidence: Mapping[str, Any],
    *,
    case_id: str,
    source_fingerprint: str,
    evidence_type: str,
) -> bool:
    """Validate fields shared by every case-specific evidence schema."""
    return (
        evidence.get("case_id") == case_id
        and evidence.get("status") == "PASS"
        and evidence.get("source_fingerprint") == source_fingerprint
        and evidence.get("evidence_type") == evidence_type
        and isinstance(evidence.get("recorded_on"), str)
    )


def _passed_test(evidence: Mapping[str, Any]) -> bool:
    test = evidence.get("test")
    return isinstance(test, Mapping) and test.get("passed") is True


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_canonical_json(path: Path) -> tuple[bytes, Mapping[str, Any]] | None:
    """Read one canonical JSON object without accepting a rewritten variant."""
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, Mapping):
            return None
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if raw != canonical:
        return None
    return raw, value


def _safe_release_path(root: Path, relative: Any) -> Path | None:
    """Resolve a release path only when it is a regular file below ``root``."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None
    candidate = root / relative
    try:
        root_resolved = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if candidate.is_symlink() or not resolved.is_file():
        return None
    if root_resolved == resolved or root_resolved not in resolved.parents:
        return None
    return resolved


def _sidecar_is_bound(
    *,
    root: Path,
    artifact: Mapping[str, Any],
    artifact_path: Path,
    artifact_digest: str,
    capability_version: Any,
    validation: Any,
) -> bool:
    """Require a real, canonical approval record when approval is claimed."""
    if artifact.get("sidecar_committed") is not True:
        return False
    sidecar_relative = artifact.get("approval_sidecar_path")
    if sidecar_relative is None:
        sidecar_relative = artifact.get("sidecar_path")
    if sidecar_relative is None:
        sidecar_relative = f"{artifact_path.relative_to(root.resolve())}.approval.json"
    sidecar_path = _safe_release_path(root, sidecar_relative)
    if sidecar_path is None:
        return False
    loaded = _read_canonical_json(sidecar_path)
    if loaded is None:
        return False
    _, sidecar = loaded
    sidecar_digest = sidecar.get("digest")
    if isinstance(sidecar_digest, str) and sidecar_digest.startswith("sha256:"):
        sidecar_digest = sidecar_digest.removeprefix("sha256:")
    validation_run_ref = (
        validation.get("validation_run_ref")
        if isinstance(validation, Mapping)
        else None
    )
    return (
        isinstance(sidecar_digest, str)
        and f"sha256:{sidecar_digest}" == artifact_digest
        and sidecar.get("capability_version") == capability_version
        and _nonempty_string(sidecar.get("validation_run_ref"))
        and sidecar.get("validation_run_ref") == validation_run_ref
        and _nonempty_string(sidecar.get("reviewer_ref"))
        and sidecar.get("reviewer_type") in {"independent_reviewer", "operator"}
        and _nonempty_string(sidecar.get("approved_at"))
    )


def _mapping_with_true_flags(
    evidence: Mapping[str, Any],
    field: str,
    *flags: str,
) -> bool:
    values = evidence.get(field)
    return isinstance(values, Mapping) and all(values.get(flag) is True for flag in flags)


def _validate_v1(
    evidence: Mapping[str, Any],
    source_fingerprint: str,
    *,
    root: Path = ROOT,
) -> bool:
    trace = evidence.get("trace")
    capability = evidence.get("capability")
    provider = evidence.get("provider")
    target = evidence.get("target")
    artifact = evidence.get("artifact")
    provenance_types = (
        artifact.get("provenance_types") if isinstance(artifact, Mapping) else None
    )
    artifact_digest = artifact.get("digest") if isinstance(artifact, Mapping) else None
    artifact_reference = artifact.get("reference") if isinstance(artifact, Mapping) else None
    artifact_path = (
        _safe_release_path(root, artifact.get("path"))
        if isinstance(artifact, Mapping)
        else None
    )
    artifact_loaded = (
        _read_canonical_json(artifact_path) if artifact_path is not None else None
    )
    artifact_payload = artifact_loaded[1] if artifact_loaded is not None else None
    payload_capability = (
        artifact_payload.get("capability")
        if isinstance(artifact_payload, Mapping)
        else None
    )
    payload_provenance = (
        artifact_payload.get("provenance")
        if isinstance(artifact_payload, Mapping)
        else None
    )
    payload_steps = artifact_payload.get("steps") if isinstance(artifact_payload, Mapping) else None
    payload_source_types = (
        {
            source.get("type")
            for step in payload_steps
            if isinstance(step, Mapping)
            and isinstance(source := step.get("source"), Mapping)
            and isinstance(source.get("type"), str)
        }
        if isinstance(payload_steps, list)
        else set()
    )
    payload_observed_steps = (
        sum(
            1
            for step in payload_steps
            if isinstance(step, Mapping)
            and isinstance(source := step.get("source"), Mapping)
            and source.get("type") == "observed"
        )
        if isinstance(payload_steps, list)
        else 0
    )
    payload_digest = (
        f"sha256:{hashlib.sha256(artifact_loaded[0]).hexdigest()}"
        if artifact_loaded is not None
        else None
    )
    validation = evidence.get("validation")
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V1",
            source_fingerprint=source_fingerprint,
            evidence_type="live_discovery",
        )
        and _passed_test(evidence)
        and isinstance(trace, Mapping)
        and _positive_int(trace.get("step_count"))
        and trace.get("verified") is True
        and trace.get("completion_proof_present") is True
        and trace.get("derived_capability") is True
        and isinstance(capability, Mapping)
        and capability.get("trace_derived") is True
        and _nonempty_string(capability.get("reference"))
        and capability.get("reference") == artifact_reference
        and artifact_digest == capability.get("digest")
        and isinstance(artifact, Mapping)
        and _nonempty_string(artifact_reference)
        and isinstance(artifact_digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest, re.ASCII) is not None
        and artifact.get("lifecycle") == "APPROVED"
        and artifact_path is not None
        and artifact_loaded is not None
        and payload_digest == artifact_digest
        and isinstance(payload_capability, Mapping)
        and payload_capability.get("name") == capability.get("reference", "").removeprefix("capability/").split("@", 1)[0]
        and payload_capability.get("version") == artifact_reference.rsplit("@", 1)[-1]
        and isinstance(payload_provenance, Mapping)
        and payload_provenance.get("verified") is True
        and payload_provenance.get("trace_id") == trace.get("trace_id")
        and _nonempty_string(payload_provenance.get("completion_proof_id"))
        and isinstance(payload_steps, list)
        and payload_observed_steps == trace.get("step_count")
        and {"observed", "declared", "reviewer_added"}.issubset(payload_source_types)
        and isinstance(provenance_types, list)
        and {"observed", "declared", "reviewer_added"}.issubset(provenance_types)
        and artifact.get("trace_id") == trace.get("trace_id")
        and _sidecar_is_bound(
            root=root,
            artifact=artifact,
            artifact_path=artifact_path,
            artifact_digest=artifact_digest,
            capability_version=payload_capability.get("version"),
            validation=validation,
        )
        and isinstance(provider, Mapping)
        and provider.get("mode") in {"live", "codex_saved_login"}
        and _positive_int(provider.get("provider_calls"))
        and provider.get("credentials") in {"removed", "removed_from_provider_child"}
        and isinstance(target, Mapping)
        and target.get("kind") == "parabank"
        and target.get("origin") == "http://127.0.0.1:8080/parabank"
        and target.get("upstream_commit") == "ee82474be5f58bea3ddc8be0fd831072b00201cb"
        and target.get("loopback") is True
        and _nonempty_string(target.get("war_sha256"))
    )


def _validate_v2(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    criterion = evidence.get("criterion")
    test = evidence.get("test")
    replay = evidence.get("replay")
    provider = evidence.get("provider")
    target = evidence.get("target")
    safety = evidence.get("safety")
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V2",
            source_fingerprint=source_fingerprint,
            evidence_type="native_loopback",
        )
        and isinstance(criterion, Mapping)
        and criterion.get("native_customers") == 2
        and criterion.get("replays_per_customer") == 5
        and criterion.get("replay_count") == 10
        and criterion.get("successful_replays") == 10
        and criterion.get("balance_change_observed") is True
        and criterion.get("all_outputs_match_independent_oracle") is True
        and isinstance(test, Mapping)
        and test.get("nodeid")
        == "tests/test_discovery_native.py::test_native_offline_discovery_validation_approval_and_cross_client_replay"
        and test.get("passed") is True
        and isinstance(replay, Mapping)
        and replay.get("discovery") == "SUCCESS"
        and replay.get("draft_validation") == "SUCCESS"
        and replay.get("approval") == "APPROVED"
        and replay.get("cross_client") is True
        and isinstance(provider, Mapping)
        and provider.get("mode") == "offline_scripted_test_backend"
        and provider.get("credentials") == "removed"
        and provider.get("provider_calls") == 0
        and isinstance(target, Mapping)
        and target.get("origin") == "http://127.0.0.1:8080/parabank"
        and target.get("upstream_commit") == "ee82474be5f58bea3ddc8be0fd831072b00201cb"
        and target.get("loopback") is True
        and isinstance(safety, Mapping)
        and safety.get("private_values_persisted") is False
    )


def _validate_v3(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V3",
            source_fingerprint=source_fingerprint,
            evidence_type="native_no_model",
        )
        and _passed_test(evidence)
        and _mapping_with_true_flags(
            evidence,
            "criterion",
            "normal_replay",
            "recovery_passed",
        )
        and isinstance(evidence.get("criterion"), Mapping)
        and evidence["criterion"].get("model_calls") == 0
        and isinstance(evidence.get("provider"), Mapping)
        and evidence["provider"].get("credentials") == "removed"
        and evidence["provider"].get("provider_calls") == 0
    )


def _validate_v4(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V4",
            source_fingerprint=source_fingerprint,
            evidence_type="native_boundaries",
        )
        and _passed_test(evidence)
        and _mapping_with_true_flags(
            evidence,
            "criterion",
            "account_not_found",
            "access_denied",
            "missing_account_id",
            "no_balance_leak",
        )
    )


def _validate_v5(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V5",
            source_fingerprint=source_fingerprint,
            evidence_type="native_wrong_success",
        )
        and _passed_test(evidence)
        and _mapping_with_true_flags(
            evidence,
            "criterion",
            "wrong_member",
            "wrong_account",
            "wrong_type",
            "duplicate_target",
            "evaluator_rejects_wrong_result",
        )
    )


def _validate_v6(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V6",
            source_fingerprint=source_fingerprint,
            evidence_type="native_recovery",
        )
        and _passed_test(evidence)
        and _mapping_with_true_flags(
            evidence,
            "criterion",
            "transient_recovered",
            "exhausted_stops",
            "unknown_effect_not_retried",
        )
    )


def _validate_v7(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V7",
            source_fingerprint=source_fingerprint,
            evidence_type="native_validity",
        )
        and _passed_test(evidence)
        and _mapping_with_true_flags(
            evidence,
            "criterion",
            "stale_observation_rejected",
            "wrong_frame_rejected",
            "undefined_condition_rejected",
            "approval_invalidated_after_dependency_change",
        )
    )


def _validate_v8(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V8",
            source_fingerprint=source_fingerprint,
            evidence_type="native_handoff_protocol",
        )
        and _passed_test(evidence)
        and _mapping_with_true_flags(
            evidence,
            "criterion",
            "in_flight_drained",
            "duplicate_claim_rejected",
            "stale_epoch_rejected",
            "wrong_principal_rejected",
        )
    )


def _validate_v9(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V9",
            source_fingerprint=source_fingerprint,
            evidence_type="manual_same_session_takeover",
        )
        and isinstance(evidence.get("operator"), Mapping)
        and evidence["operator"].get("person_performed") is True
        and _mapping_with_true_flags(
            evidence,
            "criterion",
            "same_session",
            "resumed_successfully",
        )
    )


def _validate_v10(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    criterion = evidence.get("criterion")
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V10",
            source_fingerprint=source_fingerprint,
            evidence_type="native_safety",
        )
        and _passed_test(evidence)
        and isinstance(criterion, Mapping)
        and criterion.get("forbidden_side_effects") == 0
        and criterion.get("canary_leaks") == 0
        and criterion.get("richer_failure_evidence") is True
    )


def _validate_v11_review_file(
    evidence: Mapping[str, Any],
    source_fingerprint: str,
    *,
    root: Path,
) -> bool:
    review_path = _safe_release_path(root, evidence.get("review_evidence_path"))
    loaded = _read_canonical_json(review_path) if review_path is not None else None
    review = loaded[1] if loaded is not None else None
    if not isinstance(review, Mapping):
        return False
    artifact = review.get("approved_artifact")
    target = review.get("native_target")
    qualification = review.get("qualification")
    replay = review.get("no_model_replay")
    artifact_path = (
        _safe_release_path(root, artifact.get("path"))
        if isinstance(artifact, Mapping)
        else None
    )
    artifact_loaded = (
        _read_canonical_json(artifact_path) if artifact_path is not None else None
    )
    artifact_digest = artifact.get("digest") if isinstance(artifact, Mapping) else None
    artifact_file_digest = (
        f"sha256:{hashlib.sha256(artifact_loaded[0]).hexdigest()}"
        if artifact_loaded is not None
        else None
    )
    current_runtime = runtime_fingerprint(root)
    recorded_runtime = (
        qualification.get("runtime_fingerprint")
        if isinstance(qualification, Mapping)
        else None
    )
    return (
        _common_evidence_is_current(
            review,
            case_id="V11",
            source_fingerprint=source_fingerprint,
            evidence_type="clean_checkout",
        )
        and isinstance(review.get("checkout"), Mapping)
        and review["checkout"].get("clean_worktree") is True
        and review["checkout"].get("development_state") == "not_used"
        and review["checkout"].get("lockfile") == "uv.lock"
        and review["checkout"].get("lockfile_sha256")
        == hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
        and isinstance(target, Mapping)
        and target.get("prepared") is True
        and target.get("loopback") is True
        and target.get("pinned_revision")
        == "ee82474be5f58bea3ddc8be0fd831072b00201cb"
        and _nonempty_string(target.get("browser_version"))
        and target.get("provider_credentials") == "unset"
        and isinstance(artifact, Mapping)
        and artifact.get("approved") is True
        and artifact.get("approval_scope") == "temporary_registry"
        and artifact.get("fingerprint_checked") is True
        and artifact.get("sidecar_committed") is False
        and artifact.get("lifecycle") == "DRAFT"
        and artifact_path is not None
        and artifact_path.relative_to(root.resolve()).as_posix()
        == "artifacts/get_savings_balance-1.0.0.json"
        and artifact.get("reference") == "capability/get_savings_balance@1.0.0"
        and isinstance(artifact_digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest, re.ASCII)
        and artifact_file_digest == artifact_digest
        and isinstance(qualification, Mapping)
        and isinstance(recorded_runtime, Mapping)
        and current_runtime.get("status") == "AVAILABLE"
        and all(
            recorded_runtime.get(field) == current_runtime.get(field)
            for field in (
                "source_sha256",
                "parser_sha256",
                "condition_sha256",
                "profile_sha256",
            )
        )
        and qualification.get("bundle_digest") == artifact_digest
        and qualification.get("target_revision")
        == "ee82474be5f58bea3ddc8be0fd831072b00201cb"
        and qualification.get("replay_passed") is True
        and qualification.get("independent_oracle_passed") is True
        and isinstance(replay, Mapping)
        and replay.get("command")
        == "CUA_NATIVE_REVIEW=1 .venv/bin/python -B scripts/review_native_bundle.py --artifact artifacts/get_savings_balance-1.0.0.json"
        and replay.get("artifact_reference") == artifact.get("reference")
        and replay.get("artifact_digest") == artifact_digest
        and replay.get("model_calls") == 0
        and isinstance(replay.get("validation"), Mapping)
        and replay["validation"].get("principal") == "beta"
        and replay["validation"].get("status") == "SUCCESS"
        and replay["validation"].get("independent_oracle_match") is True
        and isinstance(replay.get("approval"), Mapping)
        and replay["approval"].get("status") == "APPROVED"
        and replay["approval"].get("scope") == "temporary_registry"
        and replay["approval"].get("fingerprint_checked") is True
        and replay.get("replay_counts") == {"delta": 1, "gamma": 1}
        and isinstance(replay.get("result"), Mapping)
        and replay["result"].get("passed") is True
        and replay["result"].get("exit_code") == 0
        and replay.get("provider_calls") == 0
        and replay.get("credentials") == "removed"
        and review.get("independent_oracle_match") is True
    )


def _validate_v11(
    evidence: Mapping[str, Any],
    source_fingerprint: str,
    *,
    root: Path = ROOT,
) -> bool:
    commands = evidence.get("commands")
    result = evidence.get("result")
    checkout = evidence.get("checkout")
    native_target = evidence.get("native_target")
    approved_artifact = evidence.get("approved_artifact")
    no_model_replay = evidence.get("no_model_replay")
    artifact_reference = (
        approved_artifact.get("reference")
        if isinstance(approved_artifact, Mapping)
        else None
    )
    artifact_digest = (
        approved_artifact.get("digest")
        if isinstance(approved_artifact, Mapping)
        else None
    )
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V11",
            source_fingerprint=source_fingerprint,
            evidence_type="clean_checkout",
        )
        and commands == [
            "uv sync --locked",
            "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -p no:cacheprovider -q",
        ]
        and isinstance(result, Mapping)
        and result.get("passed") == 268
        and result.get("skipped") == 2
        and result.get("failed") == 0
        and result.get("model_credentials") == "removed"
        and result.get("replay_assertions") is True
        and result.get("provider_calls") == 0
        and isinstance(checkout, Mapping)
        and checkout.get("fresh_clone") is True
        and checkout.get("development_state") == "not_used"
        and checkout.get("lockfile") == "uv.lock"
        # Clean checkout setup alone is a diagnostic. V11 also needs a real
        # native target and an approved artifact replayed without a model.
        and isinstance(native_target, Mapping)
        and native_target.get("prepared") is True
        and native_target.get("loopback") is True
        and _nonempty_string(native_target.get("pinned_revision"))
        and isinstance(approved_artifact, Mapping)
        and approved_artifact.get("approved") is True
        and _nonempty_string(artifact_reference)
        and _nonempty_string(artifact_digest)
        and isinstance(no_model_replay, Mapping)
        and _nonempty_string(no_model_replay.get("command"))
        and no_model_replay.get("artifact_reference") == artifact_reference
        and no_model_replay.get("artifact_digest") == artifact_digest
        and isinstance(no_model_replay.get("result"), Mapping)
        and no_model_replay["result"].get("passed") is True
        and no_model_replay["result"].get("exit_code") == 0
        and no_model_replay.get("provider_calls") == 0
        and no_model_replay.get("credentials") == "removed"
        and evidence.get("independent_oracle_match") is True
        and _validate_v11_review_file(evidence, source_fingerprint, root=root)
    )


def _validate_v12(evidence: Mapping[str, Any], source_fingerprint: str) -> bool:
    return (
        _common_evidence_is_current(
            evidence,
            case_id="V12",
            source_fingerprint=source_fingerprint,
            evidence_type="browser_operator",
        )
        and _passed_test(evidence)
        and _mapping_with_true_flags(
            evidence,
            "criterion",
            "discovery_to_approval",
            "replay",
            "unapproved_blocked",
            "duplicate_blocked",
            "handoff_ui",
            "cli_consistent",
        )
    )


_CASE_EVIDENCE_VALIDATORS: dict[
    str, Callable[[Mapping[str, Any], str], bool]
] = {
    "V1": _validate_v1,
    "V2": _validate_v2,
    "V3": _validate_v3,
    "V4": _validate_v4,
    "V5": _validate_v5,
    "V6": _validate_v6,
    "V7": _validate_v7,
    "V8": _validate_v8,
    "V9": _validate_v9,
    "V10": _validate_v10,
    "V11": _validate_v11,
    "V12": _validate_v12,
}


def build_report(
    root: Path = ROOT,
    *,
    run_tests: bool = True,
    strict: bool = False,
    submission: bool = False,
) -> dict[str, Any]:
    """Build either a diagnostic report or a submission-readiness report.

    Diagnostic mode preserves ``NOT_RUN`` for missing evidence.  Strict mode
    changes those unresolved cases to ``BLOCKED`` and requires every case to
    have valid, fingerprinted evidence before the report can be submitted.
    ``submission`` is an explicit alias for ``strict`` for callers that prefer
    the release terminology.
    """
    submission_mode = strict or submission
    checks = offline_test(root, run=run_tests)
    current_source_fingerprint = source_fingerprint(root)
    release_evidence = _release_evidence(root)
    evidence_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in release_evidence["entries"]:
        case_id = entry.get("case_id")
        if isinstance(case_id, str) and case_id in _CASE_EVIDENCE_VALIDATORS:
            evidence_by_case[case_id].append(entry)
    cases: list[dict[str, Any]] = []
    for original in _CASES:
        case = dict(original)
        case_id = case["id"]
        case["offline_check"] = (
            checks["status"] if case_id in _OFFLINE_CASES else "NOT_APPLICABLE"
        )
        entries = evidence_by_case.get(case_id, [])
        evidence_is_current = len(entries) == 1 and _case_evidence_is_current(
            case_id,
            entries[0],
            current_source_fingerprint,
            root=root,
        )
        explicit_failure = any(entry.get("status") == "FAIL" for entry in entries)
        if explicit_failure:
            case["status"] = "FAIL"
            case["status_basis"] = "Case-specific evidence explicitly reports FAIL."
        elif evidence_is_current:
            case["status"] = "PASS"
            case["status_basis"] = "Case-specific evidence manifest matches this source fingerprint."
        else:
            # An aggregate offline suite cannot prove a case-specific native or
            # manual acceptance criterion. In diagnostic mode retain NOT_RUN;
            # submission mode turns the same unresolved state into BLOCKED.
            case["status"] = "BLOCKED" if submission_mode else "NOT_RUN"
            if entries:
                case["status_basis"] = (
                    "Evidence was rejected by the case-specific schema, fingerprint, "
                    "or uniqueness check."
                )
            else:
                case["status_basis"] = "No matching case-specific release evidence manifest is present."
        if entries:
            first_entry = entries[0]
            case["evidence"] = {
                "case_id": case_id,
                "source_fingerprint": first_entry.get("source_fingerprint"),
                "evidence_type": first_entry.get("evidence_type"),
                "entry_count": len(entries),
                "validation": "PASS" if evidence_is_current else "REJECTED",
            }
        cases.append(case)
    offline_failed = checks["status"] == "FAIL"
    case_failures = sum(case["status"] == "FAIL" for case in cases)
    unresolved = sum(case["status"] in {"NOT_RUN", "BLOCKED"} for case in cases)
    submission_ready = not offline_failed and case_failures == 0 and unresolved == 0
    summary: dict[str, int] = {
        "fail": case_failures + int(offline_failed),
        "not_run": sum(case["status"] == "NOT_RUN" for case in cases),
        "pass": sum(case["status"] == "PASS" for case in cases),
    }
    if submission_mode:
        summary["blocked"] = sum(case["status"] == "BLOCKED" for case in cases)
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "submission" if submission_mode else "report",
        "submission_status": "READY" if submission_ready else "BLOCKED",
        "submission_ready": submission_ready,
        "source_fingerprint": current_source_fingerprint,
        "runtime_fingerprint": runtime_fingerprint(root),
        "offline_check": checks,
        "commands": {
            "setup": "uv sync --locked",
            "offline_tests": checks["command"],
            "native_prepare": ".venv/bin/python -m testbed.parabank prepare",
            "native_start": ".venv/bin/python -m testbed.parabank start",
            "native_seed": ".venv/bin/python -m testbed.parabank seed",
            "server": ".venv/bin/cua serve",
        },
        "fixture_paths": [
            _path_status(root, "testbed/fixtures/session_manifest.json"),
            _path_status(root, "testbed/.cache/deployment_manifest.json"),
            _path_status(root, "testbed/.cache/seed_manifest.json"),
        ],
        "evidence_paths": [
            _path_status(root, "artifacts/index.json"),
            _path_status(root, "evidence/index.json"),
            {"path": "testbed/.cache/", "exists": (root / "testbed/.cache").is_dir()},
        ],
        "release_evidence": {
            "path": "evidence/index.json",
            "schema_version": release_evidence.get("schema_version"),
            "entries": len(release_evidence["entries"]),
        },
        "cases": cases,
        "summary": summary,
    }


def _exit_code(report: Mapping[str, Any]) -> int:
    """Return a nonzero code for failed checks or an unready submission."""
    summary = report.get("summary")
    if not isinstance(summary, Mapping) or summary.get("fail", 0):
        return 1
    if report.get("mode") == "submission" and not report.get("submission_ready", False):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="report the matrix without running the offline pytest command",
    )
    parser.add_argument(
        "--strict",
        "--submission",
        dest="submission",
        action="store_true",
        help="require every V1–V12 case to have valid evidence; unresolved cases block submission",
    )
    args = parser.parse_args(argv)
    report = build_report(
        ROOT,
        run_tests=not args.skip_tests,
        submission=args.submission,
    )
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
