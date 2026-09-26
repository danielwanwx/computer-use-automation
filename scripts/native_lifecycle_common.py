"""Shared value-safe helpers for the opt-in native lifecycle commands.

This module deliberately contains no provider code.  The live runner supplies a
decision backend; the reviewer command imports only these helpers and therefore
cannot accidentally make a model call.
"""

from __future__ import annotations

import hashlib
import re
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from typing import Any, Iterable, Mapping

from cua.models.bundles import CapabilityBundle, BundleReference
from cua.models.traces import VerifiedDiscoveryTrace
from cua.registry.bundle_registry import canonical_bundle_json
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from testbed.parabank import DEFAULT_ORIGIN, UPSTREAM_COMMIT


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_ROOT = ROOT / "artifacts"
EVIDENCE_ROOT = ROOT / "evidence"
ARTIFACT_RELATIVE_PATH = "artifacts/get_savings_balance-1.0.0.json"
EVIDENCE_RELATIVE_PATH = "evidence/live_lifecycle.json"
_PROVIDER_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_FEDERATION_RULE_ID",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "LLM_API_KEY",
)


def temporary_credentials() -> dict[str, str]:
    """Return short synthetic fixture values without persisting them."""

    return {
        "PARABANK_DEMO_ALPHA_USERNAME": "codex_a_" + secrets.token_hex(3),
        "PARABANK_DEMO_ALPHA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_BETA_USERNAME": "codex_b_" + secrets.token_hex(3),
        "PARABANK_DEMO_BETA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_GAMMA_USERNAME": "codex_g_" + secrets.token_hex(3),
        "PARABANK_DEMO_GAMMA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_DELTA_USERNAME": "codex_d_" + secrets.token_hex(3),
        "PARABANK_DEMO_DELTA_PASSWORD": secrets.token_hex(8),
    }


def install_credentials(credentials: Mapping[str, str]) -> dict[str, str | None]:
    """Set synthetic credentials and return the previous values for restoration."""

    previous: dict[str, str | None] = {}
    for name in _PROVIDER_ENV_NAMES:
        previous[name] = os.environ.get(name)
        os.environ.pop(name, None)
    for name, value in credentials.items():
        previous[name] = os.environ.get(name)
        os.environ[name] = value
    previous["PARABANK_ORIGIN"] = os.environ.get("PARABANK_ORIGIN")
    os.environ["PARABANK_ORIGIN"] = DEFAULT_ORIGIN
    return previous


def restore_environment(previous: Mapping[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def principal_specs():
    # Keep this fixture definition in the native test helper's shape.  The
    # runtime still obtains each secret from its named environment variable.
    from cua.sessions import PrincipalSpec

    return tuple(
        PrincipalSpec(
            alias,
            f"PARABANK_DEMO_{alias.upper()}_USERNAME",
            f"PARABANK_DEMO_{alias.upper()}_PASSWORD",
            f"Synthetic {alias.title()}",
        )
        for alias in ("alpha", "beta", "gamma", "delta")
    )


def private_values(credentials: Mapping[str, str], manifest: Mapping[str, Any]) -> tuple[bytes, ...]:
    """Collect values that must never occur in committed trace/artifact files."""

    values = {str(value) for value in credentials.values() if value}
    for principal in manifest.get("principals", ()):
        if not isinstance(principal, Mapping):
            continue
        if principal.get("customer_id") is not None:
            values.add(str(principal["customer_id"]))
        for account in principal.get("accounts", ()):
            if isinstance(account, Mapping) and account.get("account_id") is not None:
                values.add(str(account["account_id"]))
    return tuple(sorted((value.encode("utf-8") for value in values if value), key=len, reverse=True))


def safe_bundle_bytes(
    bundle: CapabilityBundle,
    *,
    forbidden_values: Iterable[bytes] = (),
) -> tuple[bytes, dict[str, Any]]:
    """Return canonical bundle bytes after provenance and value-leak checks."""

    canonical = canonical_bundle_json(bundle)
    payload = json.loads(canonical.decode("utf-8"))
    source_types = {
        step.get("source", {}).get("type")
        for step in payload.get("steps", ())
        if isinstance(step, Mapping)
    }
    if not {"observed", "declared", "reviewer_added"}.issubset(source_types):
        raise ValueError("compiled artifact does not preserve all required provenance types")
    if contains_private_value(canonical, forbidden_values):
        raise ValueError("private synthetic value reached a persisted artifact")
    return canonical, payload


def safe_trace_summary(
    trace: VerifiedDiscoveryTrace,
    *,
    decision_count: int,
    model_id: str,
) -> dict[str, Any]:
    """Build a value-free trace summary suitable for committed evidence."""

    return {
        "trace_id": trace.trace_id,
        "verified": trace.success is True and trace.completion_proof is not None,
        "completion_proof_present": trace.completion_proof is not None,
        "event_count": len(trace.events),
        "event_ids": [event.event_id for event in trace.events],
        "step_ids": [event.step.id for event in trace.events],
        "effect_states": [event.effect_state for event in trace.events],
        "source_types": [event.step.source.type for event in trace.events],
        "decisions_used": decision_count,
        "model_id": model_id,
    }


def runtime_fingerprint_payload() -> dict[str, str]:
    fingerprint = current_runtime_fingerprint()
    return fingerprint.model_dump(mode="json")


def target_payload() -> dict[str, Any]:
    deployment_path = ROOT / "testbed/.cache/deployment_manifest.json"
    try:
        deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("pinned deployment manifest is unavailable") from error
    runtime = deployment.get("runtime")
    source = deployment.get("source")
    if not isinstance(runtime, Mapping) or not isinstance(source, Mapping):
        raise RuntimeError("pinned deployment manifest is malformed")
    if (
        source.get("immutable_commit") != UPSTREAM_COMMIT
        or runtime.get("http_connector") != "127.0.0.1:8080"
        or runtime.get("activemq_connector") != "127.0.0.1:61616"
        or runtime.get("hsqldb_bind_address") != "127.0.0.1"
    ):
        raise RuntimeError("native deployment is not the pinned loopback target")
    return {
        "kind": "parabank",
        "origin": DEFAULT_ORIGIN,
        "upstream_commit": UPSTREAM_COMMIT,
        "loopback": True,
        "war_sha256": deployment.get("build", {}).get("war_sha256"),
        "tomcat_version": runtime.get("tomcat_version"),
    }


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".cua-native-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def export_draft_artifact(
    bundle: CapabilityBundle,
    *,
    forbidden_values: Iterable[bytes],
    trace_summary: Mapping[str, Any],
    target: Mapping[str, Any],
    qualification: Mapping[str, Any],
    replay: Mapping[str, Any],
    artifact_relative_path: str = ARTIFACT_RELATIVE_PATH,
) -> BundleReference:
    """Export only the value-safe draft and a matching release-safe index entry."""

    canonical, _ = safe_bundle_bytes(bundle, forbidden_values=forbidden_values)
    digest = hashlib.sha256(canonical).hexdigest()
    reference = BundleReference(name=bundle.capability.name, version=bundle.capability.version, digest=digest)
    artifact_path = ROOT / artifact_relative_path
    if artifact_path.exists() and artifact_path.read_bytes() != canonical:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", artifact_relative_path],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if tracked:
            raise RuntimeError("committed artifact path already contains a different immutable draft")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if not artifact_path.exists() or artifact_path.read_bytes() != canonical:
        artifact_path.write_bytes(canonical)

    index_path = ARTIFACTS_ROOT / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        index = {"schema_version": 1, "entries": []}
    if not isinstance(index, dict) or not isinstance(index.get("entries"), list):
        raise RuntimeError("artifact index is malformed")
    entry = {
        "reference": f"capability/{reference.name}@{reference.version}",
        "name": reference.name,
        "version": reference.version,
        "digest": f"sha256:{reference.digest}",
        "artifact_path": artifact_relative_path,
        "lifecycle": "DRAFT",
        "provenance": {
            "trace_id": trace_summary.get("trace_id"),
            "source_types": sorted(set(trace_summary.get("source_types", ())) | {"declared", "reviewer_added"}),
            "raw_values_persisted": False,
        },
        "target": dict(target),
        "runtime_fingerprint": runtime_fingerprint_payload(),
        "qualification": dict(qualification),
        "approval": {"performed_in_temporary_registry": True, "sidecar_committed": False},
        "replay": dict(replay),
    }
    entries = [
        old
        for old in index["entries"]
        if not isinstance(old, Mapping) or old.get("reference") != entry["reference"]
    ]
    entries.append(entry)
    index["schema_version"] = 1
    index["status"] = "LIVE_DRAFT_AVAILABLE"
    index["entries"] = entries
    index["note"] = "Draft is trace-derived and value-safe; temporary validation/approval sidecars remain outside the checkout."
    write_json_atomic(index_path, index)
    return reference


def export_evidence_manifest(
    payload: Mapping[str, Any],
    *,
    relative_path: str = EVIDENCE_RELATIVE_PATH,
) -> None:
    """Write a separate safe lifecycle manifest after the full run succeeds."""

    forbidden = {"OPENAI_API_KEY", "LLM_API_KEY", "CODEX_API_KEY"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if any(value.encode("utf-8") in encoded for value in forbidden):
        raise ValueError("provider credential name reached native evidence")
    write_json_atomic(ROOT / relative_path, dict(payload))


def source_fingerprint(root: Path = ROOT) -> str:
    """Hash runtime and testbed sources so evidence names the code that produced it."""

    paths = [root / "pyproject.toml", root / "uv.lock"]
    for directory in (root / "src", root / "testbed"):
        if directory.exists():
            paths.extend(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)
    digest = hashlib.sha256()
    for path in sorted(p for p in paths if p.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def contains_private_value(data: bytes, values: Iterable[bytes]) -> bool:
    """Return whether any private value occurs in ``data``.

    Short numeric fixtures (account and customer numbers) are matched as whole
    digit runs: a 5-digit account number also occurs by chance inside random
    hex identifiers, which would be a false leak. Other values match anywhere.
    """
    for value in values:
        if not value:
            continue
        if value.isdigit():
            if re.search(rb"(?<![0-9A-Za-z])" + re.escape(value) + rb"(?![0-9A-Za-z])", data):
                return True
        elif value in data:
            return True
    return False
