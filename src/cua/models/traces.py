from typing import Literal

from pydantic import Field, model_validator

from cua.models.base import StrictModel
from cua.models.bundles import Reference, BundleStep


class CompletionProof(StrictModel):
    proof_id: Reference
    trace_id: Reference
    session_ref: Reference
    account_binding_ref: Reference
    observation_id: Reference
    membership_proof_id: Reference
    authentication_generation: int = Field(ge=0)


class VerifiedTraceEvent(StrictModel):
    event_id: Reference
    step: BundleStep
    effect_state: Literal["NOT_DISPATCHED", "DISPATCHED", "VERIFIED", "OUTCOME_UNKNOWN"]


class VerifiedDiscoveryTrace(StrictModel):
    trace_id: Reference
    success: bool
    completion_proof: CompletionProof | None = None
    events: tuple[VerifiedTraceEvent, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_trace_refs(self):
        if self.completion_proof is not None and self.completion_proof.trace_id != self.trace_id:
            raise ValueError("completion proof must belong to this trace")
        event_ids = [event.event_id for event in self.events]
        step_ids = [event.step.id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("trace event IDs must be unique")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("trace step IDs must be unique")
        return self
