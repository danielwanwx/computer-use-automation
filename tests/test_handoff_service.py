from __future__ import annotations

import asyncio

import pytest

from cua.evidence.models import SafeReasonCode
from cua.handoff import (
    HandoffError,
    HandoffService,
    HandoffState,
    InterventionCredential,
    ReconciliationDisposition,
    ReconciliationResult,
)
from cua.sessions.actor import SessionActor


def _service() -> HandoffService:
    return HandoffService(
        authorize_operator=lambda operator: operator in {"operator-a", "operator-b"}
    )


def test_claimant_binding_rejects_other_authorized_operator_on_resume_and_abort():
    async def scenario():
        service = _service()
        actor = SessionActor()
        await actor.begin_run("run-a")
        delivered: list[InterventionCredential] = []

        async def deliver(credential: InterventionCredential) -> None:
            delivered.append(credential)

        async def reconcile(_context):
            return ReconciliationResult(ReconciliationDisposition.NEXT)

        request = asyncio.create_task(
            service.request(
                actor=actor,
                session_id="session-a",
                run_id="run-a",
                step_id="read_balance",
                reason=SafeReasonCode.POSTCONDITION_UNKNOWN,
                page_id="page-a",
                reconciler=reconcile,
                token_delivery=deliver,
                timeout_seconds=2,
            )
        )
        while not delivered:
            await asyncio.sleep(0)
        credential = delivered[0]
        waiting = await service.get(credential.intervention_id, operator_ref="operator-a")
        claimed = await service.claim(
            credential.intervention_id,
            operator_ref="operator-a",
            credential=credential,
            expected_epoch=waiting.epoch,
        )

        with pytest.raises(HandoffError, match="INTERVENTION_OPERATOR_FORBIDDEN"):
            await service.resume(
                credential.intervention_id,
                operator_ref="operator-b",
                credential=credential,
                expected_epoch=claimed.epoch,
            )
        with pytest.raises(HandoffError, match="INTERVENTION_OPERATOR_FORBIDDEN"):
            await service.abort(
                credential.intervention_id,
                operator_ref="operator-b",
                credential=credential,
                expected_epoch=claimed.epoch,
            )

        resumed = await service.resume(
            credential.intervention_id,
            operator_ref="operator-a",
            credential=credential,
            expected_epoch=claimed.epoch,
        )
        assert resumed.state is HandoffState.RUNNING
        result = await request
        assert result.state is HandoffState.RUNNING

    asyncio.run(scenario())


def test_terminal_resolution_clears_active_session_and_allows_second_intervention():
    async def scenario():
        service = _service()
        actor = SessionActor()
        await actor.begin_run("run-a")
        delivered: list[InterventionCredential] = []

        async def deliver(credential: InterventionCredential) -> None:
            delivered.append(credential)

        async def reconcile(_context):
            return ReconciliationResult(ReconciliationDisposition.NEXT)

        first = asyncio.create_task(
            service.request(
                actor=actor,
                session_id="session-a",
                run_id="run-a",
                step_id="read_balance",
                reason=SafeReasonCode.POSTCONDITION_UNKNOWN,
                page_id="page-a",
                reconciler=reconcile,
                token_delivery=deliver,
                timeout_seconds=2,
            )
        )
        while len(delivered) < 1:
            await asyncio.sleep(0)
        first_credential = delivered[0]
        first_waiting = await service.get(
            first_credential.intervention_id,
            operator_ref="operator-a",
        )
        first_claimed = await service.claim(
            first_credential.intervention_id,
            operator_ref="operator-a",
            credential=first_credential,
            expected_epoch=first_waiting.epoch,
        )
        first_view = await service.resume(
            first_credential.intervention_id,
            operator_ref="operator-a",
            credential=first_credential,
            expected_epoch=first_claimed.epoch,
        )
        assert first_view.state is HandoffState.RUNNING
        assert (await first).state is HandoffState.RUNNING
        assert service._by_session == {}

        second = asyncio.create_task(
            service.request(
                actor=actor,
                session_id="session-a",
                run_id="run-a",
                step_id="read_balance",
                reason=SafeReasonCode.POSTCONDITION_UNKNOWN,
                page_id="page-a",
                reconciler=reconcile,
                token_delivery=deliver,
                timeout_seconds=2,
            )
        )
        while len(delivered) < 2:
            await asyncio.sleep(0)
        second_credential = delivered[1]
        second_waiting = await service.get(
            second_credential.intervention_id,
            operator_ref="operator-a",
        )
        second_view = await service.abort(
            second_credential.intervention_id,
            operator_ref="operator-a",
            credential=second_credential,
            expected_epoch=second_waiting.epoch,
        )
        assert second_view.state is HandoffState.ABORTED
        assert (await second).state is HandoffState.ABORTED
        assert service._by_session == {}

        # Terminal records remain safe history, but protected references are gone.
        record = service._records[first_credential.intervention_id]
        assert record.credential is None
        assert record.reconciler is None
        assert record.token_delivery is None
        assert record.state_callback is None

    asyncio.run(scenario())


def test_reconciler_timeout_restores_human_and_resolves_request():
    async def scenario():
        service = _service()
        actor = SessionActor()
        await actor.begin_run("run-a")
        delivered: list[InterventionCredential] = []
        started = asyncio.Event()
        never = asyncio.Event()

        async def deliver(credential: InterventionCredential) -> None:
            delivered.append(credential)

        async def reconcile(_context):
            started.set()
            await never.wait()
            return ReconciliationResult(ReconciliationDisposition.NEXT)

        request = asyncio.create_task(
            service.request(
                actor=actor,
                session_id="session-a",
                run_id="run-a",
                step_id="read_balance",
                reason=SafeReasonCode.POSTCONDITION_UNKNOWN,
                page_id="page-a",
                reconciler=reconcile,
                token_delivery=deliver,
                timeout_seconds=0.1,
            )
        )
        while not delivered:
            await asyncio.sleep(0)
        credential = delivered[0]
        waiting = await service.get(credential.intervention_id, operator_ref="operator-a")
        claimed = await service.claim(
            credential.intervention_id,
            operator_ref="operator-a",
            credential=credential,
            expected_epoch=waiting.epoch,
        )
        resume = asyncio.create_task(
            service.resume(
                credential.intervention_id,
                operator_ref="operator-a",
                credential=credential,
                expected_epoch=claimed.epoch,
            )
        )
        await started.wait()
        with pytest.raises(HandoffError, match="INTERVENTION_RECONCILIATION_TIMEOUT"):
            await asyncio.wait_for(resume, timeout=0.3)
        assert actor.snapshot.owner == "HUMAN"
        # The request owner also reaches a safe terminal result after the
        # reconciliation cancellation instead of being left pending.
        result = await asyncio.wait_for(request, timeout=0.3)
        assert result.state in {HandoffState.ABORTED, HandoffState.TIMED_OUT}
        assert (await service.get(credential.intervention_id, operator_ref="operator-a")).state is result.state
        assert request.done()

    asyncio.run(scenario())


def test_reconciler_failure_before_deadline_remains_claimable():
    async def scenario():
        service = _service()
        actor = SessionActor()
        await actor.begin_run("run-a")
        delivered: list[InterventionCredential] = []

        async def deliver(credential: InterventionCredential) -> None:
            delivered.append(credential)

        async def reconcile(_context):
            raise RuntimeError("synthetic reconciliation failure")

        request = asyncio.create_task(
            service.request(
                actor=actor,
                session_id="session-a",
                run_id="run-a",
                step_id="read_balance",
                reason=SafeReasonCode.POSTCONDITION_UNKNOWN,
                page_id="page-a",
                reconciler=reconcile,
                token_delivery=deliver,
                timeout_seconds=2,
            )
        )
        while not delivered:
            await asyncio.sleep(0)
        credential = delivered[0]
        waiting = await service.get(credential.intervention_id, operator_ref="operator-a")
        claimed = await service.claim(
            credential.intervention_id,
            operator_ref="operator-a",
            credential=credential,
            expected_epoch=waiting.epoch,
        )
        with pytest.raises(HandoffError, match="INTERVENTION_RECONCILIATION_FAILED"):
            await service.resume(
                credential.intervention_id,
                operator_ref="operator-a",
                credential=credential,
                expected_epoch=claimed.epoch,
            )
        view = await service.get(credential.intervention_id, operator_ref="operator-a")
        assert view.state is HandoffState.HUMAN_CLAIMED
        assert actor.snapshot.owner == "HUMAN"

        aborted = await service.abort(
            credential.intervention_id,
            operator_ref="operator-a",
            credential=credential,
            expected_epoch=view.epoch,
        )
        assert aborted.state is HandoffState.ABORTED
        assert (await request).state is HandoffState.ABORTED

    asyncio.run(scenario())


def test_success_preserves_private_reconciliation_context_but_not_repr():
    async def scenario():
        service = _service()
        actor = SessionActor()
        await actor.begin_run("run-a")
        delivered: list[InterventionCredential] = []
        protected = object()

        async def deliver(credential: InterventionCredential) -> None:
            delivered.append(credential)

        async def reconcile(_context):
            return ReconciliationResult(
                ReconciliationDisposition.NEXT,
                context=protected,
            )

        request = asyncio.create_task(
            service.request(
                actor=actor,
                session_id="session-a",
                run_id="run-a",
                step_id="read_balance",
                reason=SafeReasonCode.POSTCONDITION_UNKNOWN,
                page_id="page-a",
                reconciler=reconcile,
                token_delivery=deliver,
                timeout_seconds=2,
            )
        )
        while not delivered:
            await asyncio.sleep(0)
        credential = delivered[0]
        waiting = await service.get(credential.intervention_id, operator_ref="operator-a")
        claimed = await service.claim(
            credential.intervention_id,
            operator_ref="operator-a",
            credential=credential,
            expected_epoch=waiting.epoch,
        )
        resumed = await service.resume(
            credential.intervention_id,
            operator_ref="operator-a",
            credential=credential,
            expected_epoch=claimed.epoch,
        )
        result = await request
        assert resumed.state is HandoffState.RUNNING
        assert result.context is protected
        assert "protected" not in repr(result)
        assert "object at" not in repr(result)

    asyncio.run(scenario())


def test_abort_has_no_private_context_and_view_has_no_context_field():
    async def scenario():
        service = _service()
        actor = SessionActor()
        await actor.begin_run("run-a")
        delivered: list[InterventionCredential] = []

        async def deliver(credential: InterventionCredential) -> None:
            delivered.append(credential)

        async def reconcile(_context):
            return ReconciliationResult(ReconciliationDisposition.NEXT, context=object())

        request = asyncio.create_task(
            service.request(
                actor=actor,
                session_id="session-a",
                run_id="run-a",
                step_id="read_balance",
                reason=SafeReasonCode.POSTCONDITION_UNKNOWN,
                page_id="page-a",
                reconciler=reconcile,
                token_delivery=deliver,
                timeout_seconds=2,
            )
        )
        while not delivered:
            await asyncio.sleep(0)
        credential = delivered[0]
        waiting = await service.get(credential.intervention_id, operator_ref="operator-a")
        aborted = await service.abort(
            credential.intervention_id,
            operator_ref="operator-a",
            credential=credential,
            expected_epoch=waiting.epoch,
        )
        result = await request
        assert aborted.state is HandoffState.ABORTED
        assert result.context is None
        assert "context" not in {item.name for item in __import__("dataclasses").fields(type(aborted))}

    asyncio.run(scenario())
