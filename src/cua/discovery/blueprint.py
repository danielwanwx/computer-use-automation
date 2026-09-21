"""Reviewable capability scaffolding for verified native discoveries."""

from __future__ import annotations

from cua.models.bundles import (
    BundleStep,
    CapabilityBlueprint,
    CapabilityContract,
    CapabilityIdentity,
    CompilerCheckpoint,
    ConditionDefinition,
    InputContract,
    LocatorProfileOverride,
    OutputContract,
    ParserPin,
    RecoveryDefinition,
    StepSource,
    TargetDefinition,
)
from cua.models.conditions import (
    ApprovedConstant,
    AccountPresentCondition,
    AllCondition,
    AnyCondition,
    ConditionReference,
    FieldEqualsCondition,
    InputValueReference,
    MembershipValidCondition,
    OverviewCompleteCondition,
    PageStateCondition,
    ParseableCondition,
    OutputValidCondition,
    PrincipalMatchesCondition,
)
from cua.profiles.parabank import PROFILE_ID
from cua.registry.runtime_fingerprint import current_runtime_fingerprint


def parabank_savings_balance_blueprint(
    *, reviewer_ref: str, capability_version: str
) -> CapabilityBlueprint:
    """Build the static ParaBank closure used to compile a successful native trace.

    The overview navigation is deliberately marked reviewer-added: it is a pinned,
    human-reviewed route anchor that may be needed on replay even when the discovery
    session already started on the overview page. It is never presented as a model or
    browser-observed event.
    """
    fingerprint = current_runtime_fingerprint()
    membership = BundleStep(
        id="verify_membership",
        kind="ASSERT",
        preconditions=(
            AllCondition(
                kind="all",
                conditions=(
                    ConditionReference(kind="condition_ref", name="overview_ready"),
                    PrincipalMatchesCondition(kind="principal_matches"),
                    AccountPresentCondition(
                        kind="account_present",
                        input_ref="inputs.account_id",
                    ),
                ),
            ),
        ),
        source=StepSource(type="declared"),
    )
    read_available = BundleStep(
        id="read_available",
        kind="EXTRACT",
        target_ref="detail_available_balance",
        preconditions=(
            PageStateCondition(kind="page_state", value="DETAIL_READY"),
            MembershipValidCondition(kind="membership_valid", input_ref="inputs.account_id"),
            FieldEqualsCondition(
                kind="field_equals",
                target_ref="detail_account_id",
                expected=InputValueReference(kind="input_ref", name="inputs.account_id"),
            ),
            FieldEqualsCondition(
                kind="field_equals",
                target_ref="detail_account_type",
                expected=ApprovedConstant(kind="constant", value="SAVINGS"),
            ),
            ParseableCondition(
                kind="parseable",
                target_ref="detail_available_balance",
                parser_id="USD_DECIMAL_V1",
                parser_version="1",
            ),
        ),
        postconditions=(OutputValidCondition(kind="output_valid", output_ref="available_balance"),),
        output_ref="available_balance",
        parser_id="USD_DECIMAL_V1",
        parser_version="1",
        source=StepSource(type="declared"),
    )
    return CapabilityBlueprint(
        schema_version="1",
        capability=CapabilityIdentity(
            name="get_savings_balance", version=capability_version
        ),
        compatibility={
            "profile": PROFILE_ID,
            "runtime_contract": "cua-v1",
            "profile_sha256": fingerprint.profile_sha256,
            "condition_runtime_sha256": fingerprint.condition_sha256,
        },
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
                OutputContract(
                    name="currency",
                    value_type="string",
                    enum=("USD",),
                ),
            ),
            business_outcomes=("ACCOUNT_NOT_FOUND", "ACCESS_DENIED"),
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
                ref="detail_account_id",
                locator="PROFILE_ACCOUNT_NUMBER",
                allowed_operations=("READ",),
            ),
            TargetDefinition(
                ref="detail_account_type",
                locator="PROFILE_ACCOUNT_TYPE",
                allowed_operations=("READ",),
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
        parsers=(
            ParserPin(
                parser_id="USD_DECIMAL_V1",
                version="1",
                implementation_sha256=fingerprint.parser_sha256,
            ),
        ),
        profile_overrides=(
            LocatorProfileOverride(
                field="overview_nav",
                locator="ROLE_LINK_ACCOUNTS_OVERVIEW",
            ),
            LocatorProfileOverride(
                field="requested_account_link",
                locator="TABLE_ACCOUNT_LINK_BY_INPUT",
            ),
            LocatorProfileOverride(
                field="detail_account_id",
                locator="PROFILE_ACCOUNT_NUMBER",
            ),
            LocatorProfileOverride(
                field="detail_account_type",
                locator="PROFILE_ACCOUNT_TYPE",
            ),
            LocatorProfileOverride(
                field="detail_available_balance",
                locator="PROFILE_AVAILABLE_BALANCE",
            ),
        ),
        checkpoints=(
            CompilerCheckpoint(
                step=BundleStep(
                    id="ensure_overview",
                    kind="CLICK",
                    target_ref="overview_nav",
                    preconditions=(
                        AnyCondition(
                            kind="any",
                            conditions=(
                                PageStateCondition(
                                    kind="page_state", value="AUTHENTICATED_HOME"
                                ),
                                PageStateCondition(
                                    kind="page_state", value="OVERVIEW_READY"
                                ),
                                PageStateCondition(
                                    kind="page_state", value="DETAIL_READY"
                                ),
                            ),
                        ),
                    ),
                    postconditions=(
                        PageStateCondition(kind="page_state", value="OVERVIEW_READY"),
                    ),
                    source=StepSource(
                        type="reviewer_added",
                        reviewer_ref=reviewer_ref,
                    ),
                ),
                placement="BEFORE_ALL",
            ),
            CompilerCheckpoint(step=membership, placement="BEFORE_ALL"),
            CompilerCheckpoint(step=read_available, placement="AFTER_ALL"),
            CompilerCheckpoint(
                step=BundleStep(
                    id="verify_completion",
                    kind="VERIFY",
                    source=StepSource(type="declared"),
                ),
                placement="AFTER_ALL",
            ),
        ),
        recoveries=(
            RecoveryDefinition(
                ref="readonly_overview_anchor",
                anchor_target_ref="overview_nav",
                max_attempts=2,
            ),
        ),
    )
