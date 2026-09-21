from pydantic import ValidationError

from cua.models.bundles import (
    BundleStep,
    CapabilityBlueprint,
    CapabilityBundle,
    CompilerCheckpoint,
    TraceProvenance,
    StepSource,
)
from cua.models.conditions import (
    AccountPresentCondition,
    AllCondition,
    ConditionReference,
    OverviewCompleteCondition,
    PrincipalMatchesCondition,
)
from cua.models.traces import VerifiedDiscoveryTrace
from cua.registry.bundle_registry import InvalidBundleError, _check_static_references


class CompilationError(ValueError):
    def __init__(self, code: str, findings: tuple[str, ...] = ()) -> None:
        self.code = code
        self.findings = findings
        super().__init__(code)


class CapabilityCompiler:
    """Turn only verified observed events into a new static-checkable draft."""

    def compile(
        self,
        trace: VerifiedDiscoveryTrace,
        blueprint: CapabilityBlueprint,
    ) -> CapabilityBundle:
        if not isinstance(trace, VerifiedDiscoveryTrace):
            raise CompilationError("verified discovery trace required")
        if not isinstance(blueprint, CapabilityBlueprint):
            raise CompilationError("capability blueprint required")
        try:
            trace = VerifiedDiscoveryTrace.model_validate(trace.model_dump(mode="python"))
            blueprint = CapabilityBlueprint.model_validate(blueprint.model_dump(mode="python"))
        except (AttributeError, ValidationError) as error:
            raise CompilationError("invalid compiler input") from error

        if trace.success is not True:
            raise CompilationError("discovery trace is not successful")
        if trace.completion_proof is None:
            raise CompilationError("completion proof required")
        if trace.completion_proof.trace_id != trace.trace_id:
            raise CompilationError("completion proof does not match trace")
        if trace.completion_proof.account_binding_ref != "inputs.account_id":
            raise CompilationError("completion proof is not bound to the requested account input")
        if not trace.events:
            raise CompilationError("verified trace contains no observed events")
        if len(trace.events) > 32:
            raise CompilationError("trace exceeds the 32-step capability limit")

        observed_steps: list[BundleStep] = []
        for event in trace.events:
            if event.effect_state != "VERIFIED":
                raise CompilationError("all compiled events must have verified effects")
            if event.step.kind in {"ASSERT", "VERIFY"}:
                raise CompilationError("ASSERT and VERIFY belong in declared checkpoints")
            if event.step.source.type != "observed":
                raise CompilationError("compiler accepts observed events only")
            if event.step.source.event_ids != (event.event_id,):
                raise CompilationError("step source must identify its exact trace event")
            try:
                observed_steps.append(BundleStep.model_validate(event.step.model_dump(mode="python")))
            except ValidationError as error:
                raise CompilationError("trace event contains an invalid action step") from error

        checkpoints = self._validate_checkpoints(blueprint, trace)
        steps = self._place_checkpoints(checkpoints, trace, observed_steps)

        try:
            bundle = CapabilityBundle(
                schema_version=blueprint.schema_version,
                capability=blueprint.capability,
                compatibility=blueprint.compatibility,
                contract=blueprint.contract,
                steps=tuple(steps),
                targets=blueprint.targets,
                conditions=blueprint.conditions,
                recoveries=blueprint.recoveries,
                parsers=blueprint.parsers,
                profile_overrides=blueprint.profile_overrides,
                profile_signals=blueprint.profile_signals,
                provenance=TraceProvenance(
                    trace_id=trace.trace_id,
                    verified=True,
                    completion_proof_id=trace.completion_proof.proof_id,
                ),
            )
            _check_static_references(bundle)
        except InvalidBundleError as error:
            raise CompilationError("compiled bundle failed static closure checks", error.findings) from error
        except ValidationError as error:
            raise CompilationError("compiled bundle failed schema checks") from error
        return bundle

    @staticmethod
    def _validate_checkpoints(blueprint: CapabilityBlueprint, trace: VerifiedDiscoveryTrace):
        checkpoint_ids = {checkpoint.step.id for checkpoint in blueprint.checkpoints}
        event_to_index = {event.event_id: index for index, event in enumerate(trace.events)}
        if len(checkpoint_ids) != len(blueprint.checkpoints):
            raise CompilationError("checkpoint step IDs must be unique")
        for checkpoint in blueprint.checkpoints:
            if checkpoint.step.id in {event.step.id for event in trace.events}:
                raise CompilationError("checkpoint cannot replace an observed step")
            if checkpoint.event_id is not None and checkpoint.event_id not in event_to_index:
                raise CompilationError("checkpoint references an unknown trace event")

        verify_checkpoints = tuple(
            checkpoint for checkpoint in blueprint.checkpoints if checkpoint.step.kind == "VERIFY"
        )
        if (
            len(verify_checkpoints) != 1
            or verify_checkpoints[0].placement != "AFTER_ALL"
            or verify_checkpoints[0].step.source.type != "declared"
        ):
            raise CompilationError("one final runtime-owned VERIFY checkpoint is required")
        verify_index = next(
            index
            for index, checkpoint in enumerate(blueprint.checkpoints)
            if checkpoint.step.kind == "VERIFY"
        )
        if any(
            checkpoint.placement == "AFTER_ALL"
            for checkpoint in blueprint.checkpoints[verify_index + 1 :]
        ):
            raise CompilationError("VERIFY must be the final checkpoint")

        account_targets = {
            target.ref for target in blueprint.targets if target.binding_ref == "inputs.account_id"
        }
        bound_event_indexes = [
            index
            for index, event in enumerate(trace.events)
            if event.step.target_ref in account_targets
        ]
        first_bound_event = min(bound_event_indexes) if bound_event_indexes else 0
        membership_checkpoints = []
        for checkpoint in blueprint.checkpoints:
            step = checkpoint.step
            if step.kind != "ASSERT":
                continue
            if step.source.type != "declared":
                continue
            if not _has_membership_preconditions(step.preconditions, blueprint):
                continue
            if _checkpoint_position(checkpoint, event_to_index, len(trace.events)) < first_bound_event:
                membership_checkpoints.append(checkpoint)
        if not membership_checkpoints:
            raise CompilationError(
                "a declared principal, complete-overview, and account-membership ASSERT must precede account use"
            )
        return blueprint.checkpoints

    @staticmethod
    def _place_checkpoints(
        checkpoints: tuple[CompilerCheckpoint, ...],
        trace: VerifiedDiscoveryTrace,
        observed_steps: list[BundleStep],
    ) -> list[BundleStep]:
        before_all: list[BundleStep] = []
        after_all: list[BundleStep] = []
        before_event: dict[str, list[BundleStep]] = {}
        after_event: dict[str, list[BundleStep]] = {}
        for checkpoint in checkpoints:
            step = BundleStep.model_validate(checkpoint.step.model_dump(mode="python"))
            if checkpoint.placement == "BEFORE_ALL":
                before_all.append(step)
            elif checkpoint.placement == "AFTER_ALL":
                after_all.append(step)
            elif checkpoint.placement == "BEFORE_EVENT":
                before_event.setdefault(checkpoint.event_id, []).append(step)
            else:
                after_event.setdefault(checkpoint.event_id, []).append(step)

        ordered = list(before_all)
        for event, observed_step in zip(trace.events, observed_steps, strict=True):
            ordered.extend(before_event.get(event.event_id, ()))
            ordered.append(observed_step)
            ordered.extend(after_event.get(event.event_id, ()))
        ordered.extend(after_all)
        return ordered


def _has_membership_preconditions(expressions, blueprint: CapabilityBlueprint) -> bool:
    available = set()
    definitions = {condition.name: condition.expression for condition in blueprint.conditions}
    for expression in expressions:
        available.update(_positive_condition_facts(expression, definitions, ()))
    return {
        ("principal_matches", None),
        ("overview_complete", None),
        ("account_present", "inputs.account_id"),
    }.issubset(available)


def _positive_condition_facts(expression, definitions, stack):
    if isinstance(expression, ConditionReference):
        if expression.name in stack or expression.name not in definitions:
            return set()
        return _positive_condition_facts(
            definitions[expression.name],
            definitions,
            (*stack, expression.name),
        )
    if isinstance(expression, AllCondition):
        facts = set()
        for child in expression.conditions:
            facts.update(_positive_condition_facts(child, definitions, stack))
        return facts
    if isinstance(expression, PrincipalMatchesCondition):
        return {("principal_matches", None)}
    if isinstance(expression, OverviewCompleteCondition):
        return {("overview_complete", None)}
    if isinstance(expression, AccountPresentCondition):
        return {("account_present", expression.input_ref)}
    # A fact beneath `any` or `not` cannot establish membership on every path.
    return set()


def _checkpoint_position(checkpoint, event_to_index, event_count):
    if checkpoint.placement == "BEFORE_ALL":
        return -1
    if checkpoint.placement == "AFTER_ALL":
        return event_count
    index = event_to_index[checkpoint.event_id]
    return index - 0.5 if checkpoint.placement == "BEFORE_EVENT" else index + 0.5
