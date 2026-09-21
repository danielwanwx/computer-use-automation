from typing import Annotated, Literal

from pydantic import Field, SecretStr, StringConstraints

from cua.models.base import StrictModel
from cua.models.bundles import Reference


AccountBindingRef = Literal["inputs.account_id"]
NativeOrigin = Annotated[
    str,
    StringConstraints(
        pattern=r"^http://(?:127\.0\.0\.1|localhost):8080$",
        min_length=20,
        max_length=48,
    ),
]


class MembershipProof(StrictModel):
    """Ephemeral evidence minted only after a complete overview membership check."""

    proof_ref: Reference
    run_ref: Reference
    session_ref: Reference
    authentication_generation: int = Field(ge=0)
    account_binding_ref: AccountBindingRef
    account_binding_value: SecretStr = Field(repr=False)
    overview_observation_ref: Reference
    overview_complete: Literal[True]
    account_present: Literal[True]
    verified_monotonic_ms: int = Field(ge=0)


class CompletionContext(StrictModel):
    """In-memory bindings used only for final account identity verification."""

    run_ref: Reference
    session_ref: Reference
    authentication_generation: int = Field(ge=0)
    account_binding_ref: AccountBindingRef
    target_origin: NativeOrigin
    approved_profile: Literal["parabank-native-v1"]
    requested_account_id: SecretStr = Field(repr=False)
    membership_proof: MembershipProof


class CompletionView(StrictModel):
    """Redacted view metadata plus ephemeral protected detail fields."""

    run_ref: Reference
    session_ref: Reference
    observation_ref: Reference
    authentication_generation: int = Field(ge=0)
    origin: NativeOrigin
    profile_id: Literal["parabank-native-v1"]
    safe_route: Literal["account_details"]
    account_number_field_ref: Literal["PROFILE_ACCOUNT_NUMBER"]
    account_type_field_ref: Literal["PROFILE_ACCOUNT_TYPE"]
    available_balance_field_ref: Literal["PROFILE_AVAILABLE_BALANCE"]
    available_balance_parser_id: Literal["USD_DECIMAL_V1"]
    available_balance_parser_version: Literal["1"]
    available_balance_currency: Literal["USD"]
    page_state: Annotated[str, StringConstraints(min_length=1, max_length=48)]
    principal_matches: bool | None
    account_number_values: tuple[SecretStr, ...] = Field(max_length=4, repr=False)
    account_type_values: tuple[SecretStr, ...] = Field(max_length=4, repr=False)
    available_balance_values: tuple[SecretStr, ...] = Field(max_length=4, repr=False)
