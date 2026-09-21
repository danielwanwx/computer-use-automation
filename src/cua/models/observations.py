from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from cua.models.base import StrictModel


ControlOperation = Literal["CLICK", "TYPE_TEXT", "SELECT", "SCROLL"]
ControlRef = Annotated[str, StringConstraints(pattern=r"^c_[A-Za-z0-9_-]+$", max_length=64)]


class ObservedControl(StrictModel):
    ref: ControlRef
    frame_ref: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    role: Annotated[str, StringConstraints(min_length=1, max_length=48)]
    safe_name: Annotated[str, StringConstraints(max_length=120)]
    enabled: bool
    visible: bool
    allowed_operations: tuple[ControlOperation, ...]
    binding_ref: Annotated[str, StringConstraints(max_length=96)] | None = None


class Observation(StrictModel):
    id: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    session_id: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    page_id: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    document_generation: int = Field(ge=0)
    frame_generations: dict[str, int]
    captured_monotonic_ms: int = Field(ge=0)
    safe_route: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    state_tags: tuple[Annotated[str, StringConstraints(max_length=48)], ...]
    controls: tuple[ObservedControl, ...]
    safe_text: tuple[Annotated[str, StringConstraints(max_length=240)], ...]
    fingerprint: Annotated[str, StringConstraints(min_length=1, max_length=96)]
