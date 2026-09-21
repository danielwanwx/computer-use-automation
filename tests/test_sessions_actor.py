import asyncio
from dataclasses import FrozenInstanceError

import pytest

from cua.sessions.actor import (
    ActorBusy,
    ActorPaused,
    ActorStaleEpoch,
    ActorSnapshot,
    ResumeDisposition,
    SessionActor,
)


def test_actor_rejects_stale_epoch():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        with pytest.raises(ActorStaleEpoch):
            await actor.submit(
                expected_epoch=1,
                run_id="run-a",
                operation=_return("unused"),
            )

    asyncio.run(scenario())


def test_actor_drains_in_flight_command_before_human_claim():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_operation():
            started.set()
            await release.wait()
            return "done"

        command = asyncio.create_task(
            actor.submit(expected_epoch=0, run_id="run-a", operation=slow_operation)
        )
        await started.wait()
        pause = asyncio.create_task(actor.pause_and_drain(expected_epoch=0))
        await asyncio.sleep(0)
        assert not pause.done()
        with pytest.raises(ActorPaused):
            await actor.submit(
                expected_epoch=0,
                run_id="run-a",
                operation=_return("rejected"),
            )
        release.set()
        assert await command == "done"
        paused = await pause
        assert paused.owner == "PAUSING"
        assert paused.epoch == 0
        assert paused.active_run_id == "run-a"
        human = await actor.claim_human(expected_epoch=paused.epoch)
        assert human.owner == "HUMAN"
        assert human.epoch == 1
        with pytest.raises(ActorStaleEpoch):
            await actor.begin_resume(expected_epoch=0)
        resuming = await actor.begin_resume(expected_epoch=human.epoch)
        assert resuming.owner == "RESUMING"
        with pytest.raises(ActorPaused):
            await actor.submit(
                expected_epoch=resuming.epoch,
                run_id="run-a",
                operation=_return("must remain fenced"),
            )
        completed = await actor.complete_resume(
            expected_epoch=resuming.epoch,
            disposition=ResumeDisposition.RETRY_SAFE,
        )
        assert completed.owner == "RUNTIME"
        runtime_epoch = completed.epoch
        await actor.submit(
            expected_epoch=runtime_epoch,
            run_id="run-a",
            operation=_return("resumed"),
        )

    asyncio.run(scenario())


def test_actor_allows_only_one_active_run():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        with pytest.raises(ActorBusy):
            await actor.begin_run("run-b")
        with pytest.raises(ActorBusy):
            await actor.submit(
                expected_epoch=0,
                run_id="run-b",
                operation=_return("wrong owner"),
            )
        await actor.finish_run("run-a")
        assert actor.active_run_id is None

    asyncio.run(scenario())


def test_queued_command_is_fenced_by_pause_before_dispatch():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_effects = []

        async def first_operation():
            first_started.set()
            await release_first.wait()
            return "first"

        async def second_operation():
            second_effects.append("dispatched")

        first = asyncio.create_task(
            actor.submit(expected_epoch=0, run_id="run-a", operation=first_operation)
        )
        await first_started.wait()
        second = asyncio.create_task(
            actor.submit(expected_epoch=0, run_id="run-a", operation=second_operation)
        )
        while actor._accepted_commands < 2:
            await asyncio.sleep(0)
        pause = asyncio.create_task(actor.pause_and_drain(expected_epoch=0))
        await asyncio.sleep(0)
        release_first.set()
        assert await first == "first"
        with pytest.raises(ActorPaused):
            await second
        paused = await pause
        assert second_effects == []
        assert paused.owner == "PAUSING"
        await actor.claim_human(expected_epoch=paused.epoch)

    asyncio.run(scenario())


def test_canceled_submitter_does_not_release_drain_while_effect_runs():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        started = asyncio.Event()
        release = asyncio.Event()
        effects = []

        async def operation():
            started.set()
            await release.wait()
            effects.append("finished")

        command = asyncio.create_task(
            actor.submit(expected_epoch=0, run_id="run-a", operation=operation)
        )
        await started.wait()
        command.cancel()
        command.cancel()
        await asyncio.sleep(0)
        pause = asyncio.create_task(actor.pause_and_drain(expected_epoch=0))
        await asyncio.sleep(0)
        assert not pause.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await command
        paused = await pause
        assert effects == ["finished"]
        assert paused.owner == "PAUSING"
        await actor.claim_human(expected_epoch=paused.epoch)


def test_actor_rejects_untyped_resume_disposition():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        paused = await actor.pause_and_drain(expected_epoch=0)
        human = await actor.claim_human(expected_epoch=paused.epoch)
        resuming = await actor.begin_resume(expected_epoch=human.epoch)
        with pytest.raises(ValueError):
            await actor.complete_resume(
                expected_epoch=resuming.epoch,
                disposition="RETRY_SAFE",
            )

    asyncio.run(scenario())


def test_reconciliation_is_exact_run_epoch_and_resuming_only():
    async def scenario():
        actor = SessionActor()
        initial = actor.snapshot
        assert initial == ActorSnapshot("RUNTIME", 0, None)
        with pytest.raises(FrozenInstanceError):
            initial.epoch = 2
        await actor.begin_run("run-a")
        with pytest.raises(ActorPaused):
            await actor.submit_reconciliation(
                expected_epoch=0,
                run_id="run-a",
                operation=_return("must not run"),
            )
        assert await actor.submit(
            expected_epoch=0,
            run_id="run-a",
            operation=_return("runtime"),
        ) == "runtime"

        pause = await actor.pause_and_drain(expected_epoch=0)
        human = await actor.claim_human(expected_epoch=pause.epoch)
        with pytest.raises(ActorPaused):
            await actor.submit_reconciliation(
                expected_epoch=human.epoch,
                run_id="run-a",
                operation=_return("must not run"),
            )
        resuming = await actor.begin_resume(expected_epoch=human.epoch)
        assert actor.snapshot == ActorSnapshot("RESUMING", resuming.epoch, "run-a")

        with pytest.raises(ActorStaleEpoch):
            await actor.submit_reconciliation(
                expected_epoch=resuming.epoch - 1,
                run_id="run-a",
                operation=_return("must not run"),
            )
        with pytest.raises(ActorBusy):
            await actor.submit_reconciliation(
                expected_epoch=resuming.epoch,
                run_id="run-b",
                operation=_return("must not run"),
            )
        with pytest.raises(ActorStaleEpoch):
            await actor.complete_resume(
                expected_epoch=resuming.epoch - 1,
                disposition=ResumeDisposition.NEXT,
            )

        assert await actor.submit_reconciliation(
            expected_epoch=resuming.epoch,
            run_id="run-a",
            operation=_return("reconciled"),
        ) == "reconciled"
        runtime = await actor.complete_resume(
            expected_epoch=resuming.epoch,
            disposition=ResumeDisposition.NEXT,
        )
        assert actor.snapshot == ActorSnapshot(runtime.owner, runtime.epoch, "run-a")
        with pytest.raises(ActorPaused):
            await actor.complete_resume(
                expected_epoch=runtime.epoch,
                disposition=ResumeDisposition.NEXT,
            )

    asyncio.run(scenario())


def test_queued_reconciliation_is_serialized_and_cannot_be_overtaken_by_resume():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        paused = await actor.pause_and_drain(expected_epoch=0)
        human = await actor.claim_human(expected_epoch=paused.epoch)
        resuming = await actor.begin_resume(expected_epoch=human.epoch)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        order = []

        async def first_operation():
            order.append("first-start")
            first_started.set()
            await release_first.wait()
            order.append("first-finish")

        async def second_operation():
            order.append("second")

        first = asyncio.create_task(actor.submit_reconciliation(
            expected_epoch=resuming.epoch,
            run_id="run-a",
            operation=first_operation,
        ))
        await first_started.wait()
        second = asyncio.create_task(actor.submit_reconciliation(
            expected_epoch=resuming.epoch,
            run_id="run-a",
            operation=second_operation,
        ))
        while actor._accepted_commands != 2:
            await asyncio.sleep(0)
        with pytest.raises(ActorBusy):
            await actor.complete_resume(
                expected_epoch=resuming.epoch,
                disposition=ResumeDisposition.NEXT,
            )
        first.cancel()
        release_first.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        while actor._accepted_commands:
            await asyncio.sleep(0)
        assert order == ["first-start", "first-finish", "second"]
        completed = await actor.complete_resume(
            expected_epoch=resuming.epoch,
            disposition=ResumeDisposition.NEXT,
        )
        assert completed.owner == "RUNTIME"

    asyncio.run(scenario())


def test_complete_resume_cannot_overtake_cancelled_reconciliation():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        pause = await actor.pause_and_drain(expected_epoch=0)
        human = await actor.claim_human(expected_epoch=pause.epoch)
        resuming = await actor.begin_resume(expected_epoch=human.epoch)
        started = asyncio.Event()
        release = asyncio.Event()
        effects = []

        async def reconcile():
            started.set()
            await release.wait()
            effects.append("finished")

        command = asyncio.create_task(actor.submit_reconciliation(
            expected_epoch=resuming.epoch,
            run_id="run-a",
            operation=reconcile,
        ))
        await started.wait()
        with pytest.raises(ActorBusy):
            await actor.complete_resume(
                expected_epoch=resuming.epoch,
                disposition=ResumeDisposition.NEXT,
            )
        command.cancel()
        await asyncio.sleep(0)
        with pytest.raises(ActorBusy):
            await actor.complete_resume(
                expected_epoch=resuming.epoch,
                disposition=ResumeDisposition.NEXT,
            )
        assert actor.snapshot == ActorSnapshot("RESUMING", resuming.epoch, "run-a")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await command
        while actor._accepted_commands:
            await asyncio.sleep(0)
        assert effects == ["finished"]
        completed = await actor.complete_resume(
            expected_epoch=resuming.epoch,
            disposition=ResumeDisposition.NEXT,
        )
        assert completed.owner == "RUNTIME"

    asyncio.run(scenario())


def test_finish_run_waits_for_cancelled_submitter_effect_before_releasing_lease():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        started = asyncio.Event()
        release = asyncio.Event()

        async def operation():
            started.set()
            await release.wait()

        submitter = asyncio.create_task(
            actor.submit(expected_epoch=0, run_id="run-a", operation=operation)
        )
        await started.wait()
        submitter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submitter

        finish = asyncio.create_task(actor.finish_run("run-a"))
        await asyncio.sleep(0)
        assert not finish.done()
        assert actor.active_run_id == "run-a"

        release.set()
        await finish
        assert actor.active_run_id is None

    asyncio.run(scenario())


def test_wait_for_human_releases_runtime_at_fresh_none_epoch():
    async def scenario():
        actor = SessionActor()
        await actor.begin_run("run-a")
        pausing = await actor.pause_and_drain(expected_epoch=0)
        waiting = await actor.wait_for_human(expected_epoch=pausing.epoch)
        assert actor.snapshot == ActorSnapshot("NONE", 1, "run-a")
        assert waiting.owner == "NONE"
        assert waiting.epoch == 1
        with pytest.raises(ActorPaused):
            await actor.submit(
                expected_epoch=waiting.epoch,
                run_id="run-a",
                operation=_return("runtime must remain fenced"),
            )
        with pytest.raises(ActorStaleEpoch):
            await actor.claim_human(expected_epoch=pausing.epoch)
        human = await actor.claim_human(expected_epoch=waiting.epoch)
        assert human.owner == "HUMAN"
        assert human.epoch == 2

    asyncio.run(scenario())


def _return(value):
    async def operation():
        return value

    return operation
