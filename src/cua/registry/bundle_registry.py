import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from cua.models.bundles import (
    BundleReference,
    BundleStep,
    CapabilityBundle,
    ConditionDefinition,
    TraceProvenance,
)
from cua.models.qualification import ApprovalRecord, RuntimeFingerprint, ValidationQualification
from cua.models.conditions import (
    AccountPresentCondition,
    AllCondition,
    AnyCondition,
    ApprovedConstant,
    ConditionExpr,
    ConditionReference,
    ExplicitDenialCondition,
    FieldEqualsCondition,
    MembershipValidCondition,
    NotCondition,
    OutputValidCondition,
    PageStateCondition,
    ParseableCondition,
)
from cua.profiles.parabank import (
    EXPLICIT_DENIAL_SIGNALS,
    LOCATORS,
    PAGE_STATES,
    PROFILE_ID,
)
from cua.registry.runtime_fingerprint import current_runtime_fingerprint


_PAGE_STATES = set(PAGE_STATES)
_SAFE_CONSTANTS = _PAGE_STATES | {"SAVINGS", "CHECKING", "USD"}
_STEP_OPERATION = {
    "CLICK": "CLICK",
    "TYPE_TEXT": "TYPE_TEXT",
    "SELECT": "SELECT",
    "SCROLL": "SCROLL",
    "EXTRACT": "READ",
}


class RegistryError(Exception):
    """Expected, redacted bundle-registry failure."""


class InvalidBundleError(RegistryError):
    def __init__(self, findings: tuple[str, ...]) -> None:
        self.findings = findings
        super().__init__("bundle failed static checks")


class UnknownReferenceError(InvalidBundleError):
    def __init__(self, references: tuple[str, ...]) -> None:
        self.references = references
        super().__init__(references)


class ImmutableRevisionError(RegistryError):
    pass


class DigestMismatchError(RegistryError):
    pass


class BundleNotFoundError(RegistryError):
    pass


class QualificationMismatchError(RegistryError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class RegistryRevision:
    """Integrity-checked, value-safe view of one stored capability revision."""

    reference: BundleReference
    lifecycle: Literal["DRAFT", "VALIDATED", "APPROVED"]
    bundle: CapabilityBundle
    qualification: ValidationQualification | None = None
    approval: ApprovalRecord | None = None


class BundleRegistry:
    """Local immutable draft store with canonical SHA-256 content checks."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def put_draft(self, bundle: CapabilityBundle) -> BundleReference:
        # Revalidate at the persistence boundary because Pydantic model_copy(update=...)
        # intentionally bypasses validation and nested caller-owned instances are untrusted.
        try:
            bundle = CapabilityBundle.model_validate(bundle.model_dump(mode="python"))
        except (AttributeError, ValidationError) as error:
            raise InvalidBundleError(("invalid_bundle_schema",)) from error

        _check_static_references(bundle)
        if not bundle.provenance.verified or not bundle.provenance.completion_proof_id:
            raise InvalidBundleError(("verified_trace_and_completion_proof_required",))

        canonical = canonical_bundle_json(bundle)
        digest = hashlib.sha256(canonical).hexdigest()
        reference = BundleReference(
            name=bundle.capability.name,
            version=bundle.capability.version,
            digest=digest,
        )
        revision_dir = self._revision_dir(reference)
        manifest_path = revision_dir / "revision.json"
        artifact_path = revision_dir / "bundle.json"

        if manifest_path.exists():
            manifest = self._read_manifest(manifest_path)
            existing_digest = manifest.get("digest")
            if existing_digest != digest:
                raise ImmutableRevisionError("capability revision already has different content")
            if manifest.get("lifecycle") not in {"DRAFT", "VALIDATED", "APPROVED"}:
                raise DigestMismatchError("revision metadata is invalid")
            stored = self._read_and_verify_artifact(artifact_path, reference, existing_digest)
            if stored != bundle:
                raise DigestMismatchError("stored revision does not match its canonical digest")
            return reference

        if revision_dir.exists() and any(revision_dir.iterdir()):
            raise DigestMismatchError("incomplete revision storage")

        revision_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(artifact_path, canonical)
        manifest = json.dumps(
            {"digest": digest, "lifecycle": "DRAFT"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self._atomic_write(manifest_path, manifest)
        return reference

    def load_draft(self, reference: BundleReference) -> CapabilityBundle:
        reference = BundleReference.model_validate(reference.model_dump(mode="python"))
        revision_dir = self._revision_dir(reference)
        manifest_path = revision_dir / "revision.json"
        artifact_path = revision_dir / "bundle.json"
        if not manifest_path.is_file() or not artifact_path.is_file():
            raise BundleNotFoundError("bundle revision not found")
        manifest = self._read_manifest(manifest_path)
        if manifest.get("digest") != reference.digest:
            raise DigestMismatchError("revision reference does not match stored digest")
        if manifest.get("lifecycle") not in {"DRAFT", "VALIDATED", "APPROVED"}:
            raise DigestMismatchError("revision metadata is invalid")
        return self._read_and_verify_artifact(artifact_path, reference, reference.digest)

    def inspect_revision(self, reference: BundleReference) -> RegistryRevision:
        """Return an integrity-checked artifact and its stored lifecycle records."""
        reference = BundleReference.model_validate(reference.model_dump(mode="python"))
        revision_dir = self._revision_dir(reference)
        manifest = self._read_manifest(revision_dir / "revision.json")
        if manifest.get("digest") != reference.digest:
            raise DigestMismatchError("revision reference does not match stored digest")
        lifecycle = manifest.get("lifecycle")
        if lifecycle not in {"DRAFT", "VALIDATED", "APPROVED"}:
            raise DigestMismatchError("revision lifecycle is invalid")
        bundle = self.load_draft(reference)
        qualification = None
        approval = None
        if lifecycle in {"VALIDATED", "APPROVED"}:
            qualification = self._read_validation(revision_dir / "validation.json", reference)
        if lifecycle == "APPROVED":
            try:
                approval = ApprovalRecord.model_validate_json(
                    (revision_dir / "approval.json").read_bytes()
                )
            except (OSError, ValidationError, ValueError) as error:
                raise DigestMismatchError("approval record cannot be verified") from error
            if (
                approval.digest != reference.digest
                or approval.capability_version != bundle.capability.version
                or approval.validation_run_ref != qualification.validation_run_ref
            ):
                raise DigestMismatchError("approval is not bound to the validated revision")
        return RegistryRevision(
            reference=reference,
            lifecycle=lifecycle,
            bundle=bundle,
            qualification=qualification,
            approval=approval,
        )

    def list_revisions(self) -> tuple[RegistryRevision, ...]:
        """Enumerate valid stored revisions without exposing filesystem paths."""
        if not self._root.is_dir() or self._root.is_symlink():
            return ()
        revisions: list[RegistryRevision] = []
        for name_dir in sorted(self._root.iterdir(), key=lambda item: item.name):
            if name_dir.is_symlink() or not name_dir.is_dir():
                continue
            for version_dir in sorted(name_dir.iterdir(), key=lambda item: item.name):
                if version_dir.is_symlink() or not version_dir.is_dir():
                    continue
                try:
                    manifest = self._read_manifest(version_dir / "revision.json")
                    reference = BundleReference(
                        name=name_dir.name,
                        version=version_dir.name,
                        digest=manifest["digest"],
                    )
                    revisions.append(self.inspect_revision(reference))
                except (RegistryError, OSError, ValidationError, KeyError, ValueError):
                    # Malformed or tampered entries do not become catalog records.
                    continue
        return tuple(revisions)

    def validate(
        self,
        reference: BundleReference,
        qualification: ValidationQualification,
    ) -> ValidationQualification:
        reference = BundleReference.model_validate(reference.model_dump(mode="python"))
        qualification = ValidationQualification.model_validate(
            qualification.model_dump(mode="python")
        )
        bundle = self.load_draft(reference)
        manifest_path = self._revision_dir(reference) / "revision.json"
        manifest = self._read_manifest(manifest_path)
        if manifest["lifecycle"] != "DRAFT":
            raise ImmutableRevisionError("only DRAFT revisions can be validated")
        if qualification.bundle_digest != reference.digest:
            raise QualificationMismatchError("validation is bound to another bundle digest")
        current = current_runtime_fingerprint()
        if qualification.runtime_fingerprint != current:
            raise QualificationMismatchError("validation does not qualify the current runtime")
        if (
            qualification.runtime_fingerprint.profile_sha256
            != bundle.compatibility.profile_sha256
            or qualification.runtime_fingerprint.condition_sha256
            != bundle.compatibility.condition_runtime_sha256
        ):
            raise QualificationMismatchError("bundle code pins differ from validation runtime")

        self._atomic_write(
            self._revision_dir(reference) / "validation.json",
            qualification.model_dump_json().encode("utf-8"),
        )
        self._write_manifest(manifest_path, reference.digest, "VALIDATED")
        return qualification

    def approve(
        self,
        reference: BundleReference,
        *,
        reviewer_ref: str,
        reviewer_type: str,
    ) -> ApprovalRecord:
        reference = BundleReference.model_validate(reference.model_dump(mode="python"))
        bundle = self.load_draft(reference)
        revision_dir = self._revision_dir(reference)
        manifest_path = revision_dir / "revision.json"
        manifest = self._read_manifest(manifest_path)
        if manifest["lifecycle"] != "VALIDATED":
            raise ImmutableRevisionError("only VALIDATED revisions can be approved")
        qualification = self._read_validation(revision_dir / "validation.json", reference)
        current = current_runtime_fingerprint()
        if qualification.runtime_fingerprint != current:
            raise QualificationMismatchError(
                "validation no longer qualifies the current runtime executable closure"
            )
        record = ApprovalRecord(
            digest=reference.digest,
            capability_version=bundle.capability.version,
            reviewer_ref=reviewer_ref,
            reviewer_type=reviewer_type,
            validation_run_ref=qualification.validation_run_ref,
            approved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        self._atomic_write(
            revision_dir / "approval.json",
            record.model_dump_json().encode("utf-8"),
        )
        self._write_manifest(manifest_path, reference.digest, "APPROVED")
        return record

    def load_approved(
        self,
        reference: BundleReference,
        *,
        runtime_fingerprint: RuntimeFingerprint,
        browser_version: str,
        target_revision: str,
    ) -> CapabilityBundle:
        """Recheck content, approval, and qualification before replay can dispatch."""
        reference = BundleReference.model_validate(reference.model_dump(mode="python"))
        runtime_fingerprint = RuntimeFingerprint.model_validate(
            runtime_fingerprint.model_dump(mode="python")
        )
        revision_dir = self._revision_dir(reference)
        manifest = self._read_manifest(revision_dir / "revision.json")
        if manifest.get("digest") != reference.digest:
            raise DigestMismatchError("revision reference does not match stored digest")
        if manifest.get("lifecycle") != "APPROVED":
            raise ImmutableRevisionError("execution requires an APPROVED revision")
        bundle = self._read_and_verify_artifact(
            revision_dir / "bundle.json",
            reference,
            reference.digest,
        )
        qualification = self._read_validation(revision_dir / "validation.json", reference)
        try:
            approval = ApprovalRecord.model_validate_json(
                (revision_dir / "approval.json").read_bytes()
            )
        except (OSError, ValidationError, ValueError) as error:
            raise DigestMismatchError("approval record cannot be verified") from error
        if (
            approval.digest != reference.digest
            or approval.capability_version != bundle.capability.version
            or approval.validation_run_ref != qualification.validation_run_ref
        ):
            raise DigestMismatchError("approval is not bound to the validated revision")

        current = current_runtime_fingerprint()
        if runtime_fingerprint != current or qualification.runtime_fingerprint != current:
            raise QualificationMismatchError("runtime executable closure changed after validation")
        if (
            browser_version != qualification.browser_version
            or target_revision != qualification.target_revision
        ):
            raise QualificationMismatchError("browser or target revision differs from qualification")
        if (
            bundle.compatibility.profile_sha256 != current.profile_sha256
            or bundle.compatibility.condition_runtime_sha256 != current.condition_sha256
        ):
            raise QualificationMismatchError("profile or condition implementation changed")
        return bundle

    def prepare_execution(self, reference: BundleReference, **qualification) -> CapabilityBundle:
        """Named pre-dispatch gate for replay and other execution entry points."""
        return self.load_approved(reference, **qualification)

    def prepare_validation(
        self,
        reference: BundleReference,
        *,
        runtime_fingerprint: RuntimeFingerprint,
        browser_version: str,
        target_revision: str,
    ) -> CapabilityBundle:
        """Load a statically valid DRAFT for a manager-authorized test session only."""
        reference = BundleReference.model_validate(reference.model_dump(mode="python"))
        runtime_fingerprint = RuntimeFingerprint.model_validate(
            runtime_fingerprint.model_dump(mode="python")
        )
        bundle = self.load_draft(reference)
        manifest = self._read_manifest(self._revision_dir(reference) / "revision.json")
        if manifest.get("lifecycle") != "DRAFT":
            raise ImmutableRevisionError("validation replay requires a DRAFT revision")
        if (
            not isinstance(browser_version, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}", browser_version, re.ASCII)
            is None
            or not isinstance(target_revision, str)
            or re.fullmatch(r"[a-f0-9]{40}", target_revision, re.ASCII) is None
        ):
            raise QualificationMismatchError("validation environment pin is invalid")
        current = current_runtime_fingerprint()
        if runtime_fingerprint != current:
            raise QualificationMismatchError("validation runtime is not current")
        if (
            bundle.compatibility.profile_sha256 != current.profile_sha256
            or bundle.compatibility.condition_runtime_sha256 != current.condition_sha256
        ):
            raise QualificationMismatchError("bundle code pins differ from validation runtime")
        return bundle

    def artifact_path(self, reference: BundleReference) -> Path:
        reference = BundleReference.model_validate(reference.model_dump(mode="python"))
        return self._revision_dir(reference) / "bundle.json"

    def _revision_dir(self, reference: BundleReference) -> Path:
        return self._root / reference.name / reference.version

    @staticmethod
    def _write_manifest(path: Path, digest: str, lifecycle: str) -> None:
        content = json.dumps(
            {"digest": digest, "lifecycle": lifecycle},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        BundleRegistry._atomic_write(path, content)

    @staticmethod
    def _read_validation(path: Path, reference: BundleReference) -> ValidationQualification:
        try:
            qualification = ValidationQualification.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError) as error:
            raise DigestMismatchError("validation qualification cannot be verified") from error
        if qualification.bundle_digest != reference.digest:
            raise DigestMismatchError("validation qualification has a different bundle digest")
        return qualification

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, str]:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DigestMismatchError("revision metadata cannot be read") from error
        if (
            not isinstance(manifest, dict)
            or not isinstance(manifest.get("digest"), str)
            or not isinstance(manifest.get("lifecycle"), str)
        ):
            raise DigestMismatchError("revision metadata is invalid")
        return manifest

    @staticmethod
    def _read_and_verify_artifact(
        path: Path,
        reference: BundleReference,
        expected_digest: str,
    ) -> CapabilityBundle:
        try:
            bundle = CapabilityBundle.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError) as error:
            raise DigestMismatchError("bundle artifact cannot be verified") from error
        actual_digest = hashlib.sha256(canonical_bundle_json(bundle)).hexdigest()
        if (
            actual_digest != expected_digest
            or actual_digest != reference.digest
            or bundle.capability.name != reference.name
            or bundle.capability.version != reference.version
        ):
            raise DigestMismatchError("bundle artifact digest does not match its revision")
        try:
            _check_static_references(bundle)
        except InvalidBundleError as error:
            raise DigestMismatchError("bundle artifact no longer passes static checks") from error
        return bundle

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        fd, temporary_path = tempfile.mkstemp(prefix=".cua-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
        except BaseException:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise


def canonical_bundle_json(bundle: CapabilityBundle) -> bytes:
    """Canonical UTF-8 JSON for the complete executable closure and provenance."""
    payload = bundle.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _check_static_references(bundle: CapabilityBundle) -> None:
    runtime = current_runtime_fingerprint()
    if bundle.compatibility.profile != PROFILE_ID:
        raise InvalidBundleError(("unsupported_target_profile",))
    if bundle.compatibility.profile_sha256 != runtime.profile_sha256:
        raise InvalidBundleError(("target_profile_pin_mismatch",))
    if bundle.compatibility.condition_runtime_sha256 != runtime.condition_sha256:
        raise InvalidBundleError(("condition_runtime_pin_mismatch",))

    inputs = {item.name for item in bundle.contract.inputs}
    outputs = {item.name for item in bundle.contract.outputs}
    targets = {item.ref: item for item in bundle.targets}
    conditions = {item.name: item for item in bundle.conditions}
    recoveries = {item.ref for item in bundle.recoveries}
    parsers = {(item.parser_id, item.version) for item in bundle.parsers}
    profile_signals = set(bundle.profile_signals)
    unknown: set[str] = set()
    invalid: set[str] = set()

    for step in bundle.steps:
        if step.target_ref is not None:
            target = targets.get(step.target_ref)
            if target is None:
                unknown.add(f"target:{step.target_ref}")
            else:
                required_operation = _STEP_OPERATION.get(step.kind)
                if required_operation and required_operation not in target.allowed_operations:
                    invalid.add(f"operation_not_declared:{step.id}")
        if step.recovery_ref is not None and step.recovery_ref not in recoveries:
            unknown.add(f"recovery:{step.recovery_ref}")
        if step.input_ref is not None:
            _check_input_ref(step.input_ref, inputs, unknown)
        if step.output_ref is not None:
            _check_output_ref(step.output_ref, outputs, unknown)
        if step.parser_id is not None and (step.parser_id, step.parser_version) not in parsers:
            unknown.add(f"parser:{step.parser_id}@{step.parser_version}")
        for expression in (*step.preconditions, *step.postconditions):
            _check_expression(
                expression,
                inputs,
                outputs,
                targets,
                parsers,
                conditions,
                profile_signals,
                unknown,
                invalid,
                stack=(),
                depth=1,
            )

    for condition in bundle.conditions:
        _check_expression(
            condition.expression,
            inputs,
            outputs,
            targets,
            parsers,
            conditions,
            profile_signals,
            unknown,
            invalid,
            stack=(condition.name,),
            depth=1,
        )

    for target in bundle.targets:
        profile_rule = LOCATORS.get(target.locator)
        if profile_rule is None:
            invalid.add(f"unknown_profile_locator:{target.ref}")
        else:
            expected_operations = {"READ"} if "selector" in profile_rule else {"CLICK"}
            if not set(target.allowed_operations).issubset(expected_operations):
                invalid.add(f"profile_operation_mismatch:{target.ref}")
        if target.binding_ref is not None:
            _check_input_ref(target.binding_ref, inputs, unknown)

    for recovery in bundle.recoveries:
        target = targets.get(recovery.anchor_target_ref)
        if target is None:
            unknown.add(f"target:{recovery.anchor_target_ref}")
        elif "CLICK" not in target.allowed_operations:
            invalid.add(f"recovery_anchor_not_clickable:{recovery.ref}")

    for override in bundle.profile_overrides:
        if override.field not in targets:
            # Profile-owned locators may be named independently, but the binding target
            # must be explicit in the executable closure.
            unknown.add(f"profile_target:{override.field}")
        elif targets[override.field].locator != override.locator:
            invalid.add(f"profile_override_mismatch:{override.field}")

    if not set(bundle.profile_signals).issubset(set(EXPLICIT_DENIAL_SIGNALS)):
        invalid.add("undeclared_profile_signal")
    for parser in bundle.parsers:
        if (parser.parser_id, parser.version) != ("USD_DECIMAL_V1", "1"):
            unknown.add(f"parser:{parser.parser_id}@{parser.version}")
        elif parser.implementation_sha256 != runtime.parser_sha256:
            invalid.add("parser_implementation_pin_mismatch")

    if not bundle.provenance.verified or not bundle.provenance.completion_proof_id:
        invalid.add("verified_trace_and_completion_proof_required")

    if unknown:
        raise UnknownReferenceError(tuple(sorted(unknown)))
    if invalid:
        raise InvalidBundleError(tuple(sorted(invalid)))


def _check_expression(
    expression: ConditionExpr,
    inputs: set[str],
    outputs: set[str],
    targets: dict,
    parsers: set[tuple[str, str]],
    conditions: dict[str, ConditionDefinition],
    profile_signals: set[str],
    unknown: set[str],
    invalid: set[str],
    *,
    stack: tuple[str, ...],
    depth: int,
) -> None:
    if depth > 8:
        invalid.add("condition_depth_exceeded")
        return
    if isinstance(expression, ConditionReference):
        referenced = conditions.get(expression.name)
        if referenced is None:
            unknown.add(f"condition:{expression.name}")
        elif expression.name in stack:
            invalid.add("condition_reference_cycle")
        else:
            _check_expression(
                referenced.expression,
                inputs,
                outputs,
                targets,
                parsers,
                conditions,
                profile_signals,
                unknown,
                invalid,
                stack=(*stack, expression.name),
                depth=depth + 1,
            )
        return
    if isinstance(expression, (AllCondition, AnyCondition)):
        for child in expression.conditions:
            _check_expression(
                child,
                inputs,
                outputs,
                targets,
                parsers,
                conditions,
                profile_signals,
                unknown,
                invalid,
                stack=stack,
                depth=depth + 1,
            )
        return
    if isinstance(expression, NotCondition):
        _check_expression(
            expression.child,
            inputs,
            outputs,
            targets,
            parsers,
            conditions,
            profile_signals,
            unknown,
            invalid,
            stack=stack,
            depth=depth + 1,
        )
        return
    if isinstance(expression, PageStateCondition):
        if expression.value not in _PAGE_STATES:
            invalid.add("unknown_page_state")
    elif isinstance(expression, AccountPresentCondition):
        _check_input_ref(expression.input_ref, inputs, unknown)
    elif isinstance(expression, MembershipValidCondition):
        _check_input_ref(expression.input_ref, inputs, unknown)
    elif isinstance(expression, FieldEqualsCondition):
        if expression.target_ref not in targets:
            unknown.add(f"target:{expression.target_ref}")
        if expression.expected.kind == "input_ref":
            _check_input_ref(expression.expected.name, inputs, unknown)
        elif isinstance(expression.expected, ApprovedConstant):
            if expression.expected.value not in _SAFE_CONSTANTS:
                invalid.add("unsafe_persisted_constant")
    elif isinstance(expression, ParseableCondition):
        if expression.target_ref not in targets:
            unknown.add(f"target:{expression.target_ref}")
        if (expression.parser_id, expression.parser_version) not in parsers:
            unknown.add(f"parser:{expression.parser_id}@{expression.parser_version}")
    elif isinstance(expression, OutputValidCondition):
        _check_output_ref(expression.output_ref, outputs, unknown)
    elif isinstance(expression, ExplicitDenialCondition):
        if expression.signal_ref not in profile_signals:
            unknown.add(f"profile_signal:{expression.signal_ref}")


def _check_input_ref(reference: str, inputs: set[str], unknown: set[str]) -> None:
    name = reference.removeprefix("inputs.")
    if name not in inputs:
        unknown.add(f"input:{reference}")


def _check_output_ref(reference: str, outputs: set[str], unknown: set[str]) -> None:
    name = reference.removeprefix("outputs.")
    if name not in outputs:
        unknown.add(f"output:{reference}")
