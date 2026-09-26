#!/usr/bin/env python3
"""Replay the committed capability into an unexpected dialog and hand it to a human.

The run is a normal no-model replay of ``artifacts/get_savings_balance-1.0.0.json``
with the production handoff coordinator attached. Before the replay starts, an
unrecognised modal dialog is injected into the logged-in page (a stand-in for an
unexpected interstitial). Replay classifies it as ``UNKNOWN_BLOCKER``, pauses,
and raises an intervention. The operator then:

1. claims the intervention at the displayed epoch (automation loses the page),
2. dismisses the dialog in the *same* browser page the replay was using,
3. resumes; the runtime re-observes the page, re-checks principal, origin, and
   authentication generation, and continues the replay to a verified result.

``--operator scripted`` performs step 2 with Playwright on that same page so the
run is reproducible. ``--operator person --headed`` opens the browser window and
waits for a person to click Dismiss; terminal prompts stand in for the operator
page's Claim/Resume buttons (``cua serve`` exposes the same calls over HTTP).
Committed evidence keeps only safe run events, intervention states, and a
structural record of what the operator did.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cua.application.handoff import ApplicationHandoffCoordinator
from cua.evidence.models import RunMetadata, RunMode, RunState
from cua.evidence.sink import EvidenceSink
from cua.execution.contracts import InvocationStatus
from cua.execution.gateway import ExecutionGateway
from cua.handoff import HandoffService, InterventionView
from cua.models.qualification import ValidationQualification
from cua.policy.engine import PolicyEngine
from cua.profiles.parabank import ROUTES
from cua.registry import BundleRegistry
from cua.registry.runtime_fingerprint import current_runtime_fingerprint
from cua.replay import ReplayRuntime
from cua.sessions import SessionManager
from cua.surface import PlaywrightSurface
from cua.verification import CompletionVerifier
from testbed.evaluator import assert_result_matches_backend
from testbed.parabank import DEFAULT_ORIGIN, UPSTREAM_COMMIT, check_health
from testbed.seed import seed

from scripts.export_safe_evidence import export_records
from scripts.native_lifecycle_common import (
    ARTIFACT_RELATIVE_PATH,
    contains_private_value,
    install_credentials,
    private_values,
    restore_environment,
    source_fingerprint,
    temporary_credentials,
)
from scripts.review_native_bundle import _account_ids, _load_bundle
from tests.test_discovery_native import (
    _backend_account,
    _execution_context,
    _principal_specs,
    _record_invocation_terminal,
    _result_for_oracle,
    _run_native_validation,
)

_OPERATOR = "local_operator"
_DIALOG_ID = "cua-demo-dialog"
_INJECT_DIALOG = """(dialogId) => {
  const dialog = document.createElement('div');
  dialog.id = dialogId;
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.style.cssText = 'position:fixed;top:30%;left:30%;width:40%;padding:24px;' +
    'background:#fff;border:3px solid #b00;z-index:99999;font:16px sans-serif';
  dialog.innerHTML = '<p><b>Unexpected notice</b></p>' +
    '<p>Automation cannot classify this dialog. A human must dismiss it.</p>' +
    '<button type="button" id="' + dialogId + '-dismiss">Dismiss</button>';
  dialog.querySelector('button').addEventListener('click', () => dialog.remove());
  document.body.appendChild(dialog);
  // Structural click log for the handoff record: element tag and role only.
  window.__cuaOperatorClicks = [];
  document.addEventListener('click', (event) => {
    const el = event.target instanceof Element ? event.target : null;
    window.__cuaOperatorClicks.push({
      tag: el ? el.tagName.toLowerCase() : null,
      role: el ? (el.getAttribute('role') || null) : null,
      inside_injected_dialog: Boolean(el && el.closest('#' + dialogId)),
    });
  }, true);
}"""


def _safe_route(url: str) -> str:
    path = urlsplit(url).path
    for name, route in ROUTES.items():
        if path.endswith(route):
            return name
    return "other"


class _Timeline:
    """Relative-time log of intervention states and operator actions."""

    def __init__(self) -> None:
        self._start = time.monotonic()
        self.entries: list[dict] = []

    def add(self, kind: str, **fields) -> None:
        self.entries.append({"t_ms": int((time.monotonic() - self._start) * 1000), "kind": kind, **fields})

    def intervention(self, view: InterventionView) -> None:
        self.add(
            "intervention_state",
            state=view.state.value,
            owner=view.owner,
            epoch=view.epoch,
            step_id=view.step_id,
            reason=view.reason.value,
        )


async def _dialog_present(page) -> bool:
    return await page.evaluate("(id) => Boolean(document.getElementById(id))", _DIALOG_ID)


async def _handoff_replay(*, registry, reference, account_id, evidence_root, operator, headed):
    manager = SessionManager(_principal_specs(), origin=DEFAULT_ORIGIN, headless=not headed)
    surface = PlaywrightSurface(manager, sample_interval_seconds=0.05)
    evidence = EvidenceSink(evidence_root)
    timeline = _Timeline()
    coordinator = ApplicationHandoffCoordinator(
        HandoffService(authorize_operator=lambda operator_ref: operator_ref == _OPERATOR),
        state_callback=timeline.intervention,
    )
    runtime = ReplayRuntime(
        registry,
        manager,
        surface,
        ExecutionGateway(manager, surface, PolicyEngine(), evidence),
        CompletionVerifier(),
        evidence,
        target_revision=UPSTREAM_COMMIT,
        run_timeout_seconds=600.0 if operator == "person" else 120.0,
        handoff_notifier=coordinator,
    )
    try:
        handle = await manager.prepare("gamma")
        managed = await manager.get(handle.session_id)
        actor = managed.actor
        page = managed.page
        await page.evaluate(_INJECT_DIALOG, _DIALOG_ID)
        timeline.add("blocker_injected", kind_of_blocker="unclassified_modal_dialog", route=_safe_route(page.url))

        run_id = evidence.register_run(
            RunMetadata(
                mode=RunMode.REPLAY,
                capability_name=reference.name,
                capability_version=reference.version,
                bundle_digest=reference.digest,
                profile_id=handle.profile_id,
                target_revision=UPSTREAM_COMMIT,
                browser_version=handle.browser_version,
                model_id=None,
            )
        )
        evidence.transition_run(run_id, RunState.RUNNING)
        await actor.begin_run(run_id)
        timeline.add("replay_started", run_alias=run_id)
        task = asyncio.create_task(
            runtime.run(reference, _execution_context(handle, actor, run_id, account_id))
        )

        interventions: tuple[InterventionView, ...] = ()
        while not interventions:
            if task.done():
                result = task.result()
                failure = result.failure
                raise RuntimeError(
                    "replay ended before requesting a human: "
                    f"{result.status.value} {failure.reason_code if failure else result.code} "
                    f"at {failure.step_id if failure else None}"
                )
            interventions = await coordinator.list(operator_ref=_OPERATOR, session_id=handle.session_id)
            await asyncio.sleep(0.05)
        request = interventions[0]
        request_record = {
            "intervention_id": request.intervention_id,
            "run_alias": request.run_id,
            "capability": f"{reference.name}@{reference.version}",
            "step_id": request.step_id,
            "reason": request.reason.value,
            "state": request.state.value,
            "owner": request.owner,
            "epoch": request.epoch,
            "same_page_as_automation": request.page_id == handle.page_id,
            "route_at_request": _safe_route(page.url),
        }
        timeline.add("intervention_requested", **{k: request_record[k] for k in ("step_id", "reason", "epoch")})

        if operator == "person":
            print("Replay paused: an unexpected dialog is blocking the page.", flush=True)
            await asyncio.to_thread(input, "Press Enter to CLAIM the live session... ")
        claimed = await coordinator.claim(
            request.intervention_id, operator_ref=_OPERATOR, expected_epoch=request.epoch
        )
        timeline.add("operator_claimed", operator_ref=_OPERATOR, epoch=claimed.epoch, owner=claimed.owner)

        before = {"route": _safe_route(page.url), "dialog_present": await _dialog_present(page)}
        if operator == "person":
            print("You own the browser window now. Click Dismiss on the dialog.", flush=True)
            while await _dialog_present(page):
                await asyncio.sleep(0.2)
            await asyncio.to_thread(input, "Dialog gone. Press Enter to RESUME automation... ")
        else:
            await page.click(f"#{_DIALOG_ID}-dismiss")
        clicks = await page.evaluate("() => window.__cuaOperatorClicks || []")
        after = {"route": _safe_route(page.url), "dialog_present": await _dialog_present(page)}
        operator_action = {
            "performed_by": "person" if operator == "person" else "scripted_operator",
            "category": "DISMISS_UNEXPECTED_DIALOG",
            "same_page_as_automation": (await manager.get(handle.session_id)).page is page,
            "before": before,
            "after": after,
            "clicks": clicks,
        }
        timeline.add("operator_action", category=operator_action["category"], click_count=len(clicks))

        resumed = await coordinator.resume(
            request.intervention_id, operator_ref=_OPERATOR, expected_epoch=claimed.epoch
        )
        timeline.add("operator_resumed", epoch=resumed.epoch, state=resumed.state.value)

        result = await task
        if actor.active_run_id == run_id:
            await actor.finish_run(run_id)
        _record_invocation_terminal(evidence, run_id, result)
        timeline.add("replay_finished", status=result.status.value)
        return result, request_record, operator_action, timeline.entries
    finally:
        await manager.close_all()


def _run(args: argparse.Namespace) -> dict:
    if not check_health():
        raise RuntimeError("pinned loopback ParaBank is not healthy; start it first")
    bundle, _ = _load_bundle(args.artifact)
    credentials = temporary_credentials()
    previous = install_credentials(credentials)
    try:
        with tempfile.TemporaryDirectory(prefix="cua-handoff-") as temporary:
            root = Path(temporary)
            manifest = json.loads(seed(manifest_path=root / "seed.json").read_text(encoding="utf-8"))
            account_ids = _account_ids(manifest)
            forbidden = private_values(credentials, manifest)

            registry = BundleRegistry(root / "registry")
            reference = registry.put_draft(bundle)
            validation, browser_version = asyncio.run(
                _run_native_validation(
                    registry=registry,
                    reference=reference,
                    account_id=account_ids["beta"],
                    origin=DEFAULT_ORIGIN,
                    target_revision=UPSTREAM_COMMIT,
                    evidence_root=root / "validation-evidence",
                )
            )
            if validation.status is not InvocationStatus.SUCCESS:
                raise RuntimeError("artifact failed validation before the handoff demo")
            registry.validate(
                reference,
                ValidationQualification(
                    bundle_digest=reference.digest,
                    validation_run_ref=validation.run_id,
                    runtime_fingerprint=current_runtime_fingerprint(),
                    browser_version=browser_version,
                    target_revision=UPSTREAM_COMMIT,
                    replay_passed=True,
                    independent_oracle_passed=True,
                ),
            )
            registry.approve(reference, reviewer_ref="reviewer_handoff_demo", reviewer_type="independent_reviewer")

            result, request, action, timeline = asyncio.run(
                _handoff_replay(
                    registry=registry,
                    reference=reference,
                    account_id=account_ids["gamma"],
                    evidence_root=root / "handoff-evidence",
                    operator=args.operator,
                    headed=args.headed,
                )
            )
            if result.status is not InvocationStatus.SUCCESS:
                failure = result.failure
                raise RuntimeError(
                    f"replay did not complete after resume: {result.status.value} "
                    f"{failure.reason_code if failure else result.code}"
                )
            assert_result_matches_backend(
                _result_for_oracle(result),
                requested_account_id=account_ids["gamma"],
                backend_account=_backend_account(DEFAULT_ORIGIN, account_ids["gamma"]),
            )

            summary = {
                "schema_version": 1,
                "case": "replay_blocked_by_unexpected_dialog_then_human_handoff",
                "status": "PASS",
                "artifact": {"reference": f"capability/{reference.name}@{reference.version}", "digest": f"sha256:{reference.digest}"},
                "intervention_request": request,
                "operator_action": action,
                "timeline": timeline,
                "result": {
                    "status": result.status.value,
                    "outputs_returned": sorted(result.outputs or {}),
                    "independent_oracle_match": True,
                    "provider_calls": 0,
                },
            }
            if args.export:
                out_dir = ROOT / "evidence" / "handoff_run"
                exported = export_records(
                    {"handoff_replay": root / "handoff-evidence"},
                    out_dir,
                    source_kind="native_target",
                    source_fingerprint=source_fingerprint(ROOT),
                    artifact_digest=f"sha256:{reference.digest}",
                    forbidden_values=forbidden,
                )
                summary["event_records"] = {"index_path": f"{exported['path']}/index.json", "index_sha256": exported["sha256"]}
                encoded = json.dumps(summary, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                if contains_private_value(encoded, forbidden):
                    raise RuntimeError("private synthetic value reached the handoff summary")
                (out_dir / "handoff_summary.json").write_bytes(encoded)
                summary["summary_sha256"] = hashlib.sha256(encoded).hexdigest()
            return summary
    finally:
        restore_environment(previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifact", type=Path, default=ROOT / ARTIFACT_RELATIVE_PATH)
    parser.add_argument("--operator", choices=("scripted", "person"), default="scripted")
    parser.add_argument("--headed", action="store_true", help="show the browser window (required for --operator person)")
    parser.add_argument("--export", action="store_true", help="write evidence/handoff_run/ (refuses to overwrite)")
    args = parser.parse_args(argv)
    if args.operator == "person" and not args.headed:
        parser.error("--operator person needs --headed so the person can see the page")
    try:
        summary = _run(args)
    except Exception as error:
        print(f"handoff demo blocked: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
