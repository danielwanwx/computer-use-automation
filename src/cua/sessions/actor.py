"""Serial command submission and epoch-fenced session ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Awaitable, Callable, TypeVar


T = TypeVar("T")
Operation = Callable[[], Awaitable[T]]


class ActorError(RuntimeError):
    pass


class ActorPaused(ActorError):
    pass


class ActorStaleEpoch(ActorError):
    pass


class ActorBusy(ActorError):
    pass


class _Owner(StrEnum):
    RUNTIME = "RUNTIME"
    PAUSING = "PAUSING"
    NONE = "NONE"
    HUMAN = "HUMAN"
    RESUMING = "RESUMING"


@dataclass(frozen=True, slots=True)
class HandoffGrant:
    owner: str
    epoch: int
    active_run_id: str | None


@dataclass(frozen=True, slots=True)
class ActorSnapshot:
    """Immutable, read-only view of the current session ownership lease."""

    owner: str
    epoch: int
    active_run_id: str | None


class ResumeDisposition(StrEnum):
    NEXT = "NEXT"
    RETRY_SAFE = "RETRY_SAFE"
    REMAIN_PAUSED = "REMAIN_PAUSED"


class SessionActor:
    """Serializes session commands and drains accepted work before handoff."""

    def __init__(self) -> None:
        self._state = asyncio.Condition()
        self._command_lock = asyncio.Lock()
        self._owner = _Owner.RUNTIME
        self._epoch = 0
        self._active_run_id: str | None = None
        self._accepted_commands = 0
        self._operations: set[asyncio.Task] = set()

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def owner(self) -> str:
        return self._owner.value

    @property
    def snapshot(self) -> ActorSnapshot:
        return ActorSnapshot(
            owner=self._owner.value,
            epoch=self._epoch,
            active_run_id=self._active_run_id,
        )

    @property
    def active_run_id(self) -> str | None:
        return self._active_run_id

    async def begin_run(self, run_id: str) -> None:
        if not run_id or len(run_id) > 96:
            raise ValueError("run id is invalid")
        async with self._state:
            if self._owner is not _Owner.RUNTIME:
                raise ActorPaused("runtime does not own this session")
            if self._active_run_id is not None:
                raise ActorBusy("a run already owns this session")
            self._active_run_id = run_id

    async def finish_run(self, run_id: str) -> None:
        async with self._state:
            if self._active_run_id != run_id:
                raise ActorBusy("run does not own this session")
            # A submitter may be canceled while its shielded actor-owned task is
            # still finishing. Keep the run lease until every accepted effect has
            # drained so callers cannot close or reuse the session early.
            while self._accepted_commands:
                await self._state.wait()
                if self._active_run_id != run_id:
                    raise ActorBusy("run no longer owns this session")
            self._active_run_id = None
            self._state.notify_all()

    async def submit(
        self,
        *,
        expected_epoch: int,
        run_id: str,
        operation: Operation[T],
    ) -> T:
        """Accept a runtime command at the current epoch and execute it in FIFO order."""
        return await self._submit(
            expected_owner=_Owner.RUNTIME,
            expected_epoch=expected_epoch,
            run_id=run_id,
            operation=operation,
        )

    async def submit_reconciliation(
        self,
        *,
        expected_epoch: int,
        run_id: str,
        operation: Operation[T],
    ) -> T:
        """Run one fenced reconciliation operation while RESUMING owns the page."""
        return await self._submit(
            expected_owner=_Owner.RESUMING,
            expected_epoch=expected_epoch,
            run_id=run_id,
            operation=operation,
        )

    async def _submit(
        self,
        *,
        expected_owner: _Owner,
        expected_epoch: int,
        run_id: str,
        operation: Operation[T],
    ) -> T:
        async with self._state:
            if self._owner is not expected_owner:
                raise ActorPaused("session command submission is not allowed for this owner")
            if expected_epoch != self._epoch:
                raise ActorStaleEpoch("session command has a stale ownership epoch")
            if self._active_run_id != run_id:
                raise ActorBusy("run does not own this session")
            self._accepted_commands += 1

        try:
            operation_task = asyncio.create_task(
                self._dispatch(expected_owner, expected_epoch, run_id, operation)
            )
        except BaseException:
            async with self._state:
                self._accepted_commands -= 1
                self._state.notify_all()
            raise
        self._operations.add(operation_task)
        operation_task.add_done_callback(self._operation_done)
        # The actor-owned task retains the dispatch lock and drain count if a
        # submitter is cancelled. Only that task may release them.
        return await asyncio.shield(operation_task)

    async def _dispatch(
        self,
        expected_owner: _Owner,
        expected_epoch: int,
        run_id: str,
        operation: Operation[T],
    ) -> T:
        try:
            async with self._command_lock:
                async with self._state:
                    # A command accepted behind another command is still
                    # NOT_DISPATCHED until it owns the command lock. Ownership
                    # may change while it waits, so recheck immediately before call.
                    if self._owner is not expected_owner:
                        raise ActorPaused("session command was fenced before dispatch")
                    if expected_epoch != self._epoch:
                        raise ActorStaleEpoch("session command was fenced before dispatch")
                    if self._active_run_id != run_id:
                        raise ActorBusy("run no longer owns this session")
                return await operation()
        finally:
            async with self._state:
                self._accepted_commands -= 1
                self._state.notify_all()

    def _operation_done(self, operation_task: asyncio.Task) -> None:
        self._operations.discard(operation_task)
        if not operation_task.cancelled():
            # Retrieve exceptions even when a canceled submitter no longer awaits
            # the actor-owned task. A live waiter still observes the same exception.
            operation_task.exception()

    async def pause_and_drain(
        self,
        *,
        expected_epoch: int,
        timeout_seconds: float | None = None,
    ) -> HandoffGrant:
        """Fence new runtime commands and wait until accepted work has drained."""
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        async with self._state:
            if expected_epoch != self._epoch:
                raise ActorStaleEpoch("pause request has a stale ownership epoch")
            if self._owner not in {_Owner.RUNTIME, _Owner.PAUSING}:
                raise ActorPaused("runtime cannot pause a session owned by another party")
            self._owner = _Owner.PAUSING
            while self._accepted_commands:
                if deadline is None:
                    await self._state.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Keep the actor fenced; a subsequent call can finish the drain.
                    raise TimeoutError("session command drain is still in progress")
                await asyncio.wait_for(self._state.wait(), timeout=remaining)
            return HandoffGrant(
                owner=_Owner.PAUSING.value,
                epoch=self._epoch,
                active_run_id=self._active_run_id,
            )

    async def claim_human(self, *, expected_epoch: int) -> HandoffGrant:
        """Grant a new ownership epoch after the runtime queue has drained."""
        async with self._state:
            if expected_epoch != self._epoch:
                raise ActorStaleEpoch("handoff request has a stale ownership epoch")
            if self._owner not in {_Owner.PAUSING, _Owner.NONE} or self._accepted_commands:
                raise ActorPaused("session has not drained for handoff")
            self._epoch += 1
            self._owner = _Owner.HUMAN
            return HandoffGrant(
                owner=_Owner.HUMAN.value,
                epoch=self._epoch,
                active_run_id=self._active_run_id,
            )

    async def wait_for_human(self, *, expected_epoch: int) -> HandoffGrant:
        """Release runtime ownership into a fresh, operator-claimable NONE epoch."""
        async with self._state:
            if self._owner is not _Owner.PAUSING:
                raise ActorPaused("session is not drained for human handoff")
            if expected_epoch != self._epoch:
                raise ActorStaleEpoch("handoff request has a stale ownership epoch")
            if self._accepted_commands:
                raise ActorBusy("session commands are still draining")
            self._epoch += 1
            self._owner = _Owner.NONE
            self._state.notify_all()
            return HandoffGrant(
                owner=_Owner.NONE.value,
                epoch=self._epoch,
                active_run_id=self._active_run_id,
            )

    async def begin_resume(self, *, expected_epoch: int) -> HandoffGrant:
        """Enter a fenced reconciliation phase; runtime actions remain unavailable."""
        async with self._state:
            if self._owner is not _Owner.HUMAN:
                raise ActorPaused("human does not own this session")
            if expected_epoch != self._epoch:
                raise ActorStaleEpoch("resume request has a stale ownership epoch")
            self._epoch += 1
            self._owner = _Owner.RESUMING
            return HandoffGrant(
                owner=_Owner.RESUMING.value,
                epoch=self._epoch,
                active_run_id=self._active_run_id,
            )

    async def wait_for_reconciliation(self, *, expected_epoch: int) -> None:
        """Wait for actor-owned reconciliation effects at one RESUMING epoch."""
        async with self._state:
            if self._owner is not _Owner.RESUMING:
                raise ActorPaused("resume reconciliation is not active")
            if expected_epoch != self._epoch:
                raise ActorStaleEpoch("reconciliation drain has a stale epoch")
            while self._accepted_commands:
                await self._state.wait()
                if self._owner is not _Owner.RESUMING:
                    raise ActorPaused("resume reconciliation changed ownership")
                if expected_epoch != self._epoch:
                    raise ActorStaleEpoch("reconciliation drain has a stale epoch")

    async def complete_resume(
        self,
        *,
        expected_epoch: int,
        disposition: ResumeDisposition,
    ) -> HandoffGrant:
        """Apply an external reconciliation result and establish a fresh owner epoch.

        The caller must derive disposition only after identity and postcondition
        reconciliation. This actor enforces the transition and epoch fence; it does
        not itself inspect page state or decide whether a retry is safe.
        """
        if not isinstance(disposition, ResumeDisposition):
            raise ValueError("resume disposition must be explicitly reconciled")
        async with self._state:
            if self._owner is not _Owner.RESUMING:
                raise ActorPaused("resume reconciliation is not active")
            if expected_epoch != self._epoch:
                raise ActorStaleEpoch("resume result has a stale ownership epoch")
            if self._accepted_commands:
                raise ActorBusy("reconciliation commands are still in flight")
            self._epoch += 1
            self._owner = (
                _Owner.HUMAN
                if disposition is ResumeDisposition.REMAIN_PAUSED
                else _Owner.RUNTIME
            )
            self._state.notify_all()
            return HandoffGrant(
                owner=self._owner.value,
                epoch=self._epoch,
                active_run_id=self._active_run_id,
            )
