from cua.discovery.blueprint import parabank_savings_balance_blueprint
from cua.compiler import CapabilityCompiler
from cua.models.bundles import BundleStep, StepSource
from cua.models.traces import CompletionProof, VerifiedDiscoveryTrace, VerifiedTraceEvent


def test_blueprint_keeps_reviewer_anchor_membership_and_final_verify_distinct():
    blueprint = parabank_savings_balance_blueprint(
        reviewer_ref="reviewer_blueprint_fixture",
        capability_version="1.0.0",
    )

    assert [item.step.kind for item in blueprint.checkpoints] == ["CLICK", "ASSERT", "EXTRACT", "VERIFY"]
    anchor, membership, extraction, completion = blueprint.checkpoints
    assert anchor.placement == "BEFORE_ALL"
    assert anchor.step.target_ref == "overview_nav"
    assert anchor.step.source.type == "reviewer_added"
    assert anchor.step.source.reviewer_ref == "reviewer_blueprint_fixture"
    assert membership.placement == "BEFORE_ALL"
    assert membership.step.source.type == "declared"
    assert extraction.placement == "AFTER_ALL"
    assert extraction.step.id == "read_available"
    assert extraction.step.kind == "EXTRACT"
    assert extraction.step.source.type == "declared"
    assert extraction.step.output_ref == "available_balance"
    assert completion.placement == "AFTER_ALL"
    assert completion.step.source.type == "declared"
    assert not any(
        checkpoint.step.source.type == "observed"
        for checkpoint in blueprint.checkpoints
    )


def test_blueprint_closure_compiles_a_synthetic_unit_trace():
    blueprint = parabank_savings_balance_blueprint(
        reviewer_ref="reviewer_blueprint_fixture",
        capability_version="1.0.0",
    )
    trace_id = "trace_blueprint_fixture"
    event_id = "event_blueprint_fixture"
    trace = VerifiedDiscoveryTrace(
        trace_id=trace_id,
        success=True,
        completion_proof=CompletionProof(
            proof_id="proof_blueprint_fixture",
            trace_id=trace_id,
            session_ref="session_blueprint_fixture",
            account_binding_ref="inputs.account_id",
            observation_id="observation_blueprint_fixture",
            membership_proof_id="membership_blueprint_fixture",
            authentication_generation=1,
        ),
        events=(
            VerifiedTraceEvent(
                event_id=event_id,
                effect_state="VERIFIED",
                step=BundleStep(
                    id="open_requested_account",
                    kind="CLICK",
                    target_ref="requested_account_link",
                    recovery_ref="readonly_overview_anchor",
                    source=StepSource(type="observed", event_ids=(event_id,)),
                ),
            ),
        ),
    )

    bundle = CapabilityCompiler().compile(trace, blueprint)

    assert [step.kind for step in bundle.steps] == ["CLICK", "ASSERT", "CLICK", "EXTRACT", "VERIFY"]
    assert bundle.steps[0].source.type == "reviewer_added"
    assert bundle.steps[1].source.type == "declared"
    assert bundle.steps[2].source == StepSource(type="observed", event_ids=(event_id,))
    assert bundle.steps[2].recovery_ref == "readonly_overview_anchor"
    assert bundle.steps[3].source.type == "declared"
    assert bundle.steps[3].kind == "EXTRACT"
    assert bundle.steps[-1].source.type == "declared"
