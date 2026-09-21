from typing import Annotated, Literal, Union

from pydantic import Field, StringConstraints

from cua.models.base import StrictModel


ObservationId = Annotated[str, StringConstraints(min_length=1, max_length=96)]
ControlRef = Annotated[str, StringConstraints(pattern=r"^c_[A-Za-z0-9_-]+$", max_length=64)]
OptionRef = Annotated[str, StringConstraints(pattern=r"^o_[A-Za-z0-9_-]+$", max_length=64)]
ValueRef = Annotated[
    str,
    StringConstraints(pattern=r"^(inputs|constants)\.[a-z][a-z0-9_]*$", max_length=96),
]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
Rationale = Annotated[str, StringConstraints(max_length=160)]


class _DecisionFields(StrictModel):
    observation_id: ObservationId
    reason_code: ReasonCode
    rationale: Rationale


class ClickDecision(_DecisionFields):
    operation: Literal["CLICK"]
    control_ref: ControlRef


class TypeTextDecision(_DecisionFields):
    operation: Literal["TYPE_TEXT"]
    control_ref: ControlRef
    value_ref: ValueRef


class SelectDecision(_DecisionFields):
    operation: Literal["SELECT"]
    control_ref: ControlRef
    option_ref: OptionRef


class ScrollDecision(_DecisionFields):
    operation: Literal["SCROLL"]
    control_ref: ControlRef
    direction: Literal["UP", "DOWN"]
    pixels: int = Field(gt=0, le=1000)


class WaitDecision(_DecisionFields):
    operation: Literal["WAIT"]
    timeout_ms: int = Field(ge=0, le=5000)


class DoneDecision(_DecisionFields):
    operation: Literal["DONE"]


class BlockedDecision(_DecisionFields):
    operation: Literal["BLOCKED"]


Decision = Annotated[
    Union[
        ClickDecision,
        TypeTextDecision,
        SelectDecision,
        ScrollDecision,
        WaitDecision,
        DoneDecision,
        BlockedDecision,
    ],
    Field(discriminator="operation"),
]
