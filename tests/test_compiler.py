import pytest

from cua.compiler import CapabilityCompiler, CompilationError
from cua.models.bundles import (
    BundleStep,
    CapabilityBlueprint,
    CompilerCheckpoint,
    StepSource,
)
from cua.models.conditions import (
    AccountPresentCondition,
    AllCondition,
    ConditionReference,
    PrincipalMatchesCondition,
)
from cua.models.traces import CompletionProof, VerifiedDiscoveryTrace, VerifiedTraceEvent
from cua.models.actions import DoneDecision


def _blueprint(bundle):
    return CapabilityBlueprint(
        schema_version=bundle.schema_version,
        capability=bundle.capability,
        compatibility=bundle.compatibility,
        contract=bundle.contract,
        targets=bundle.targets,
        conditions=bundle.conditions,
        recoveries=bundle.recoveries,
        parsers=bundle.parsers,
        profile_overrides=bundle.profile_overrides,
        profile_signals=bundle.profile_signals,
        checkpoints=(
            CompilerCheckpoint(
                step=_membership_checkpoint(),
                placement="BEFORE_ALL",
            ),
            CompilerCheckpoint(
                step=_completion_checkpoint(),
                placement="AFTER_ALL",
            ),
        ),
    )


def _step(step_id, kind, target_ref, event_id, **fields):
    return BundleStep(
        id=step_id,
        kind=kind,
        target_ref=target_ref,
        source=StepSource(type="observed", event_ids=(event_id,)),
        **fields,
    )


def test_compiler_rejects_done_decision_and_trace_without_completion_proof():
    bundle = _sample_bundle()
    blueprint = _blueprint(bundle)
    forged_done = DoneDecision(
        operation="DONE",
        observation_id="obs_1",
        reason_code="DONE",
        rationale="Claim completion without verification.",
    )

    with pytest.raises(CompilationError, match="verified discovery trace"):
        CapabilityCompiler().compile(forged_done, blueprint)

    trace = VerifiedDiscoveryTrace(
        trace_id="trace_2",
        success=True,
        completion_proof=None,
        events=(
            VerifiedTraceEvent(
                event_id="event_2",
                step=_step("open_details", "CLICK", "requested_account_link", "event_2"),
                effect_state="VERIFIED",
            ),
        ),
    )
    with pytest.raises(CompilationError, match="completion proof"):
        CapabilityCompiler().compile(trace, blueprint)


def test_compiler_preserves_verified_events_without_canned_workflow():
    bundle = _sample_bundle()
    blueprint = _blueprint(bundle)
    trace = VerifiedDiscoveryTrace(
        trace_id="trace_3",
        success=True,
        completion_proof=CompletionProof(
            proof_id="proof_3",
            trace_id="trace_3",
            session_ref="session_3",
            account_binding_ref="inputs.account_id",
            observation_id="obs_9",
            membership_proof_id="membership_3",
            authentication_generation=1,
        ),
        events=(
            VerifiedTraceEvent(
                event_id="event_3",
                step=_step("open_details", "CLICK", "requested_account_link", "event_3"),
                effect_state="VERIFIED",
            ),
            VerifiedTraceEvent(
                event_id="event_4",
                step=_step(
                    "read_balance",
                    "EXTRACT",
                    "detail_available_balance",
                    "event_4",
                    output_ref="available_balance",
                    parser_id="USD_DECIMAL_V1",
                    parser_version="1",
                ),
                effect_state="VERIFIED",
            ),
        ),
    )

    compiled = CapabilityCompiler().compile(trace, blueprint)

    assert [step.id for step in compiled.steps] == [
        "verify_membership",
        "open_details",
        "read_balance",
        "verify_completion",
    ]
    assert [step.source.event_ids for step in compiled.steps] == [
        (),
        ("event_3",),
        ("event_4",),
        (),
    ]
    assert compiled.provenance.trace_id == "trace_3"
    assert compiled.provenance.completion_proof_id == "proof_3"
    assert "open_overview" not in {step.id for step in compiled.steps}


def test_compiler_inserts_declared_checkpoints_without_faking_observed_events():
    bundle = _sample_bundle()
    blueprint = _blueprint(bundle).model_copy(
        update={
            "checkpoints": (
                CompilerCheckpoint(
                    step=_membership_checkpoint(),
                    placement="BEFORE_EVENT",
                    event_id="event_3",
                ),
                CompilerCheckpoint(
                    step=_completion_checkpoint("verify_completion"),
                    placement="AFTER_ALL",
                ),
            )
        }
    )
    trace = _successful_trace(
        "trace_5",
        (
            VerifiedTraceEvent(
                event_id="event_3",
                step=_step("open_details", "CLICK", "requested_account_link", "event_3"),
                effect_state="VERIFIED",
            ),
        ),
    )

    compiled = CapabilityCompiler().compile(trace, blueprint)

    assert [step.id for step in compiled.steps] == [
        "verify_membership",
        "open_details",
        "verify_completion",
    ]
    assert compiled.steps[0].source.type == "declared"
    assert compiled.steps[1].source == StepSource(type="observed", event_ids=("event_3",))
    assert compiled.steps[2].source.type == "declared"


def test_compiler_rejects_any_checkpoint_after_final_verify():
    bundle = _sample_bundle()
    blueprint = _blueprint(bundle).model_copy(
        update={
            "checkpoints": (
                CompilerCheckpoint(
                    step=_membership_checkpoint(),
                    placement="BEFORE_ALL",
                ),
                CompilerCheckpoint(
                    step=_completion_checkpoint(),
                    placement="AFTER_ALL",
                ),
                CompilerCheckpoint(
                    step=_membership_checkpoint("late_membership_check"),
                    placement="AFTER_ALL",
                ),
            )
        }
    )
    trace = _successful_trace(
        "trace_6",
        (
            VerifiedTraceEvent(
                event_id="event_6",
                step=_step("read_balance", "EXTRACT", "detail_available_balance", "event_6", output_ref="available_balance", parser_id="USD_DECIMAL_V1", parser_version="1"),
                effect_state="VERIFIED",
            ),
        ),
    )

    with pytest.raises(CompilationError, match="VERIFY must be the final checkpoint"):
        CapabilityCompiler().compile(trace, blueprint)


def _membership_checkpoint(step_id="verify_membership"):
    return BundleStep(
        id=step_id,
        kind="ASSERT",
        preconditions=(
            AllCondition(
                kind="all",
                conditions=(
                    ConditionReference(kind="condition_ref", name="overview_ready"),
                    PrincipalMatchesCondition(kind="principal_matches"),
                    AccountPresentCondition(
                        kind="account_present", input_ref="inputs.account_id"
                    ),
                ),
            ),
        ),
        source=StepSource(type="declared"),
    )


def _completion_checkpoint(step_id="verify_completion"):
    return BundleStep(
        id=step_id,
        kind="VERIFY",
        source=StepSource(type="declared"),
    )


def test_compiler_rejects_any_unverified_effect_inside_success_trace():
    bundle = _sample_bundle()
    trace = VerifiedDiscoveryTrace(
        trace_id="trace_4",
        success=True,
        completion_proof=CompletionProof(
            proof_id="proof_4",
            trace_id="trace_4",
            session_ref="session_4",
            account_binding_ref="inputs.account_id",
            observation_id="obs_10",
            membership_proof_id="membership_4",
            authentication_generation=1,
        ),
        events=(
            VerifiedTraceEvent(
                event_id="event_5",
                step=_step("open_details", "CLICK", "requested_account_link", "event_5"),
                effect_state="OUTCOME_UNKNOWN",
            ),
        ),
    )

    with pytest.raises(CompilationError, match="verified effects"):
        CapabilityCompiler().compile(trace, _blueprint(bundle))


def _sample_bundle():
    from tests.test_bundle_registry import _bundle

    return _bundle()


def _successful_trace(trace_id, events):
    return VerifiedDiscoveryTrace(
        trace_id=trace_id,
        success=True,
        completion_proof=CompletionProof(
            proof_id="proof_5",
            trace_id=trace_id,
            session_ref="session_5",
            account_binding_ref="inputs.account_id",
            observation_id="obs_11",
            membership_proof_id="membership_5",
            authentication_generation=1,
        ),
        events=events,
    )
