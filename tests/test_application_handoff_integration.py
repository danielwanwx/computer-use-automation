from __future__ import annotations

import asyncio
import time

import pytest

from cua.application.service import _RunRecord
from cua.application import ApplicationConfig, ApplicationService
from cua.evidence.models import RunState, SafeReasonCode
from cua.handoff import HandoffError, HandoffState, InterventionView
from cua.application.contracts import RunMode, ServiceError

from tests.test_application_service import SESSION_ID, _service
from tests.test_application_service import _principal


RUN_ID = "run_0123456789abcdef"
INTERVENTION_ID = "iv_0123456789abcdef01234567"


def _view(state: HandoffState, *, epoch: int = 2) -> InterventionView:
    owner = {
        HandoffState.WAITING_FOR_HUMAN: "NONE",
        HandoffState.RUNNING: "RUNTIME",
        HandoffState.ABORTED: "HUMAN",
    }[state]
    return InterventionView(
        intervention_id=INTERVENTION_ID,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_id="read_balance",
        reason=SafeReasonCode.POSTCONDITION_UNKNOWN,
        state=state,
        owner=owner,
        epoch=epoch,
        page_id="p_0123456789ab",
    )


class _FakeCoordinator:
    def __init__(self, view: InterventionView):
        self.view = view
        self.calls: list[tuple[str, str, int | None]] = []

    async def list(self, *, operator_ref, session_id=None):
        self.calls.append(("list", operator_ref, None))
        return (self.view,)

    async def get(self, intervention_id, *, operator_ref):
        self.calls.append(("get", operator_ref, None))
        return self.view

    async def claim(self, intervention_id, *, operator_ref, expected_epoch):
        self.calls.append(("claim", operator_ref, expected_epoch))
        return self.view

    async def resume(self, intervention_id, *, operator_ref, expected_epoch):
        self.calls.append(("resume", operator_ref, expected_epoch))
        raise HandoffError("INTERVENTION_STALE_EPOCH")

    async def abort(self, intervention_id, *, operator_ref, expected_epoch):
        self.calls.append(("abort", operator_ref, expected_epoch))
        return self.view


def _install_run(service, sessions):
    now = int(time.time() * 1000)
    service._runs[RUN_ID] = _RunRecord(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        mode=RunMode.REPLAY,
        state=RunState.RUNNING,
        created_at_ms=now,
        updated_at_ms=now,
        actor=sessions.actor,
    )


def test_safe_handoff_callback_projects_run_state_and_id_atomically(tmp_path):
    async def scenario():
        service, sessions, _, _ = _service(tmp_path)
        _install_run(service, sessions)

        await service._on_intervention_state(_view(HandoffState.WAITING_FOR_HUMAN))
        waiting = await service.get_run(RUN_ID)
        assert waiting.state is RunState.WAITING_FOR_HUMAN
        assert waiting.intervention_id == INTERVENTION_ID

        await service._on_intervention_state(
            _view(HandoffState.RUNNING, epoch=3)
        )
        assert (await service.get_run(RUN_ID)).state is RunState.RUNNING

        await service._on_intervention_state(
            _view(HandoffState.ABORTED, epoch=4)
        )
        aborted = await service.get_run(RUN_ID)
        assert aborted.state is RunState.ABORTED
        assert aborted.intervention_id == INTERVENTION_ID

    asyncio.run(scenario())


def test_handoff_methods_use_local_operator_and_preserve_stale_epoch(tmp_path):
    async def scenario():
        service, _, _, _ = _service(tmp_path)
        fake = _FakeCoordinator(_view(HandoffState.WAITING_FOR_HUMAN))
        service._handoff_coordinator = fake

        views = await service.list_interventions()
        assert views[0].intervention_id == INTERVENTION_ID
        claimed = await service.claim_intervention(
            INTERVENTION_ID,
            expected_epoch=2,
        )
        assert claimed.state is HandoffState.WAITING_FOR_HUMAN
        with pytest.raises(ServiceError) as stale:
            await service.resume_intervention(
                INTERVENTION_ID,
                expected_epoch=1,
            )
        assert stale.value.status == 409
        assert stale.value.code == "INTERVENTION_STALE_EPOCH"
        with pytest.raises(ServiceError) as forbidden:
            await service.abort_intervention(
                INTERVENTION_ID,
                expected_epoch=2,
                operator_ref="operator-a",
            )
        assert forbidden.value.status == 403
        assert fake.calls == [
            ("list", "local_operator", None),
            ("claim", "local_operator", 2),
            ("resume", "local_operator", 1),
        ]

    asyncio.run(scenario())


def test_from_config_shares_server_owned_coordinator_with_replay(tmp_path):
    async def scenario():
        config = ApplicationConfig(
            data_root=tmp_path,
            principal_specs=(_principal(),),
        )
        service = ApplicationService.from_config(config)
        assert service._handoff_coordinator is not None
        assert service._replay._handoff_notifier is service._handoff_coordinator
        assert await service._handoff_coordinator.list(operator_ref="local_operator") == ()
        with pytest.raises(HandoffError, match="INTERVENTION_OPERATOR_FORBIDDEN"):
            await service._handoff_coordinator.list(operator_ref="operator-a")
        await service.shutdown()

    asyncio.run(scenario())
