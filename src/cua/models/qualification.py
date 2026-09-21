from typing import Annotated, Literal

from pydantic import StringConstraints

from cua.models.base import StrictModel
from cua.models.bundles import Reference, Sha256


SafeVersion = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")]
GitRevision = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]


class RuntimeFingerprint(StrictModel):
    source_sha256: Sha256
    parser_sha256: Sha256
    condition_sha256: Sha256
    profile_sha256: Sha256


class ValidationQualification(StrictModel):
    bundle_digest: Sha256
    validation_run_ref: Reference
    runtime_fingerprint: RuntimeFingerprint
    browser_version: SafeVersion
    target_revision: GitRevision
    replay_passed: Literal[True]
    independent_oracle_passed: Literal[True]


class ApprovalRecord(StrictModel):
    digest: Sha256
    capability_version: Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)]
    reviewer_ref: Reference
    reviewer_type: Literal["independent_reviewer", "operator"]
    validation_run_ref: Reference
    approved_at: Annotated[str, StringConstraints(min_length=20, max_length=40)]
