#!/usr/bin/env python3
"""Prepare a headed, same-session handoff for the real-person V9 check.

This helper creates a real local operator service and one headed ParaBank
session, then parks the same page on a safe non-overview route before creating
an operator intervention. It never performs the human action and never records
V9 as accepted. After the operator clicks ``Accounts Overview`` in the target
window and uses Claim/Resume in the local operation page, the trusted
reconciler records only safe route/state metadata and ends this preparation run
as an intentional non-capability abort.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cua.application.config import ApplicationConfig
from cua.application.contracts import RunMode
from cua.application.service import ApplicationService
from cua.evidence.models import SafeReasonCode
from cua.execution.contracts import EffectState, FailureDetail, InvocationResult, InvocationStatus
from cua.handoff.contracts import (
    HandoffState,
    ReconciliationContext,
    ReconciliationDisposition,
    ReconciliationResult,
)
from cua.replay.runtime import HandoffRequest, TrustedHandoffContext
from cua.sessions import ActorBusy, ActorPaused, ActorStaleEpoch, PrincipalSpec, SessionError
from testbed.parabank import DEFAULT_ORIGIN, UPSTREAM_COMMIT, start
from testbed.seed import seed


_DEFAULT_DATA_ROOT = Path("runtime/v9-handoff")
_DEFAULT_OPERATOR_PORT = 8765
_BLOCKER_PATH = "/activity.htm"
_STEP_ID = "manual_handoff_probe"
_PRINCIPAL = "alpha"


def _principal_specs() -> tuple[PrincipalSpec, ...]:
    return (
        PrincipalSpec(
            _PRINCIPAL,
            "PARABANK_DEMO_ALPHA_USERNAME",
            "PARABANK_DEMO_ALPHA_PASSWORD",
            "Synthetic Alpha",
        ),
    )


def _safe_evidence_path(data_root: Path) -> Path:
    return data_root / "v9-handoff.json"


def _write_safe_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _initial_evidence(config: ApplicationConfig, handle, intervention) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scenario": "V9_HANDOFF_PREPARED",
        "acceptance_status": "NOT_RUN",
        "target": {
            "origin": config.target_origin,
            "upstream_revision": UPSTREAM_COMMIT,
            "loopback": True,
        },
        "browser": {
            "channel": config.browser_channel,
            "headed": config.headless is False,
            "same_session_required": True,
        },
        "operator_page": {
            "origin": config.operator_origin,
            "controls": ["Claim", "Resume", "Abort"],
        },
        "target_window": {
            "initial_path": _BLOCKER_PATH,
            "required_action": "click Accounts Overview in the same headed target window",
        },
        "intervention": {
            "state": intervention.state.value,
            "reason": intervention.reason.value,
            "epoch": intervention.epoch,
        },
        "manual_completion": {
            "route_verified": False,
            "resumed": False,
            "person_performed": None,
        },
        "run": {
            "session_id": handle.session_id,
            "run_id": intervention.run_id,
            "page_id": intervention.page_id,
        },
    }


async def _wait_for_server(origin: str) -> None:
    from urllib.request import urlopen

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            await asyncio.to_thread(
                lambda: urlopen(origin + "/", timeout=1.0).close()
            )
            return
        except Exception:
            await asyncio.sleep(0.1)
    raise RuntimeError("operator service did not start on loopback")


async def _prepare_target_session(service: ApplicationService):
    """Return the private browser handle behind the public session view."""
    session_view = await service.prepare_session(_PRINCIPAL)
    managed = await service._sessions.get(session_view.session_id)
    return managed.handle, managed


async def _prepare(args: argparse.Namespace) -> int:
    # The testbed functions are intentionally called before any runtime session
    # is created, so the exclusive seed lock cannot race the shared session lock.
    start()
    seed()

    data_root = Path(args.data_root).expanduser().resolve()
    config = ApplicationConfig(
        data_root=data_root,
        principal_specs=_principal_specs(),
        target_origin=DEFAULT_ORIGIN,
        target_revision=UPSTREAM_COMMIT,
        browser_channel=args.browser_channel,
        headless=False,
        run_timeout_seconds=float(args.timeout_seconds),
        operator_port=args.operator_port,
        operator_token=None,
        provider_enabled=False,
        provider="disabled",
    )
    service = ApplicationService.from_config(config)

    import uvicorn

    from cua.web import create_app

    operator_token = os.environ.get("CUA_OPERATOR_TOKEN") or secrets.token_urlsafe(32)
    app = create_app(
        service,
        operator_token=operator_token,
        operator_origin=config.operator_origin,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.operator_bind,
            port=config.operator_port,
            log_level="warning",
        )
    )
    server_task = asyncio.create_task(server.serve())
    evidence_path = _safe_evidence_path(data_root)
    try:
        await _wait_for_server(config.operator_origin)
        handle, managed = await _prepare_target_session(service)
        await managed.page.goto(
            config.target_origin + _BLOCKER_PATH,
            wait_until="domcontentloaded",
        )

        async def reconcile(context: ReconciliationContext) -> ReconciliationResult:
            async def observe():
                return await service._replay._surface.observe(context.session_id)

            try:
                _, view = await context.actor.submit_reconciliation(
                    expected_epoch=context.epoch,
                    run_id=context.run_id,
                    operation=observe,
                )
            except ActorStaleEpoch:
                return ReconciliationResult(
                    ReconciliationDisposition.REMAIN_PAUSED,
                    reason=SafeReasonCode.STALE_EPOCH,
                )
            except (ActorPaused, ActorBusy, SessionError):
                return ReconciliationResult(
                    ReconciliationDisposition.REMAIN_PAUSED,
                    reason=SafeReasonCode.SESSION_LOST,
                )
            except Exception:
                return ReconciliationResult(
                    ReconciliationDisposition.REMAIN_PAUSED,
                    reason=SafeReasonCode.UNKNOWN_BLOCKER,
                )
            if (
                view.safe_route == "accounts_overview"
                and view.page_state == "OVERVIEW_READY"
                and view.principal_matches is True
            ):
                return ReconciliationResult(
                    ReconciliationDisposition.NEXT,
                    reason=SafeReasonCode.AUTHORIZED,
                )
            return ReconciliationResult(
                ReconciliationDisposition.REMAIN_PAUSED,
                reason=SafeReasonCode.UNKNOWN_BLOCKER,
            )

        async def run(context):
            try:
                trusted = TrustedHandoffContext(
                    actor=(await service._sessions.get_state(context.session_id)).actor,
                    reconciler=reconcile,
                    page_id=handle.page_id,
                    deadline_monotonic=context.deadline_monotonic or time.monotonic(),
                )
                result = await service._handoff_coordinator.require_human(
                    HandoffRequest(
                        run_id=context.run_alias,
                        session_id=context.session_id,
                        step_id=_STEP_ID,
                        reason_code=SafeReasonCode.UNKNOWN_BLOCKER,
                        ownership_epoch=context.expected_epoch,
                    ),
                    trusted=trusted,
                )
            except Exception as error:
                print(
                    f"V9 preparation runner failed: {type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            if result is not None and result.state is HandoffState.RUNNING:
                _write_safe_evidence(
                    evidence_path,
                    {
                        "schema_version": 1,
                        "scenario": "V9_HANDOFF_PREPARED",
                        "acceptance_status": "NOT_RUN",
                        "target": {"origin": config.target_origin, "upstream_revision": UPSTREAM_COMMIT, "loopback": True},
                        "browser": {"headed": True, "same_session_required": True},
                        "operator_page": {"origin": config.operator_origin, "controls": ["Claim", "Resume", "Abort"]},
                        "intervention": {"state": result.state.value, "disposition": result.disposition.value if result.disposition else None, "reason": result.reason.value if result.reason else None},
                        "manual_completion": {"route_verified": True, "resumed": True, "person_performed": None},
                        "run": {"run_id": result.run_id},
                    },
                )
            else:
                _write_safe_evidence(
                    evidence_path,
                    {
                        "schema_version": 1,
                        "scenario": "V9_HANDOFF_PREPARED",
                        "acceptance_status": "NOT_RUN",
                        "intervention": {"state": result.state.value if result else "UNKNOWN"},
                        "manual_completion": {"route_verified": False, "resumed": False, "person_performed": None},
                    },
                )
            return InvocationResult(
                run_id=context.run_alias,
                status=InvocationStatus.ABORTED,
                failure=FailureDetail(
                    reason_code=SafeReasonCode.UNKNOWN_BLOCKER,
                    step_id=_STEP_ID,
                    effect_state=EffectState.NOT_DISPATCHED,
                ),
            )

        state = await service._sessions.get_state(handle.session_id)
        run_handle = await service._start_run(
            session_id=handle.session_id,
            expected_browser_version=state.browser_version,
            mode=RunMode.REPLAY,
            request_id="v9-preparation-" + secrets.token_hex(8),
            payload={"scenario": "V9_HANDOFF_PREPARED"},
            runner=run,
            reference=None,
            model_id=None,
            input_bindings={},
        )
        interventions = ()
        while not interventions:
            record = service._runs.get(run_handle.run_id)
            if record is not None and record.task is not None and record.task.done():
                await record.task
                raise RuntimeError(
                    "V9 preparation run terminated before WAITING_FOR_HUMAN "
                    f"(state={record.state.value}, outcome={record.outcome_code.value if record.outcome_code else 'UNKNOWN'})"
                )
            interventions = await service.list_interventions(
                operator_ref="local_operator",
                session_id=handle.session_id,
            )
            if not interventions:
                await asyncio.sleep(0.05)
        _write_safe_evidence(evidence_path, _initial_evidence(config, handle, interventions[0]))

        print("V9 handoff preparation is waiting for a real operator.", flush=True)
        print(f"Operator page: {config.operator_origin}/", flush=True)
        print(f"Operator token: {operator_token}", flush=True)
        print(f"Target window: headed {args.browser_channel} at {config.target_origin}{_BLOCKER_PATH}", flush=True)
        print("Target action: in that same window, click Accounts Overview once.", flush=True)
        print("Then use Claim and Resume on the operator page; do not use Abort if testing resume.", flush=True)
        print(f"Safe evidence path: {evidence_path}", flush=True)
        print(f"Run: {run_handle.run_id}; intervention: {interventions[0].intervention_id}", flush=True)
        print("V9 remains NOT_RUN until a human records the acceptance evidence separately.", flush=True)

        record = service._runs[run_handle.run_id]
        await record.task
        print("Handoff protocol completed; safe evidence was updated. Press Ctrl-C to close the headed session.", flush=True)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        await stop_event.wait()
        return 0
    finally:
        server.should_exit = True
        await server_task
        await service.shutdown()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default=str(_DEFAULT_DATA_ROOT))
    parser.add_argument("--operator-port", type=int, default=_DEFAULT_OPERATOR_PORT)
    parser.add_argument("--browser-channel", choices=("chrome", "msedge"), default="chrome")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout_seconds <= 0 or args.timeout_seconds > 1800:
        print("--timeout-seconds must be between 0 and 1800", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_prepare(args))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"V9 preparation failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
