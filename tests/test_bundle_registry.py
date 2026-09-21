import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.models.bundles import (
    BundleStep,
    CapabilityBundle,
    CapabilityContract,
    CapabilityIdentity,
    Compatibility,
    ConditionDefinition,
    InputContract,
    LocatorProfileOverride,
    OutputContract,
    ParserPin,
    RecoveryDefinition,
    StepSource,
    TargetDefinition,
    TraceProvenance,
)
from cua.models.qualification import RuntimeFingerprint, ValidationQualification
from cua.models.conditions import (
    ConditionReference,
    NotCondition,
    OverviewCompleteCondition,
    ParseableCondition,
)
from cua.registry import (
    BundleRegistry,
    DigestMismatchError,
    ImmutableRevisionError,
    InvalidBundleError,
    QualificationMismatchError,
    UnknownReferenceError,
)
from cua.registry.runtime_fingerprint import current_runtime_fingerprint


def _bundle(*, parser_digest=None, trace_id="trace_1"):
    fingerprint = current_runtime_fingerprint()
    return CapabilityBundle(
        schema_version="1",
        capability=CapabilityIdentity(name="get_savings_balance", version="1.0.0"),
        compatibility=Compatibility(
            profile="parabank-native-v1",
            runtime_contract="cua-v1",
            profile_sha256=fingerprint.profile_sha256,
            condition_runtime_sha256=fingerprint.condition_sha256,
        ),
        contract=CapabilityContract(
            inputs=(
                InputContract(
                    name="account_id",
                    value_type="string",
                    pattern=r"^[0-9]+$",
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
            business_outcomes=("ACCOUNT_NOT_FOUND", "ACCESS_DENIED"),
        ),
        steps=(
            BundleStep(
                id="verify_overview",
                kind="ASSERT",
                preconditions=(ConditionReference(kind="condition_ref", name="overview_ready"),),
                source=StepSource(type="declared"),
            ),
            BundleStep(
                id="read_balance",
                kind="EXTRACT",
                target_ref="detail_available_balance",
                preconditions=(
                    ParseableCondition(
                        kind="parseable",
                        target_ref="detail_available_balance",
                        parser_id="USD_DECIMAL_V1",
                        parser_version="1",
                    ),
                ),
                output_ref="available_balance",
                parser_id="USD_DECIMAL_V1",
                parser_version="1",
                source=StepSource(type="observed", event_ids=("event_1",)),
            ),
        ),
        targets=(
            TargetDefinition(
                ref="overview_nav",
                locator="ROLE_LINK_ACCOUNTS_OVERVIEW",
                allowed_operations=("CLICK",),
            ),
            TargetDefinition(
                ref="requested_account_link",
                locator="TABLE_ACCOUNT_LINK_BY_INPUT",
                allowed_operations=("CLICK",),
                binding_ref="inputs.account_id",
            ),
            TargetDefinition(
                ref="detail_available_balance",
                locator="PROFILE_AVAILABLE_BALANCE",
                allowed_operations=("READ",),
            ),
        ),
        conditions=(
            ConditionDefinition(
                name="overview_ready",
                expression=OverviewCompleteCondition(kind="overview_complete"),
            ),
        ),
        recoveries=(
            RecoveryDefinition(
                ref="readonly_overview_anchor",
                anchor_target_ref="overview_nav",
                max_attempts=2,
            ),
        ),
        parsers=(
            ParserPin(
                parser_id="USD_DECIMAL_V1",
                version="1",
                implementation_sha256=parser_digest or fingerprint.parser_sha256,
            ),
        ),
        profile_overrides=(
            LocatorProfileOverride(
                field="requested_account_link",
                locator="TABLE_ACCOUNT_LINK_BY_INPUT",
            ),
        ),
        provenance=TraceProvenance(
            trace_id=trace_id,
            verified=True,
            completion_proof_id="proof_1",
        ),
    )


def test_registry_stores_immutable_revision_with_canonical_full_closure(tmp_path):
    registry = BundleRegistry(tmp_path)
    bundle = _bundle()

    reference = registry.put_draft(bundle)

    expected_json = json.dumps(
        bundle.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert reference.digest == hashlib.sha256(expected_json).hexdigest()
    assert registry.load_draft(reference) == bundle

    # A nested parser implementation change cannot overwrite an existing revision.
    with pytest.raises(ImmutableRevisionError):
        registry.put_draft(_bundle(trace_id="trace_2"))

    changed_revision = _bundle(trace_id="trace_2").model_copy(
        update={"capability": CapabilityIdentity(name="get_savings_balance", version="1.0.1")}
    )
    assert registry.put_draft(changed_revision).version == "1.0.1"


def test_prepare_validation_accepts_only_current_static_draft(tmp_path):
    registry = BundleRegistry(tmp_path)
    bundle = _bundle()
    reference = registry.put_draft(bundle)
    current = current_runtime_fingerprint()

    assert registry.prepare_validation(
        reference,
        runtime_fingerprint=current,
        browser_version="1.60.0",
        target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
    ) == bundle

    changed = RuntimeFingerprint.model_validate(
        current.model_dump(mode="python") | {"source_sha256": "c" * 64}
    )
    with pytest.raises(QualificationMismatchError):
        registry.prepare_validation(
            reference,
            runtime_fingerprint=changed,
            browser_version="1.60.0",
            target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        )

    _qualify_and_approve(registry, reference)
    with pytest.raises(ImmutableRevisionError):
        registry.prepare_validation(
            reference,
            runtime_fingerprint=current,
            browser_version="1.60.0",
            target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        )


def _qualify_and_approve(registry, reference):
    qualification = ValidationQualification(
        bundle_digest=reference.digest,
        validation_run_ref="validation_1",
        runtime_fingerprint=current_runtime_fingerprint(),
        browser_version="1.60.0",
        target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        replay_passed=True,
        independent_oracle_passed=True,
    )
    registry.validate(reference, qualification)
    registry.approve(
        reference,
        reviewer_ref="reviewer_1",
        reviewer_type="independent_reviewer",
    )
    return qualification


def test_nested_dependency_tamper_invalidates_approval_before_dispatch(tmp_path):
    registry = BundleRegistry(tmp_path)
    reference = registry.put_draft(_bundle())
    _qualify_and_approve(registry, reference)

    payload = json.loads(registry.artifact_path(reference).read_text(encoding="utf-8"))
    payload["parsers"][0]["implementation_sha256"] = "b" * 64
    registry.artifact_path(reference).write_text(json.dumps(payload), encoding="utf-8")

    dispatched = []
    with pytest.raises(DigestMismatchError):
        approved_bundle = registry.load_approved(
            reference,
            runtime_fingerprint=current_runtime_fingerprint(),
            browser_version="1.60.0",
            target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        )
        dispatched.append(approved_bundle)

    assert dispatched == []


def test_approved_bundle_requires_matching_runtime_qualification(tmp_path):
    registry = BundleRegistry(tmp_path)
    reference = registry.put_draft(_bundle())
    _qualify_and_approve(registry, reference)
    current = current_runtime_fingerprint()
    changed_runtime = current.model_copy(update={"profile_sha256": "f" * 64})

    with pytest.raises(QualificationMismatchError):
        registry.load_approved(
            reference,
            runtime_fingerprint=changed_runtime,
            browser_version="1.60.0",
            target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        )


def test_approval_rejects_runtime_change_since_validation(tmp_path, monkeypatch):
    registry = BundleRegistry(tmp_path)
    reference = registry.put_draft(_bundle())
    qualification = ValidationQualification(
        bundle_digest=reference.digest,
        validation_run_ref="validation_1",
        runtime_fingerprint=current_runtime_fingerprint(),
        browser_version="1.60.0",
        target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        replay_passed=True,
        independent_oracle_passed=True,
    )
    registry.validate(reference, qualification)
    changed_runtime = RuntimeFingerprint.model_validate(
        qualification.runtime_fingerprint.model_dump(mode="python")
        | {"source_sha256": "c" * 64}
    )
    monkeypatch.setattr(
        "cua.registry.bundle_registry.current_runtime_fingerprint",
        lambda: changed_runtime,
    )

    with pytest.raises(QualificationMismatchError):
        registry.approve(
            reference,
            reviewer_ref="reviewer_1",
            reviewer_type="independent_reviewer",
        )

    revision_dir = registry.artifact_path(reference).parent
    assert json.loads((revision_dir / "revision.json").read_text())[
        "lifecycle"
    ] == "VALIDATED"
    assert not (revision_dir / "approval.json").exists()


def test_approval_rejects_lockfile_change_in_runtime_closure(tmp_path, monkeypatch):
    registry = BundleRegistry(tmp_path)
    reference = registry.put_draft(_bundle())
    qualification = ValidationQualification(
        bundle_digest=reference.digest,
        validation_run_ref="validation_1",
        runtime_fingerprint=current_runtime_fingerprint(),
        browser_version="1.60.0",
        target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        replay_passed=True,
        independent_oracle_passed=True,
    )
    registry.validate(reference, qualification)

    injected_root = tmp_path / "runtime-root"
    injected_root.mkdir()
    project_root = Path(__file__).resolve().parents[1]
    (injected_root / "pyproject.toml").write_bytes(
        (project_root / "pyproject.toml").read_bytes()
    )
    (injected_root / "uv.lock").write_bytes((project_root / "uv.lock").read_bytes())
    (injected_root / "uv.lock").write_text(
        (injected_root / "uv.lock").read_text(encoding="utf-8")
        + "\n# injected dependency closure change\n",
        encoding="utf-8",
    )
    changed = current_runtime_fingerprint(project_root=injected_root)
    assert changed.source_sha256 != qualification.runtime_fingerprint.source_sha256
    monkeypatch.setattr(
        "cua.registry.bundle_registry.current_runtime_fingerprint",
        lambda: changed,
    )

    with pytest.raises(QualificationMismatchError):
        registry.approve(
            reference,
            reviewer_ref="reviewer_1",
            reviewer_type="independent_reviewer",
        )

    revision_dir = registry.artifact_path(reference).parent
    assert json.loads((revision_dir / "revision.json").read_text())[
        "lifecycle"
    ] == "VALIDATED"
    assert not (revision_dir / "approval.json").exists()


def test_approved_load_rejects_manifest_digest_mismatch(tmp_path):
    registry = BundleRegistry(tmp_path)
    reference = registry.put_draft(_bundle())
    _qualify_and_approve(registry, reference)
    revision_dir = registry.artifact_path(reference).parent
    manifest_path = revision_dir / "revision.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["digest"] = "d" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DigestMismatchError):
        registry.load_approved(
            reference,
            runtime_fingerprint=current_runtime_fingerprint(),
            browser_version="1.60.0",
            target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        )


def test_registry_detects_nested_artifact_tampering(tmp_path):
    registry = BundleRegistry(tmp_path)
    reference = registry.put_draft(_bundle())
    artifact = registry.artifact_path(reference)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["parsers"][0]["implementation_sha256"] = "b" * 64
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DigestMismatchError):
        registry.load_draft(reference)


def test_registry_rejects_unknown_condition_and_target_references(tmp_path):
    registry = BundleRegistry(tmp_path)
    bundle = _bundle().model_copy(
        update={
            "steps": (
                BundleStep(
                    id="bad_step",
                    kind="CLICK",
                    target_ref="missing_target",
                    preconditions=(ConditionReference(kind="condition_ref", name="missing_condition"),),
                    source=StepSource(type="observed", event_ids=("event_2",)),
                ),
            )
        }
    )

    with pytest.raises(UnknownReferenceError) as error:
        registry.put_draft(bundle)

    assert {"target:missing_target", "condition:missing_condition"}.issubset(
        set(error.value.references)
    )


def test_registry_rejects_condition_cycles_and_excessive_depth(tmp_path):
    registry = BundleRegistry(tmp_path)
    cyclic = _bundle().model_copy(
        update={
            "conditions": (
                ConditionDefinition(
                    name="overview_ready",
                    expression=ConditionReference(kind="condition_ref", name="overview_ready"),
                ),
            )
        }
    )
    with pytest.raises(InvalidBundleError, match="static checks") as cycle_error:
        registry.put_draft(cyclic)
    assert "condition_reference_cycle" in cycle_error.value.findings

    deep_expression = OverviewCompleteCondition(kind="overview_complete")
    for _ in range(8):
        deep_expression = NotCondition(kind="not", child=deep_expression)
    too_deep = _bundle().model_copy(
        update={
            "conditions": (
                ConditionDefinition(name="overview_ready", expression=deep_expression),
            )
        }
    )
    with pytest.raises(InvalidBundleError) as depth_error:
        registry.put_draft(too_deep)
    assert "condition_depth_exceeded" in depth_error.value.findings


def test_models_reject_undeclared_operation_and_extra_persisted_fields():
    with pytest.raises(ValidationError):
        BundleStep(
            id="transfer",
            kind="TRANSFER",
            source=StepSource(type="observed", event_ids=("event_1",)),
        )

    payload = _bundle().model_dump()
    payload["goal"] = "read private account"
    with pytest.raises(ValidationError):
        CapabilityBundle.model_validate(payload)
