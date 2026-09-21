"""Application-owned adapter for private operator handoff coordination.

The replay runtime supplies only a value-safe request and a trusted, in-process
reconciliation context.  This adapter owns the HandoffService credential and
never returns it to a browser, API caller, or state callback.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from cua.handoff import (
    HandoffError,
    HandoffResult,
    HandoffService,
    HandoffState,
    InterventionCredential,
    InterventionView,
)
from cua.replay.runtime import HandoffRequest, TrustedHandoffContext


SafeStateCallback = Callable[[InterventionView], Awaitable[None] | None]


class ApplicationHandoffCoordinator:
    """Bridge replay's private handoff seam to the application service boundary.

    ``claim``, ``resume``, and ``abort`` intentionally take no credential from
    the caller.  They accept the caller's safe view epoch unchanged, authenticate
    the operator through the handoff service, and apply the server-held
    credential with that exact epoch.  A concurrent state change therefore fails
    closed with the service's stale-epoch error.
    """

    def __init__(
        self,
        service: HandoffService,
        *,
        state_callback: SafeStateCallback | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(service, HandoffService):
            raise TypeError("handoff service is required")
        if state_callback is not None and not callable(state_callback):
            raise TypeError("state callback must be callable")
        self._service = service
        self._state_callback = state_callback
        self._clock = clock if clock is not None else time.monotonic
        self._credentials: dict[str, InterventionCredential] = {}
        self._credential_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "ApplicationHandoffCoordinator(<server-held-credential>)"

    async def require_human(
        self,
        request: HandoffRequest,
        *,
        trusted: TrustedHandoffContext,
    ) -> HandoffResult | None:
        """Create and await an intervention using the runtime's trusted context."""
        if not isinstance(request, HandoffRequest):
            raise HandoffError("INTERVENTION_INPUT_INVALID")
        if not isinstance(trusted, TrustedHandoffContext):
            raise HandoffError("INTERVENTION_INPUT_INVALID")
        remaining = trusted.deadline_monotonic - self._clock()
        if remaining <= 0:
            return None
        intervention_id: str | None = None

        async def capture(credential: InterventionCredential) -> None:
            nonlocal intervention_id
            intervention_id = credential.intervention_id
            async with self._credential_lock:
                self._credentials[credential.intervention_id] = credential

        def notify(view: InterventionView):
            callback = self._state_callback
            if callback is None or self._clock() >= trusted.deadline_monotonic:
                return None
            result = callback(view)
            # HandoffService owns the bounded await when the callback is
            # awaitable.  Keeping this wrapper synchronous avoids creating a
            # coroutine after the absolute deadline has already elapsed.
            return result

        try:
            return await self._service.request(
                actor=trusted.actor,
                session_id=request.session_id,
                run_id=request.run_id,
                step_id=request.step_id,
                reason=request.reason_code,
                page_id=trusted.page_id,
                reconciler=trusted.reconciler,
                token_delivery=capture,
                timeout_seconds=remaining,
                state_callback=notify,
            )
        finally:
            if intervention_id is not None:
                await self._drop_credential(intervention_id)

    async def list(
        self,
        *,
        operator_ref: str,
        session_id: str | None = None,
    ) -> tuple[InterventionView, ...]:
        """Return authenticated, safe intervention views."""
        return await self._service.list(operator_ref=operator_ref, session_id=session_id)

    async def get(self, intervention_id: str, *, operator_ref: str) -> InterventionView:
        """Return one authenticated, safe intervention view."""
        return await self._service.get(intervention_id, operator_ref=operator_ref)

    async def claim(
        self,
        intervention_id: str,
        *,
        operator_ref: str,
        expected_epoch: int,
    ) -> InterventionView:
        """Claim an intervention with its server-held credential and live epoch."""
        await self._service.get(intervention_id, operator_ref=operator_ref)
        credential = await self._credential(intervention_id)
        return await self._service.claim(
            intervention_id,
            operator_ref=operator_ref,
            credential=credential,
            expected_epoch=expected_epoch,
        )

    async def resume(
        self,
        intervention_id: str,
        *,
        operator_ref: str,
        expected_epoch: int,
    ) -> InterventionView:
        """Resume only after the trusted reconciler chooses a typed disposition."""
        await self._service.get(intervention_id, operator_ref=operator_ref)
        credential = await self._credential(intervention_id)
        resumed = await self._service.resume(
            intervention_id,
            operator_ref=operator_ref,
            credential=credential,
            expected_epoch=expected_epoch,
        )
        if resumed.state is HandoffState.RUNNING:
            await self._drop_credential(intervention_id)
        return resumed

    async def abort(
        self,
        intervention_id: str,
        *,
        operator_ref: str,
        expected_epoch: int,
    ) -> InterventionView:
        """Abort with the server-held credential and current ownership epoch."""
        await self._service.get(intervention_id, operator_ref=operator_ref)
        credential = await self._credential(intervention_id)
        aborted = await self._service.abort(
            intervention_id,
            operator_ref=operator_ref,
            credential=credential,
            expected_epoch=expected_epoch,
        )
        if aborted.state in {HandoffState.ABORTED, HandoffState.TIMED_OUT}:
            await self._drop_credential(intervention_id)
        return aborted

    async def _credential(self, intervention_id: str) -> InterventionCredential:
        async with self._credential_lock:
            credential = self._credentials.get(intervention_id)
        if credential is None:
            raise HandoffError("INTERVENTION_TOKEN_FORBIDDEN")
        return credential

    async def _drop_credential(self, intervention_id: str) -> None:
        async with self._credential_lock:
            self._credentials.pop(intervention_id, None)
