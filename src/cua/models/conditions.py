from typing import Annotated, Literal, Union

from pydantic import Field, StringConstraints

from cua.models.base import StrictModel


Reference = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]*$", max_length=96)]


class InputValueReference(StrictModel):
    kind: Literal["input_ref"]
    name: Reference


class ApprovedConstant(StrictModel):
    kind: Literal["constant"]
    value: Annotated[str, StringConstraints(min_length=1, max_length=80)]


ExpectedValue = Annotated[
    Union[InputValueReference, ApprovedConstant],
    Field(discriminator="kind"),
]


class ConditionReference(StrictModel):
    kind: Literal["condition_ref"]
    name: Reference


class AllCondition(StrictModel):
    kind: Literal["all"]
    conditions: tuple["ConditionExpr", ...] = Field(min_length=1, max_length=32)


class AnyCondition(StrictModel):
    kind: Literal["any"]
    conditions: tuple["ConditionExpr", ...] = Field(min_length=1, max_length=32)


class NotCondition(StrictModel):
    kind: Literal["not"]
    child: "ConditionExpr"


class PageStateCondition(StrictModel):
    kind: Literal["page_state"]
    value: Annotated[str, StringConstraints(min_length=1, max_length=48)]


class PrincipalMatchesCondition(StrictModel):
    kind: Literal["principal_matches"]


class OverviewCompleteCondition(StrictModel):
    kind: Literal["overview_complete"]


class AccountPresentCondition(StrictModel):
    kind: Literal["account_present"]
    input_ref: Reference


class MembershipValidCondition(StrictModel):
    kind: Literal["membership_valid"]
    input_ref: Reference


class FieldEqualsCondition(StrictModel):
    kind: Literal["field_equals"]
    target_ref: Reference
    expected: ExpectedValue


class ParseableCondition(StrictModel):
    kind: Literal["parseable"]
    target_ref: Reference
    parser_id: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)]
    parser_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class OutputValidCondition(StrictModel):
    kind: Literal["output_valid"]
    output_ref: Reference


class ExplicitDenialCondition(StrictModel):
    kind: Literal["explicit_denial"]
    signal_ref: Reference


ConditionExpr = Annotated[
    Union[
        ConditionReference,
        AllCondition,
        AnyCondition,
        NotCondition,
        PageStateCondition,
        PrincipalMatchesCondition,
        OverviewCompleteCondition,
        AccountPresentCondition,
        MembershipValidCondition,
        FieldEqualsCondition,
        ParseableCondition,
        OutputValidCondition,
        ExplicitDenialCondition,
    ],
    Field(discriminator="kind"),
]

AllCondition.model_rebuild()
AnyCondition.model_rebuild()
NotCondition.model_rebuild()
