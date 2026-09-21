"""Small same-origin FastAPI adapter over :class:`ApplicationService`.

This module owns HTTP parsing, safe error/status mapping, and the static page.
Business behavior remains in the application service and its injected handoff
seam; importing this module never starts a browser or background task.
"""

from __future__ import annotations

import os
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from cua.application.contracts import (
    DiscoveryRequest,
    ReplayRequest,
    RunHandle,
    ServiceError,
)
from cua.application.service import ApplicationService
from cua.models.bundles import BundleReference


class _PrepareSessionRequest:
    """Validated manually to keep malformed body details out of 422 responses."""

    def __init__(self, target_id: str, principal_ref: str) -> None:
        if (
            not isinstance(target_id, str)
            or target_id != "parabank-local"
            or not isinstance(principal_ref, str)
            or not principal_ref
        ):
            raise ValueError
        self.target_id = target_id
        self.principal_ref = principal_ref


class _ValidationRequest:
    def __init__(self, digest: str, request_id: str) -> None:
        if not isinstance(digest, str) or not isinstance(request_id, str):
            raise ValueError
        self.reference_digest = digest
        self.request_id = request_id


class _ApprovalRequest:
    def __init__(self, digest: str, reviewer_ref: str, reviewer_type: str) -> None:
        if not all(isinstance(item, str) for item in (digest, reviewer_ref, reviewer_type)):
            raise ValueError
        self.reference_digest = digest
        self.reviewer_ref = reviewer_ref
        self.reviewer_type = reviewer_type


class _InterventionEpochRequest:
    def __init__(self, expected_epoch: object) -> None:
        if (
            isinstance(expected_epoch, bool)
            or not isinstance(expected_epoch, int)
            or expected_epoch < 0
        ):
            raise ValueError
        self.expected_epoch = expected_epoch


_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>CUA</title></head>
<body><main><h1>Computer-use automation</h1>
<label>Local token <input id="token" type="password" autocomplete="off"></label>
<section id="run"><h2>Run</h2>
<label>Principal <input id="principal" value="synthetic_alpha"></label>
<button id="prepare">Prepare session</button><output id="session"></output><br>
<label>Goal <input id="goal" value="Get the available balance for savings account"></label>
<label>Account <input id="account" inputmode="numeric"></label>
<button id="discover">Discover</button><button id="replay">Replay approved</button>
<pre id="run-status"></pre><button id="read-result">Read result</button><pre id="result"></pre></section>
<section id="capabilities"><h2>Capabilities</h2>
<button id="refresh">Refresh</button>
<label>Revision <select id="capability-choice"><option value="">Refresh capability list</option></select></label>
<label>Name <input id="capability-name" readonly></label>
<label>Version <input id="capability-version" readonly></label>
<label>Digest <input id="capability-digest" readonly></label>
<button id="inspect">Inspect</button><button id="validate">Validate</button><button id="approve">Approve</button>
<output id="capability-ref"></output><pre id="capability-list"></pre></section>
<section id="intervention"><h2>Intervention</h2>
<p id="intervention-state">No active intervention.</p><output id="intervention-epoch"></output>
<button id="intervention-claim" type="button">Claim</button>
<button id="intervention-resume" type="button">Resume</button>
<button id="intervention-abort" type="button">Abort</button>
<pre id="intervention-detail"></pre></section>
<script>
let token = '';
let sessionId = '';
let runId = '';
let capabilities = [];
let intervention = null;
let interventionTimer = null;
const $ = id => document.querySelector(id);
function selectedCapability() {
  const index = Number($('#capability-choice').value);
  return Number.isInteger(index) && capabilities[index] ? capabilities[index] : null;
}
function capabilityPath(action, item) {
  return `/api/capabilities/${encodeURIComponent(item.reference.name)}/${encodeURIComponent(item.reference.version)}/${action}`;
}
function showCapability(item) {
  const fields = [['capability-name', 'name'], ['capability-version', 'version'], ['capability-digest', 'digest']];
  fields.forEach(([id, key]) => { $(id).value = item ? item.reference[key] : ''; });
  if (!item) { $('#capability-ref').textContent = ''; return; }
  $('#capability-ref').textContent = `${item.reference.name}@${item.reference.version} ${item.reference.digest}`;
}
async function api(path, options={}) {
  token = $('#token').value;
  const headers = new Headers(options.headers || {});
  headers.set('Authorization', `Bearer ${token}`);
  if (options.body) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET') headers.set('X-CUA-CSRF', 'same-origin');
  if (sessionId) headers.set('X-CUA-Session-ID', sessionId);
  return fetch(path, {...options, headers, credentials: 'same-origin'});
}
async function refreshCapabilities() {
  const response = await api('/api/capabilities');
  const value = await response.json();
  capabilities = Array.isArray(value) ? value : [];
  const select = $('#capability-choice');
  select.replaceChildren();
  capabilities.forEach((item, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = `${item.reference.name}@${item.reference.version} (${item.lifecycle})`;
    select.append(option);
  });
  showCapability(selectedCapability());
  $('#capability-list').textContent = JSON.stringify(value, null, 2);
}
$('#refresh').onclick = refreshCapabilities;
$('#capability-choice').onchange = () => showCapability(selectedCapability());
$('#inspect').onclick = async () => {
  const item = selectedCapability();
  if (!item) { $('#capability-list').textContent = 'Select a capability first.'; return; }
  const query = encodeURIComponent(item.reference.digest);
  const response = await api(`/api/capabilities/${encodeURIComponent(item.reference.name)}/${encodeURIComponent(item.reference.version)}?digest=${query}`);
  $('#capability-list').textContent = JSON.stringify(await response.json(), null, 2);
};
$('#prepare').onclick = async () => {
  const r = await api('/api/sessions', {method:'POST', body:JSON.stringify({target_id:'parabank-local', principal_ref:$('#principal').value})});
  const value = await r.json(); sessionId = value.session_id || ''; $('#session').textContent = sessionId;
};
async function start(path, body) {
  const r = await api(path, {method:'POST', body:JSON.stringify(body)}); const value = await r.json();
  runId = value.run_id || ''; intervention = null; showIntervention(null); $('#run-status').textContent = JSON.stringify(value, null, 2);
  if (runId) void poll();
}
$('#discover').onclick = () => start('/api/discovery', {session_id:sessionId, goal:$('#goal').value, inputs:{account_id:$('#account').value}, request_id:`ui-${Date.now()}`});
$('#validate').onclick = () => {
  const item = selectedCapability();
  if (!item) { $('#run-status').textContent = 'Select a capability first.'; return; }
  start(capabilityPath('validate', item), {digest:item.reference.digest, request_id:`ui-validation-${Date.now()}`});
};
$('#approve').onclick = async () => {
  const item = selectedCapability();
  if (!item) { $('#capability-list').textContent = 'Select a capability first.'; return; }
  const response = await api(capabilityPath('approve', item), {method:'POST', body:JSON.stringify({digest:item.reference.digest, reviewer_ref:'local_operator', reviewer_type:'operator'})});
  $('#capability-list').textContent = JSON.stringify(await response.json(), null, 2);
};
$('#replay').onclick = () => {
  const item = selectedCapability();
  if (!item) { $('#run-status').textContent = 'Select an approved capability first.'; return; }
  start('/api/invocations', {reference:item.reference, session_id:sessionId, inputs:{account_id:$('#account').value}, request_id:`ui-replay-${Date.now()}`});
};
async function poll() {
  const r = await api(`/api/runs/${runId}`); const value = await r.json();
  $('#run-status').textContent = JSON.stringify(value, null, 2);
  if(value.state === 'WAITING_FOR_HUMAN') void refreshInterventions();
  if(['RUNNING','WAITING_FOR_HUMAN'].includes(value.state)) setTimeout(() => void poll(), 2000);
}
function showIntervention(value) {
  intervention = value;
  const state = $('#intervention-state');
  const epoch = $('#intervention-epoch');
  const detail = $('#intervention-detail');
  const buttons = ['intervention-claim', 'intervention-resume', 'intervention-abort'];
  if (!value) {
    state.textContent = 'No active intervention.';
    epoch.textContent = '';
    detail.textContent = '';
    buttons.forEach(id => { $(id).disabled = true; });
    return;
  }
  state.textContent = `${value.state} · ${value.reason}`;
  epoch.textContent = `Epoch ${value.epoch}`;
  detail.textContent = JSON.stringify(value, null, 2);
  buttons.forEach(id => { $(id).disabled = false; });
}
function scheduleInterventionPoll() {
  if (interventionTimer !== null) return;
  interventionTimer = setTimeout(() => { interventionTimer = null; void refreshInterventions(); }, 2000);
}
async function refreshInterventions() {
  const response = await api('/api/interventions');
  const value = await response.json();
  const current = Array.isArray(value) ? value.find(item => item.run_id === runId) : null;
  showIntervention(current || null);
  if (current && ['WAITING_FOR_HUMAN', 'HUMAN_CLAIMED', 'RESUMING'].includes(current.state)) scheduleInterventionPoll();
}
async function interventionAction(action) {
  if (!intervention) return;
  const response = await api(`/api/interventions/${encodeURIComponent(intervention.intervention_id)}/${action}`, {
    method: 'POST', body: JSON.stringify({expected_epoch: intervention.epoch})
  });
  const value = await response.json();
  if (response.ok) showIntervention(value);
  else $('#intervention-detail').textContent = JSON.stringify(value, null, 2);
  if (response.ok && value.state === 'RUNNING') void poll();
}
$('#intervention-claim').onclick = () => void interventionAction('claim');
$('#intervention-resume').onclick = () => void interventionAction('resume');
$('#intervention-abort').onclick = () => void interventionAction('abort');
$('#read-result').onclick = async () => {
  if (!runId) { $('#result').textContent = 'Start a run first.'; return; }
  const r = await api(`/api/runs/${runId}/result`); $('#result').textContent = JSON.stringify(await r.json(), null, 2);
};
</script></main></body></html>"""


class _NoStoreMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app(
    service: ApplicationService,
    *,
    operator_token: str | None = None,
    operator_origin: str | None = None,
) -> FastAPI:
    """Build routes around one already-composed service instance."""
    configured_token = operator_token
    if configured_token is None:
        config_token = getattr(getattr(service, "_config", None), "operator_token", None)
        configured_token = (
            config_token.get_secret_value() if config_token is not None else os.environ.get("CUA_OPERATOR_TOKEN")
        )
    expected_origin = operator_origin or getattr(
        getattr(service, "_config", None), "operator_origin", "http://127.0.0.1:8765"
    )
    expected_host = urlsplit(expected_origin).netloc
    app = FastAPI(title="Computer-use automation", docs_url=None, redoc_url=None)
    app.add_middleware(_NoStoreMiddleware)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, error: ServiceError):
        return JSONResponse(status_code=error.status, content={"code": error.code})

    @app.exception_handler(RequestValidationError)
    async def request_error_handler(_: Request, __: RequestValidationError):
        return JSONResponse(status_code=422, content={"code": "INPUT_INVALID"})

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, error: HTTPException):
        code = error.detail if isinstance(error.detail, str) else "REQUEST_REJECTED"
        return JSONResponse(status_code=error.status_code, content={"code": code})

    def require_operator(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        origin: Annotated[str | None, Header()] = None,
        host: Annotated[str | None, Header()] = None,
        csrf: Annotated[str | None, Header(alias="X-CUA-CSRF")] = None,
    ) -> None:
        if host != expected_host:
            raise HTTPException(status_code=403, detail="ORIGIN_NOT_ALLOWED")
        mutating = request.method in {"POST", "DELETE", "PATCH", "PUT"}
        if (mutating and not origin) or (origin and origin.rstrip("/") != expected_origin.rstrip("/")):
            raise HTTPException(status_code=403, detail="ORIGIN_NOT_ALLOWED")
        if mutating and csrf != "same-origin":
            raise HTTPException(status_code=403, detail="CSRF_REQUIRED")
        if configured_token is None:
            raise HTTPException(status_code=503, detail="AUTH_NOT_CONFIGURED")
        expected = f"Bearer {configured_token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")

    def require_session_header(
        x_cua_session_id: Annotated[str | None, Header()] = None,
    ) -> str:
        if not x_cua_session_id:
            raise HTTPException(status_code=401, detail="SESSION_AUTH_REQUIRED")
        return x_cua_session_id

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _INDEX_HTML

    @app.get("/api/sessions")
    async def sessions(_: object = Depends(require_operator)):
        return [view.model_dump(mode="json") for view in await service.list_sessions()]

    @app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    async def prepare_session(
        request: Request,
        _: object = Depends(require_operator),
    ):
        payload = await _json_object(request)
        _require_exact_keys(payload, {"target_id", "principal_ref"})
        try:
            prepared = _PrepareSessionRequest(payload["target_id"], payload["principal_ref"])
        except (KeyError, TypeError, ValueError):
            raise ServiceError(422, "INPUT_INVALID") from None
        view = await service.prepare_session(prepared.principal_ref)
        return view.model_dump(mode="json")

    @app.delete("/api/sessions/{session_id}")
    async def close_session(
        session_id: str,
        _: object = Depends(require_operator),
    ):
        await service.close_session(session_id)
        return {"status": "CLOSED"}

    @app.post("/api/discovery", status_code=status.HTTP_202_ACCEPTED)
    async def discovery(
        request: Request,
        _: object = Depends(require_operator),
    ):
        body = await _json_object(request)
        model = _parse_model(DiscoveryRequest, body)
        handle = await service.start_discovery(model)
        return _handle_json(handle)

    @app.post("/api/invocations", status_code=status.HTTP_202_ACCEPTED)
    async def invocation(
        request: Request,
        _: object = Depends(require_operator),
    ):
        body = await _json_object(request)
        model = _parse_model(ReplayRequest, body)
        handle = await service.invoke(model)
        return _handle_json(handle)

    @app.get("/api/runs/{run_id}")
    async def run_status(
        run_id: str,
        _: object = Depends(require_operator),
    ):
        return (await service.get_run(run_id)).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/result")
    async def run_result(
        run_id: str,
        _: object = Depends(require_operator),
        caller_session_id: str = Depends(require_session_header),
    ):
        result = await service.read_result(run_id, caller_session_id)
        return _result_json(result)

    @app.get("/api/interventions")
    async def interventions(_: object = Depends(require_operator)):
        views = await service.list_interventions(operator_ref="local_operator")
        return [_intervention_json(view) for view in views]

    @app.get("/api/interventions/{intervention_id}")
    async def intervention(
        intervention_id: str,
        _: object = Depends(require_operator),
    ):
        view = await service.get_intervention(
            intervention_id,
            operator_ref="local_operator",
        )
        return _intervention_json(view)

    @app.post("/api/interventions/{intervention_id}/claim")
    async def claim_intervention(
        intervention_id: str,
        request: Request,
        _: object = Depends(require_operator),
    ):
        payload = await _json_object(request)
        _require_exact_keys(payload, {"expected_epoch"})
        try:
            data = _InterventionEpochRequest(payload["expected_epoch"])
        except (KeyError, TypeError, ValueError):
            raise ServiceError(422, "INPUT_INVALID") from None
        view = await service.claim_intervention(
            intervention_id,
            expected_epoch=data.expected_epoch,
            operator_ref="local_operator",
        )
        return _intervention_json(view)

    @app.post("/api/interventions/{intervention_id}/resume")
    async def resume_intervention(
        intervention_id: str,
        request: Request,
        _: object = Depends(require_operator),
    ):
        payload = await _json_object(request)
        _require_exact_keys(payload, {"expected_epoch"})
        try:
            data = _InterventionEpochRequest(payload["expected_epoch"])
        except (KeyError, TypeError, ValueError):
            raise ServiceError(422, "INPUT_INVALID") from None
        view = await service.resume_intervention(
            intervention_id,
            expected_epoch=data.expected_epoch,
            operator_ref="local_operator",
        )
        return _intervention_json(view)

    @app.post("/api/interventions/{intervention_id}/abort")
    async def abort_intervention(
        intervention_id: str,
        request: Request,
        _: object = Depends(require_operator),
    ):
        payload = await _json_object(request)
        _require_exact_keys(payload, {"expected_epoch"})
        try:
            data = _InterventionEpochRequest(payload["expected_epoch"])
        except (KeyError, TypeError, ValueError):
            raise ServiceError(422, "INPUT_INVALID") from None
        view = await service.abort_intervention(
            intervention_id,
            expected_epoch=data.expected_epoch,
            operator_ref="local_operator",
        )
        return _intervention_json(view)

    @app.get("/api/capabilities")
    async def capabilities(_: object = Depends(require_operator)):
        return [item.model_dump(mode="json") for item in await service.list_capabilities()]

    @app.get("/api/capabilities/{name}/{version}")
    async def capability_detail(
        name: str,
        version: str,
        digest: str,
        _: object = Depends(require_operator),
    ):
        reference = _reference(name, version, digest)
        return (await service.inspect_capability(reference)).model_dump(mode="json")

    @app.post("/api/capabilities/{name}/{version}/validate", status_code=status.HTTP_202_ACCEPTED)
    async def validate_capability(
        name: str,
        version: str,
        request: Request,
        _: object = Depends(require_operator),
    ):
        payload = await _json_object(request)
        _require_exact_keys(payload, {"digest", "request_id"})
        try:
            data = _ValidationRequest(payload["digest"], payload["request_id"])
            reference = _reference(name, version, data.reference_digest)
        except (KeyError, TypeError, ValueError, ValidationError):
            raise ServiceError(422, "INPUT_INVALID") from None
        return _handle_json(
            await service.validate_capability(reference, request_id=data.request_id)
        )

    @app.post("/api/capabilities/{name}/{version}/approve")
    async def approve_capability(
        name: str,
        version: str,
        request: Request,
        _: object = Depends(require_operator),
    ):
        payload = await _json_object(request)
        _require_exact_keys(payload, {"digest", "reviewer_ref", "reviewer_type"})
        try:
            data = _ApprovalRequest(
                payload["digest"], payload["reviewer_ref"], payload["reviewer_type"]
            )
            reference = _reference(name, version, data.reference_digest)
        except (KeyError, TypeError, ValueError, ValidationError):
            raise ServiceError(422, "INPUT_INVALID") from None
        approval = await service.approve_capability(
            reference,
            expected_digest=data.reference_digest,
            reviewer_ref=data.reviewer_ref,
            reviewer_type=data.reviewer_type,
        )
        return approval.model_dump(mode="json")

    return app


async def _json_object(request: Request) -> dict[str, object]:
    try:
        value = await request.json()
    except Exception:
        raise ServiceError(422, "INPUT_INVALID") from None
    if not isinstance(value, dict):
        raise ServiceError(422, "INPUT_INVALID")
    return value


def _require_exact_keys(value: dict[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise ServiceError(422, "INPUT_INVALID")


def _parse_model(model_type, value: dict[str, object]):
    try:
        return model_type.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        raise ServiceError(422, "INPUT_INVALID") from None


def _reference(name: str, version: str, digest: str) -> BundleReference:
    try:
        return BundleReference(name=name, version=version, digest=digest)
    except (ValidationError, TypeError, ValueError):
        raise ServiceError(422, "INPUT_INVALID") from None


def _handle_json(handle: RunHandle) -> dict[str, object]:
    return handle.model_dump(mode="json")


def _intervention_json(view) -> dict[str, object]:
    """Serialize only the value-safe intervention view; credentials never cross HTTP."""
    return {
        "intervention_id": view.intervention_id,
        "session_id": view.session_id,
        "run_id": view.run_id,
        "step_id": view.step_id,
        "reason": view.reason.value,
        "state": view.state.value,
        "owner": view.owner,
        "epoch": view.epoch,
        "page_id": view.page_id,
    }


def _result_json(result) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": result.run_id,
        "status": result.status.value,
        "code": result.code.value if result.code is not None else None,
        "evidence_refs": [item.model_dump(mode="json") for item in result.evidence_refs],
    }
    if result.outputs is not None:
        payload["outputs"] = {
            key: value.get_secret_value() for key, value in result.outputs.items()
        }
    if result.failure is not None:
        payload["failure"] = {
            "reason_code": result.failure.reason_code.value,
            "step_id": result.failure.step_id,
            "effect_state": result.failure.effect_state.value
            if result.failure.effect_state is not None
            else None,
        }
    return payload
