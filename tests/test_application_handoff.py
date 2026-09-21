from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from cua.application.handoff import ApplicationHandoffCoordinator
from cua.evidence.models import SafeReasonCode
from cua.handoff import (
    HandoffError,
    HandoffService,
    HandoffState,
    ReconciliationDisposition,
    ReconciliationResult,
)
from cua.replay.runtime import HandoffRequest, TrustedHandoffContext
from cua.sessions import SessionActor


RUN = "run_0123456789abcdef"
SESSION = "s_0123456789abcdef"
PAGE = "p_0123456789ab"


def _service() -> HandoffService:
    return HandoffService(
        authorize_operator=lambda operator: operator in {"operator-a", "operator-b"}
    )


async def _next(_context):
    return ReconciliationResult(ReconciliationDisposition.NEXT)


def _request() -> HandoffRequest:
    return HandoffRequest(
        run_id=RUN,
        session_id=SESSION,
        step_id="read_balance",
        reason_code=SafeReasonCode.POSTCONDITION_UNKNOWN,
        ownership_epoch=0,
    )


def _trusted(actor: SessionActor, *, timeout: float = 2.0) -> TrustedHandoffContext:
    return TrustedHandoffContext(
        actor=actor,
        reconciler=_next,
        page_id=PAGE,
        deadline_monotonic=time.monotonic() + timeout,
    )


async def _start(coordinator, actor, *, timeout: float = 2.0):
    await actor.begin_run(RUN)
    states = []

    def report(view):
        states.append(view)

    coordinator._state_callback = report
    task = asyncio.create_task(
        coordinator.require_human(_request(), trusted=_trusted(actor, timeout=timeout))
    )
    while not states:
        await asyncio.sleep(0)
    while not coordinator._credentials:
        await asyncio.sleep(0)
    return task, states


def test_request_reports_waiting_claim_resume_safely_and_cleans_credential():
    async def scenario():
        coordinator = ApplicationHandoffCoordinator(_service())
        actor = SessionActor()
        request_task, states = await _start(coordinator, actor)
        intervention_id = states[0].intervention_id

        waiting = await coordinator.get(intervention_id, operator_ref="operator-a")
        assert waiting.state is HandoffState.WAITING_FOR_HUMAN
        assert (await coordinator.list(operator_ref="operator-a"))[0] == waiting

        claimed = await coordinator.claim(
            intervention_id,
            operator_ref="operator-a",
            expected_epoch=waiting.epoch,
        )
        resumed = await coordinator.resume(
            intervention_id,
            operator_ref="operator-a",
            expected_epoch=claimed.epoch,
        )
        result = await asyncio.wait_for(request_task, timeout=0.3)

        assert claimed.state is HandoffState.HUMAN_CLAIMED
        assert resumed.state is HandoffState.RUNNING
        assert result.state is HandoffState.RUNNING
        assert [view.state for view in states] == [
            HandoffState.WAITING_FOR_HUMAN,
            HandoffState.HUMAN_CLAIMED,
            HandoffState.RESUMING,
            HandoffState.RUNNING,
        ]
        assert coordinator._credentials == {}

    asyncio.run(scenario())


def test_wrong_operator_cannot_resume_server_held_credential():
    async def scenario():
        coordinator = ApplicationHandoffCoordinator(_service())
        actor = SessionActor()
        request_task, states = await _start(coordinator, actor)
        intervention_id = states[0].intervention_id
        waiting = await coordinator.get(intervention_id, operator_ref="operator-a")
        claimed = await coordinator.claim(
            intervention_id,
            operator_ref="operator-a",
            expected_epoch=waiting.epoch,
        )

        with pytest.raises(HandoffError, match="INTERVENTION_OPERATOR_FORBIDDEN"):
            await coordinator.resume(
                intervention_id,
                operator_ref="operator-b",
                expected_epoch=claimed.epoch,
            )

        resumed = await coordinator.resume(
            intervention_id,
            operator_ref="operator-a",
            expected_epoch=claimed.epoch,
        )
        assert resumed.state is HandoffState.RUNNING
        assert (await request_task).state is HandoffState.RUNNING

    asyncio.run(scenario())


def test_stale_client_epoch_is_not_replaced_with_new_server_epoch():
    async def scenario():
        coordinator = ApplicationHandoffCoordinator(_service())
        actor = SessionActor()
        request_task, states = await _start(coordinator, actor)
        intervention_id = states[0].intervention_id
        waiting = await coordinator.get(intervention_id, operator_ref="operator-a")
        claimed = await coordinator.claim(
            intervention_id,
            operator_ref="operator-a",
            expected_epoch=waiting.epoch,
        )

        with pytest.raises(HandoffError, match="INTERVENTION_STALE_EPOCH"):
            await coordinator.resume(
                intervention_id,
                operator_ref="operator-a",
                expected_epoch=waiting.epoch,
            )

        resumed = await coordinator.resume(
            intervention_id,
            operator_ref="operator-a",
            expected_epoch=claimed.epoch,
        )
        assert resumed.state is HandoffState.RUNNING
        assert (await request_task).state is HandoffState.RUNNING

    asyncio.run(scenario())


def test_abort_and_deadline_timeout_remove_private_credential():
    async def scenario():
        coordinator = ApplicationHandoffCoordinator(_service())
        actor = SessionActor()
        request_task, states = await _start(coordinator, actor)
        intervention_id = states[0].intervention_id
        waiting = await coordinator.get(intervention_id, operator_ref="operator-a")
        aborted = await coordinator.abort(
            intervention_id,
            operator_ref="operator-a",
            expected_epoch=waiting.epoch,
        )
        result = await request_task
        assert aborted.state is HandoffState.ABORTED
        assert result.state is HandoffState.ABORTED
        assert coordinator._credentials == {}

        timeout_actor = SessionActor()
        timeout_task, timeout_states = await _start(
            coordinator,
            timeout_actor,
            timeout=0.05,
        )
        timeout_result = await asyncio.wait_for(timeout_task, timeout=0.3)
        assert timeout_states[0].state is HandoffState.WAITING_FOR_HUMAN
        assert timeout_result.state is HandoffState.TIMED_OUT
        assert coordinator._credentials == {}

    asyncio.run(scenario())


def test_views_and_public_methods_never_expose_token():
    async def scenario():
        coordinator = ApplicationHandoffCoordinator(_service())
        actor = SessionActor()
        request_task, states = await _start(coordinator, actor)
        intervention_id = states[0].intervention_id
        credential = next(iter(coordinator._credentials.values()))
        token = credential._token

        views = await coordinator.list(operator_ref="operator-a")
        view = await coordinator.get(intervention_id, operator_ref="operator-a")
        assert token not in repr(views)
        assert token not in repr(view)
        assert token not in repr(coordinator)
        for method_name in ("claim", "resume", "abort"):
            assert "credential" not in inspect.signature(
                getattr(coordinator, method_name)
            ).parameters

        waiting = await coordinator.get(intervention_id, operator_ref="operator-a")
        aborted = await coordinator.abort(
            intervention_id,
            operator_ref="operator-a",
            expected_epoch=waiting.epoch,
        )
        assert token not in repr(aborted)

    asyncio.run(scenario())
