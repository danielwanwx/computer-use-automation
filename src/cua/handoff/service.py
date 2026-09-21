"""Bounded, in-memory human handoff coordination.

This service only coordinates ownership and trusted callbacks. It does not call
an LLM, inspect page contents, or deliver notifications itself.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import secrets
from dataclasses import dataclass
from typing import Iterable

from cua.evidence.models import SafeReasonCode
from cua.sessions.actor import (
    ActorBusy,
    ActorPaused,
    ActorStaleEpoch,
    ResumeDisposition,
    SessionActor,
)
from cua.handoff.contracts import (
    HandoffError,
    HandoffResult,
    HandoffState,
    InterventionCredential,
    InterventionView,
    OperatorAuthorizer,
    ReconciliationContext,
    ReconciliationDisposition,
    ReconciliationResult,
    StateCallback,
    TokenDelivery,
    TrustedReconciler,
)


_OPERATOR = re.compile(r"^[A-Za-z0-9._:-]{1,96}$", re.ASCII)
_INTERVENTION_ID = re.compile(r"^iv_[a-f0-9]{24}$", re.ASCII)
_SAFE_REASONS = frozenset(
    {
        SafeReasonCode.PRECONDITION_UNKNOWN,
        SafeReasonCode.POSTCONDITION_UNKNOWN,
        SafeReasonCode.SESSION_EXPIRED,
        SafeReasonCode.SESSION_LOST,
        SafeReasonCode.SUBJECT_MISMATCH,
        SafeReasonCode.UNKNOWN_BLOCKER,
        SafeReasonCode.APP_ERROR,
        SafeReasonCode.ACCESS_DENIED,
        SafeReasonCode.AMBIGUOUS_STATE,
    }
)


@dataclass(slots=True)
class _Intervention:
    intervention_id: str
    session_id: str
    run_id: str
    step_id: str
    reason: SafeReasonCode
    page_id: str
    actor: SessionActor
    reconciler: TrustedReconciler | None
    token_delivery: TokenDelivery | None
    state_callback: StateCallback | None
    credential: InterventionCredential | None
    future: asyncio.Future[HandoffResult]
    state: HandoffState
    owner: str
    epoch: int
    operator_ref: str | None = None
    callback_running: bool = False
    deadline: float | None = None
    callback_task: asyncio.Task[object] | None = None
    lock: asyncio.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.lock = asyncio.Lock()

    def view(self) -> InterventionView:
        return InterventionView(
            intervention_id=self.intervention_id,
            session_id=self.session_id,
            run_id=self.run_id,
            step_id=self.step_id,
            reason=self.reason,
            state=self.state,
            owner=self.owner,
            epoch=self.epoch,
            page_id=self.page_id,
        )


class HandoffService:
    """Coordinates one bounded operator intervention per active session run."""

    def __init__(self, *, authorize_operator: OperatorAuthorizer) -> None:
        if not callable(authorize_operator):
            raise TypeError("operator authorization callback is required")
        self._authorize_operator = authorize_operator
        self._records: dict[str, _Intervention] = {}
        self._by_session: dict[str, str] = {}
        self._records_lock = asyncio.Lock()

    async def request(
        self,
        *,
        actor: SessionActor,
        session_id: str,
        run_id: str,
        step_id: str,
        reason: SafeReasonCode,
        page_id: str,
        reconciler: TrustedReconciler,
        token_delivery: TokenDelivery,
        timeout_seconds: float,
        state_callback: StateCallback | None = None,
    ) -> HandoffResult:
        """Fence a run, deliver a protected token, then wait for resolution."""
        self._validate_request(
            actor,
            session_id,
            run_id,
            step_id,
            reason,
            page_id,
            reconciler,
            token_delivery,
            timeout_seconds,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout_seconds)
        async with self._records_lock:
            if session_id in self._by_session:
                raise HandoffError("INTERVENTION_ALREADY_ACTIVE")
            snapshot = actor.snapshot
            if snapshot.owner != "RUNTIME" or snapshot.active_run_id != run_id:
                raise HandoffError("INTERVENTION_SESSION_BUSY")
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("handoff deadline expired")
                pausing = await actor.pause_and_drain(
                    expected_epoch=snapshot.epoch,
                    timeout_seconds=remaining,
                )
                waiting = await actor.wait_for_human(expected_epoch=pausing.epoch)
            except TimeoutError:
                raise HandoffError("INTERVENTION_TIMEOUT") from None
            except (ActorBusy, ActorPaused, ActorStaleEpoch):
                raise HandoffError("INTERVENTION_SESSION_BUSY") from None
            intervention_id = "iv_" + secrets.token_hex(12)
            credential = InterventionCredential(
                intervention_id=intervention_id,
                session_id=session_id,
                epoch=waiting.epoch,
                _token=secrets.token_urlsafe(32),
            )
            future: asyncio.Future[HandoffResult] = asyncio.get_running_loop().create_future()
            record = _Intervention(
                intervention_id=intervention_id,
                session_id=session_id,
                run_id=run_id,
                step_id=step_id,
                reason=reason,
                page_id=page_id,
                actor=actor,
                reconciler=reconciler,
                token_delivery=token_delivery,
                state_callback=state_callback,
                credential=credential,
                future=future,
                state=HandoffState.WAITING_FOR_HUMAN,
                owner="NONE",
                epoch=waiting.epoch,
                deadline=deadline,
            )
            self._records[intervention_id] = record
            self._by_session[session_id] = intervention_id

        try:
            await self._notify(record)
            remaining = self._remaining(record)
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(self._deliver(record), timeout=remaining)
        except asyncio.TimeoutError:
            await self._abort_internal(record, timed_out=True)
        except asyncio.CancelledError:
            await self._abort_internal(record, timed_out=False)
            raise
        except Exception:
            await self._abort_internal(record, timed_out=False)

        if record.future.done():
            return await asyncio.shield(record.future)
        try:
            remaining = self._remaining(record)
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(asyncio.shield(record.future), timeout=remaining)
        except asyncio.TimeoutError:
            await self._abort_internal(record, timed_out=True)
            return await asyncio.shield(record.future)
        except asyncio.CancelledError:
            await self._abort_internal(record, timed_out=False)
            raise

    async def get(self, intervention_id: str, *, operator_ref: str) -> InterventionView:
        await self._authorize(operator_ref)
        record = await self._get_record(intervention_id)
        async with record.lock:
            return record.view()

    async def list(
        self,
        *,
        operator_ref: str,
        session_id: str | None = None,
    ) -> tuple[InterventionView, ...]:
        await self._authorize(operator_ref)
        async with self._records_lock:
            records = tuple(self._records.values())
        views: list[InterventionView] = []
        for record in records:
            if session_id is not None and record.session_id != session_id:
                continue
            async with record.lock:
                views.append(record.view())
        return tuple(sorted(views, key=lambda item: item.intervention_id))

    async def claim(
        self,
        intervention_id: str,
        *,
        operator_ref: str,
        credential: InterventionCredential,
        expected_epoch: int,
    ) -> InterventionView:
        record = await self._get_record(intervention_id)
        await self._authorize(operator_ref)
        async with record.lock:
            self._check_credential(record, credential, expected_epoch)
            if record.state is not HandoffState.WAITING_FOR_HUMAN:
                raise HandoffError("INTERVENTION_ALREADY_CLAIMED")
            try:
                grant = await record.actor.claim_human(expected_epoch=record.epoch)
            except ActorStaleEpoch:
                raise HandoffError("INTERVENTION_STALE_EPOCH") from None
            except (ActorPaused, ActorBusy):
                raise HandoffError("INTERVENTION_NOT_CLAIMABLE") from None
            record.operator_ref = operator_ref
            record.epoch = grant.epoch
            record.owner = grant.owner
            record.state = HandoffState.HUMAN_CLAIMED
            view = record.view()
        await self._notify(record)
        return view

    async def resume(
        self,
        intervention_id: str,
        *,
        operator_ref: str,
        credential: InterventionCredential,
        expected_epoch: int,
    ) -> InterventionView:
        record = await self._get_record(intervention_id)
        await self._authorize(operator_ref)
        async with record.lock:
            self._check_credential(
                record,
                credential,
                expected_epoch,
                operator_ref=operator_ref,
                require_bound_operator=True,
            )
            if record.state is not HandoffState.HUMAN_CLAIMED:
                if record.state is HandoffState.RESUMING:
                    raise HandoffError("INTERVENTION_BUSY")
                raise HandoffError("INTERVENTION_NOT_CLAIMED")
            try:
                grant = await record.actor.begin_resume(expected_epoch=record.epoch)
            except ActorStaleEpoch:
                raise HandoffError("INTERVENTION_STALE_EPOCH") from None
            except ActorPaused:
                raise HandoffError("INTERVENTION_NOT_CLAIMED") from None
            record.epoch = grant.epoch
            record.owner = grant.owner
            record.state = HandoffState.RESUMING
            record.callback_running = True
            callback = record.reconciler
            if callback is None:
                record.callback_running = False
                record.state = HandoffState.HUMAN_CLAIMED
                raise HandoffError("INTERVENTION_RECONCILIATION_FAILED")
            context = ReconciliationContext(
                intervention_id=record.intervention_id,
                session_id=record.session_id,
                run_id=record.run_id,
                epoch=record.epoch,
                actor=record.actor,
            )
            callback_task = asyncio.create_task(self._invoke_reconciler(callback, context))
            record.callback_task = callback_task

        # Notification and trusted reconciliation run outside the record lock.
        await self._notify(record)
        async with record.lock:
            callback_task = record.callback_task
        if callback_task is None:
            await self._restore_human(record)
            raise HandoffError("INTERVENTION_RECONCILIATION_FAILED")

        try:
            result = await self._await_callback(record, callback_task)
            if not isinstance(result, ReconciliationResult):
                raise HandoffError("INTERVENTION_RECONCILIATION_FAILED")
            # A trusted reconciler owns the individual actor submissions.  The
            # service only waits for those accepted effects before fencing the
            # actor into its next owner.
            await self._wait_for_actor_effects(record)
            if result.disposition is ReconciliationDisposition.REMAIN_PAUSED:
                disposition = ResumeDisposition.REMAIN_PAUSED
            elif result.disposition is ReconciliationDisposition.NEXT:
                disposition = ResumeDisposition.NEXT
            elif result.disposition is ReconciliationDisposition.RETRY_SAFE:
                disposition = ResumeDisposition.RETRY_SAFE
            else:  # defensive, typed enum validation normally prevents this
                raise HandoffError("INTERVENTION_RECONCILIATION_FAILED")
            grant = await record.actor.complete_resume(
                expected_epoch=record.epoch,
                disposition=disposition,
            )
        except asyncio.CancelledError:
            await self._cancel_callback(callback_task)
            await self._restore_human(record)
            raise
        except asyncio.TimeoutError:
            await self._cancel_callback(callback_task)
            await self._restore_human(record)
            raise HandoffError("INTERVENTION_RECONCILIATION_TIMEOUT") from None
        except HandoffError:
            await self._cancel_callback(callback_task)
            await self._restore_human(record)
            raise
        except Exception:
            await self._cancel_callback(callback_task)
            await self._restore_human(record)
            raise HandoffError("INTERVENTION_RECONCILIATION_FAILED") from None
        finally:
            async with record.lock:
                record.callback_running = False
                record.callback_task = None

        async with record.lock:
            record.epoch = grant.epoch
            record.owner = grant.owner
            if result.disposition is ReconciliationDisposition.REMAIN_PAUSED:
                record.state = HandoffState.HUMAN_CLAIMED
                view = record.view()
            else:
                record.state = HandoffState.RUNNING
                view = record.view()
                self._resolve(
                    record,
                    HandoffResult(
                        intervention_id=record.intervention_id,
                        session_id=record.session_id,
                        run_id=record.run_id,
                        state=record.state,
                        epoch=record.epoch,
                        disposition=result.disposition,
                        reason=result.reason,
                        context=result.context,
                    ),
                )
        await self._notify(record)
        if view.state is HandoffState.RUNNING:
            await self._cleanup_terminal(record)
        return view

    async def abort(
        self,
        intervention_id: str,
        *,
        operator_ref: str,
        credential: InterventionCredential,
        expected_epoch: int,
    ) -> InterventionView:
        record = await self._get_record(intervention_id)
        await self._authorize(operator_ref)
        async with record.lock:
            self._check_credential(
                record,
                credential,
                expected_epoch,
                operator_ref=operator_ref,
                require_bound_operator=record.operator_ref is not None,
            )
            if record.state is HandoffState.RESUMING or record.callback_running:
                raise HandoffError("INTERVENTION_BUSY")
            await self._abort_locked(record, timed_out=False)
            view = record.view()
        await self._notify(record)
        await self._cleanup_terminal(record)
        return view

    async def _deliver(self, record: _Intervention) -> None:
        async with record.lock:
            credential = record.credential
            delivery = record.token_delivery
        if credential is None or delivery is None:
            raise HandoffError("INTERVENTION_TOKEN_FORBIDDEN")
        result = delivery(credential)
        if inspect.isawaitable(result):
            await result

    async def _abort_internal(self, record: _Intervention, *, timed_out: bool) -> None:
        callback_task: asyncio.Task[object] | None = None
        async with record.lock:
            if record.future.done() or record.state in {HandoffState.RUNNING, HandoffState.ABORTED, HandoffState.TIMED_OUT}:
                return
            if record.state is HandoffState.RESUMING or record.callback_running:
                callback_task = record.callback_task
        if callback_task is not None:
            await self._cancel_callback(callback_task)
            await self._restore_human(record)
        async with record.lock:
            if record.future.done() or record.state in {HandoffState.RUNNING, HandoffState.ABORTED, HandoffState.TIMED_OUT}:
                return
            if record.state is HandoffState.RESUMING:
                # A callback that could not be fenced leaves no safe terminal
                # transition.  Keep the record explicitly human-owned only
                # after the actor reconciliation drain completed.
                raise HandoffError("INTERVENTION_ABORT_FAILED")
            await self._abort_locked(record, timed_out=timed_out)
        await self._notify(record)
        await self._cleanup_terminal(record)

    async def _abort_locked(self, record: _Intervention, *, timed_out: bool) -> None:
        if record.state is HandoffState.WAITING_FOR_HUMAN:
            try:
                grant = await record.actor.claim_human(expected_epoch=record.epoch)
            except (ActorBusy, ActorPaused, ActorStaleEpoch):
                raise HandoffError("INTERVENTION_ABORT_FAILED") from None
            record.epoch = grant.epoch
            record.owner = grant.owner
        record.state = HandoffState.TIMED_OUT if timed_out else HandoffState.ABORTED
        self._resolve(
            record,
            HandoffResult(
                intervention_id=record.intervention_id,
                session_id=record.session_id,
                run_id=record.run_id,
                state=record.state,
                epoch=record.epoch,
                reason=SafeReasonCode.UNKNOWN_BLOCKER if timed_out else SafeReasonCode.APP_ERROR,
            ),
        )

    async def _cleanup_terminal(self, record: _Intervention) -> None:
        """Drop active-session and protected callback references after resolution."""
        if record.state not in {
            HandoffState.RUNNING,
            HandoffState.ABORTED,
            HandoffState.TIMED_OUT,
        }:
            return
        async with self._records_lock:
            if self._by_session.get(record.session_id) == record.intervention_id:
                self._by_session.pop(record.session_id, None)
        record.credential = None
        record.reconciler = None
        record.token_delivery = None
        record.state_callback = None

    async def _restore_human(self, record: _Intervention) -> None:
        """Return a RESUMING actor to a claimable human state after a failure."""
        async with record.lock:
            if record.state is HandoffState.HUMAN_CLAIMED:
                return
            if record.state is not HandoffState.RESUMING:
                return
            expected_epoch = record.epoch
        try:
            # The public intervention deadline may have elapsed already.  This
            # is cleanup of an accepted actor effect, so it gets a small,
            # independent drain budget rather than reusing that deadline.
            await asyncio.wait_for(
                record.actor.wait_for_reconciliation(expected_epoch=expected_epoch),
                timeout=0.5,
            )
            grant = await record.actor.complete_resume(
                expected_epoch=expected_epoch,
                disposition=ResumeDisposition.REMAIN_PAUSED,
            )
        except asyncio.TimeoutError:
            # An already accepted browser effect may outlive the public
            # intervention deadline.  Do not leak a cleanup timeout through a
            # caller that is already handling the original reconciliation
            # failure; the actor remains fenced until its own effect drains.
            return
        except (ActorBusy, ActorPaused, ActorStaleEpoch):
            # Another cancellation path may have completed the same actor
            # transition concurrently.  Reconcile the in-memory record with
            # the actor's already-authoritative human lease rather than
            # reintroducing RESUMING.
            snapshot = record.actor.snapshot
            if snapshot.owner == "HUMAN" and snapshot.epoch >= expected_epoch:
                async with record.lock:
                    record.epoch = snapshot.epoch
                    record.owner = snapshot.owner
                    record.state = HandoffState.HUMAN_CLAIMED
            return
        async with record.lock:
            record.epoch = grant.epoch
            record.owner = grant.owner
            record.state = HandoffState.HUMAN_CLAIMED

    async def _invoke_reconciler(
        self,
        callback: TrustedReconciler,
        context: ReconciliationContext,
    ) -> object:
        result = callback(context)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _await_callback(
        self,
        record: _Intervention,
        callback_task: asyncio.Task[object],
    ) -> object:
        remaining = self._remaining(record)
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(asyncio.shield(callback_task), timeout=remaining)

    async def _wait_for_actor_effects(self, record: _Intervention) -> None:
        remaining = self._remaining(record)
        if remaining <= 0:
            raise asyncio.TimeoutError
        await asyncio.wait_for(
            record.actor.wait_for_reconciliation(expected_epoch=record.epoch),
            timeout=remaining,
        )

    async def _cancel_callback(self, callback_task: asyncio.Task[object] | None) -> None:
        if callback_task is None or callback_task.done():
            return
        callback_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(callback_task), timeout=0.25)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            # The callback is trusted code, but it is still fenced and its
            # cancellation is best-effort.  Actor completion below remains the
            # ownership barrier before the intervention can be terminal.
            return

    @staticmethod
    def _remaining(record: _Intervention) -> float:
        if record.deadline is None:
            return 86_400.0
        return record.deadline - asyncio.get_running_loop().time()

    async def _authorize(self, operator_ref: str) -> None:
        if not isinstance(operator_ref, str) or not _OPERATOR.fullmatch(operator_ref):
            raise HandoffError("INTERVENTION_OPERATOR_FORBIDDEN")
        try:
            allowed = self._authorize_operator(operator_ref)
            if inspect.isawaitable(allowed):
                allowed = await allowed
        except Exception:
            allowed = False
        if allowed is not True:
            raise HandoffError("INTERVENTION_OPERATOR_FORBIDDEN")

    async def _get_record(self, intervention_id: str) -> _Intervention:
        if not isinstance(intervention_id, str) or not _INTERVENTION_ID.fullmatch(intervention_id):
            raise HandoffError("INTERVENTION_NOT_FOUND")
        async with self._records_lock:
            record = self._records.get(intervention_id)
        if record is None:
            raise HandoffError("INTERVENTION_NOT_FOUND")
        return record

    @staticmethod
    def _check_credential(
        record: _Intervention,
        credential: InterventionCredential,
        expected_epoch: int,
        *,
        operator_ref: str | None = None,
        require_bound_operator: bool = False,
    ) -> None:
        if not isinstance(credential, InterventionCredential):
            raise HandoffError("INTERVENTION_TOKEN_FORBIDDEN")
        if credential.intervention_id != record.intervention_id or credential.session_id != record.session_id:
            raise HandoffError("INTERVENTION_TOKEN_FORBIDDEN")
        # The token remains scoped to this intervention while the ownership
        # epoch advances on claim/resume.  The caller supplies the exact epoch
        # for the control operation; the credential's issuance epoch is not a
        # second, stale-epoch fence.
        if expected_epoch != record.epoch:
            raise HandoffError("INTERVENTION_STALE_EPOCH")
        if record.credential is None or not secrets.compare_digest(
            credential._token,
            record.credential._token,
        ):
            raise HandoffError("INTERVENTION_TOKEN_FORBIDDEN")
        if require_bound_operator and record.operator_ref != operator_ref:
            raise HandoffError("INTERVENTION_OPERATOR_FORBIDDEN")

    @staticmethod
    def _resolve(record: _Intervention, result: HandoffResult) -> None:
        if not record.future.done():
            record.future.set_result(result)

    async def _notify(self, record: _Intervention) -> None:
        async with record.lock:
            callback = record.state_callback
            view = record.view()
        if callback is None:
            return
        try:
            result = callback(view)
            if inspect.isawaitable(result):
                remaining = self._remaining(record)
                if remaining <= 0:
                    return
                await asyncio.wait_for(result, timeout=min(remaining, 1.0))
        except Exception:
            # The service's in-memory state is authoritative; notification is
            # advisory and must never make a safe handoff unsafe.
            return

    @staticmethod
    def _validate_request(
        actor: SessionActor,
        session_id: str,
        run_id: str,
        step_id: str,
        reason: SafeReasonCode,
        page_id: str,
        reconciler: TrustedReconciler,
        token_delivery: TokenDelivery,
        timeout_seconds: float,
    ) -> None:
        if not isinstance(actor, SessionActor):
            raise HandoffError("INTERVENTION_INPUT_INVALID")
        if not isinstance(reason, SafeReasonCode) or reason not in _SAFE_REASONS:
            raise HandoffError("INTERVENTION_REASON_INVALID")
        if not isinstance(step_id, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", step_id, re.ASCII) is None:
            raise HandoffError("INTERVENTION_INPUT_INVALID")
        if not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", session_id, re.ASCII) is None:
            raise HandoffError("INTERVENTION_INPUT_INVALID")
        if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", run_id, re.ASCII) is None:
            raise HandoffError("INTERVENTION_INPUT_INVALID")
        if not isinstance(page_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", page_id, re.ASCII) is None:
            raise HandoffError("INTERVENTION_INPUT_INVALID")
        if not callable(reconciler) or not callable(token_delivery):
            raise HandoffError("INTERVENTION_INPUT_INVALID")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 86_400:
            raise HandoffError("INTERVENTION_TIMEOUT_INVALID")


async def _no_reconciler(_context: ReconciliationContext) -> ReconciliationResult:
    raise HandoffError("INTERVENTION_ABORTED")


async def _no_delivery(_credential: InterventionCredential) -> None:
    return None
