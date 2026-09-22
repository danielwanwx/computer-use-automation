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
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>Operator — Computer use</title>
<style>
:root {
  color-scheme: light;
  --canvas: #f5f5f7;
  --surface: #fff;
  --ink: #1d1d1f;
  --muted: #6e6e73;
  --line: #d2d2d7;
  --blue: #0071e3;
  --blue-dark: #0066cc;
  --green: #1e854b;
  --red: #d70015;
  --dock: #1d1d1f;
  --shadow: 0 18px 48px rgb(0 0 0 / 7%), 0 2px 8px rgb(0 0 0 / 4%);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", sans-serif;
}
* { box-sizing: border-box; }
html { background: var(--canvas); scroll-behavior: smooth; }
body {
  margin: 0;
  min-width: 320px;
  background: var(--canvas);
  color: var(--ink);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
button, input, select { font: inherit; }
button { cursor: pointer; }
button:disabled { cursor: not-allowed; }
button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible {
  outline: 3px solid rgb(0 113 227 / 30%);
  outline-offset: 3px;
}
.skip-link {
  position: absolute;
  left: 16px;
  top: -48px;
  z-index: 10;
  padding: 10px 14px;
  border-radius: 999px;
  background: var(--ink);
  color: white;
  text-decoration: none;
  transition: top 140ms ease-out;
}
.skip-link:focus { top: 16px; }
.app-shell { width: min(1120px, calc(100% - 48px)); margin: 0 auto; padding: 38px 0 72px; }
.site-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 32px; margin-bottom: 54px; }
.eyebrow { margin: 0 0 8px; color: var(--muted); font-size: 13px; font-weight: 600; letter-spacing: .02em; }
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 0; font-size: clamp(48px, 8vw, 86px); font-weight: 700; letter-spacing: -.055em; line-height: .95; text-wrap: balance; }
h2 { margin-bottom: 8px; font-size: 28px; letter-spacing: -.035em; line-height: 1.1; }
h3 { margin-bottom: 4px; font-size: 18px; letter-spacing: -.02em; }
.token-field { display: grid; gap: 8px; width: min(250px, 100%); color: var(--muted); font-size: 12px; font-weight: 600; }
.token-field input { width: 100%; }
.card { border-radius: 28px; background: var(--surface); box-shadow: var(--shadow); }
.intervention-card { position: relative; margin-bottom: 22px; padding: clamp(26px, 5vw, 52px); overflow: hidden; }
.intervention-card[data-state="active"] { box-shadow: 0 0 0 2px var(--blue), var(--shadow); }
.intervention-card[data-state="idle"] { box-shadow: 0 0 0 1px rgb(0 0 0 / 4%), var(--shadow); }
.intervention-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
.intervention-kicker { margin: 0 0 14px; color: var(--blue); font-size: 13px; font-weight: 700; letter-spacing: .02em; }
#intervention-state { margin: 0; color: var(--muted); font-size: 17px; line-height: 1.45; text-wrap: pretty; }
.status-pill { display: inline-flex; align-items: center; gap: 8px; flex: 0 0 auto; padding: 8px 13px; border-radius: 999px; background: #f5f5f7; color: var(--muted); font-size: 12px; font-weight: 700; letter-spacing: .01em; }
.status-pill::before { width: 7px; height: 7px; border-radius: 50%; background: #a1a1a6; content: ""; }
.intervention-card[data-state="active"] .status-pill { background: #e8f2ff; color: var(--blue-dark); }
.intervention-card[data-state="active"] .status-pill::before { background: var(--blue); }
.intervention-instruction { max-width: 680px; margin: 30px 0 28px; font-size: clamp(25px, 4vw, 42px); font-weight: 600; letter-spacing: -.04em; line-height: 1.08; text-wrap: balance; }
.handoff-steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 0 0 28px; padding: 0; list-style: none; }
.handoff-steps li { display: flex; align-items: center; gap: 10px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 13px; }
.handoff-steps li::before { display: grid; place-items: center; width: 22px; height: 22px; flex: 0 0 22px; border-radius: 50%; background: #f5f5f7; color: var(--ink); font-size: 11px; font-weight: 700; content: attr(data-step); }
.handoff-steps li:first-child { border-top-color: var(--blue); color: var(--ink); }
.handoff-steps li:first-child::before { background: var(--blue); color: #fff; }
.epoch { display: block; min-height: 18px; margin-bottom: 18px; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.action-dock { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px; border-radius: 20px; background: var(--dock); color: #fff; }
.dock-copy { display: grid; gap: 3px; min-width: 0; }
.dock-label { color: #a1a1a6; font-size: 11px; font-weight: 600; letter-spacing: .02em; }
.dock-value { overflow: hidden; color: #fff; font-size: 14px; font-variant-numeric: tabular-nums; text-overflow: ellipsis; white-space: nowrap; }
.button-row { display: flex; flex-wrap: wrap; gap: 10px; }
.button { min-height: 42px; padding: 10px 17px; border: 0; border-radius: 999px; font-size: 14px; font-weight: 600; transition: background-color 140ms ease-out, color 140ms ease-out, transform 140ms ease-out, opacity 140ms ease-out; }
.button:active:not(:disabled) { transform: scale(.96); }
.button-primary { background: var(--blue); color: #fff; }
.button-primary:hover:not(:disabled) { background: var(--blue-dark); }
.button-secondary { background: #f5f5f7; color: var(--ink); }
.button-secondary:hover:not(:disabled) { background: #e8e8ed; }
.button-dark { background: #fff; color: var(--ink); }
.button-dark:hover:not(:disabled) { background: #e8e8ed; }
.button-danger { background: transparent; color: var(--red); }
.button-danger:hover:not(:disabled) { background: #fff2f3; }
.action-dock .button-danger { color: #ff8a91; }
.action-dock .button-danger:hover:not(:disabled) { background: rgb(255 255 255 / 12%); color: #ffadb2; }
.button:disabled { opacity: .4; }
.action-status { min-height: 20px; margin: 12px 0 0; color: var(--red); font-size: 13px; font-weight: 600; }
.action-status:empty { display: none; }
.technical-disclosure { margin-top: 24px; }
.technical-disclosure summary { width: fit-content; cursor: pointer; color: var(--muted); font-size: 12px; font-weight: 600; }
.technical-disclosure pre { margin-top: 12px; }
.tool-stack { display: grid; gap: 14px; }
.tool-panel { padding: 0 30px; box-shadow: 0 0 0 1px rgb(0 0 0 / 4%); }
.tool-panel > summary { display: flex; align-items: center; justify-content: space-between; min-height: 72px; cursor: pointer; list-style: none; font-size: 18px; font-weight: 600; letter-spacing: -.02em; }
.tool-panel > summary::-webkit-details-marker { display: none; }
.tool-panel > summary::after { color: var(--muted); content: "+"; font-size: 22px; font-weight: 400; }
.tool-panel[open] > summary::after { content: "−"; }
.summary-meta { color: var(--muted); font-size: 12px; font-weight: 500; letter-spacing: 0; }
.tool-body { padding: 2px 0 30px; }
.field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.field { display: grid; gap: 8px; color: var(--muted); font-size: 12px; font-weight: 600; }
.field-wide { grid-column: 1 / -1; }
input, select { width: 100%; min-height: 44px; padding: 10px 13px; border: 1px solid var(--line); border-radius: 12px; background: #fff; color: var(--ink); font-size: 15px; }
input[readonly] { background: #f5f5f7; color: var(--muted); }
select { appearance: none; background-image: linear-gradient(45deg, transparent 50%, #6e6e73 50%), linear-gradient(135deg, #6e6e73 50%, transparent 50%); background-position: calc(100% - 17px) 19px, calc(100% - 12px) 19px; background-repeat: no-repeat; background-size: 5px 5px, 5px 5px; padding-right: 32px; }
.secondary-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 22px; }
.result-block { margin-top: 26px; }
.result-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 9px; color: var(--muted); font-size: 12px; font-weight: 600; }
pre { overflow: auto; max-height: 260px; margin: 0; padding: 15px; border-radius: 14px; background: #f5f5f7; color: #424245; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }
.capability-ref { display: block; margin-top: 12px; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; word-break: break-all; }
.capability-output { margin-top: 18px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 720px) {
  .app-shell { width: min(100% - 28px, 600px); padding-top: 24px; }
  .site-header { display: block; margin-bottom: 34px; }
  .token-field { width: min(100%, 300px); margin-top: 26px; }
  .intervention-header { display: block; }
  .status-pill { margin-top: 18px; }
  .handoff-steps { grid-template-columns: 1fr; gap: 0; }
  .handoff-steps li { padding: 11px 0; }
  .action-dock { align-items: stretch; flex-direction: column; }
  .action-dock .button-row { display: grid; grid-template-columns: 1fr 1fr; }
  .action-dock .button { width: 100%; }
  .tool-panel { padding: 0 20px; }
  .tool-panel > summary { min-height: 64px; }
  .field-grid { grid-template-columns: 1fr; }
  .field-wide { grid-column: auto; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; }
}
</style></head>
<body>
<a class="skip-link" href="#intervention">Skip to handoff</a>
<main class="app-shell">
  <header class="site-header">
    <div><p class="eyebrow">Computer use</p><h1>Operator</h1></div>
    <label class="token-field">Local token<input id="token" type="password" autocomplete="off" spellcheck="false"></label>
  </header>

  <section id="intervention" class="card intervention-card" data-state="idle" aria-labelledby="intervention-heading">
    <div class="intervention-header">
      <div><p class="intervention-kicker">Human handoff</p><h2 id="intervention-heading">Ready when you are.</h2><p id="intervention-state" aria-live="polite">No active intervention.</p></div>
      <span class="status-pill" aria-label="Intervention status">Idle</span>
    </div>
    <p id="intervention-instruction" class="intervention-instruction">Your session is clear.</p>
    <ol class="handoff-steps" aria-label="Handoff steps"><li data-step="1">Claim</li><li data-step="2">Fix blocker</li><li data-step="3">Resume</li></ol>
    <output id="intervention-epoch" class="epoch" aria-live="polite"></output>
    <div class="action-dock" aria-label="Intervention actions">
      <div class="dock-copy"><span class="dock-label">Session control</span><span id="intervention-dock-status" class="dock-value">No active handoff.</span></div>
      <div class="button-row"><button id="intervention-claim" class="button button-dark" type="button" disabled>Claim</button><button id="intervention-resume" class="button button-primary" type="button" disabled>Resume</button><button id="intervention-abort" class="button button-danger" type="button" disabled>Abort</button></div>
    </div>
    <p id="intervention-action-status" class="action-status" role="status" aria-live="assertive"></p>
    <details class="technical-disclosure"><summary>Technical details</summary><pre id="intervention-detail"></pre></details>
  </section>

  <div class="tool-stack">
    <details id="run" class="card tool-panel">
      <summary><span>Run</span><span class="summary-meta">Discover or replay</span></summary>
      <div class="tool-body">
        <div class="field-grid">
          <label class="field">Principal<input id="principal" value="synthetic_alpha" autocomplete="off"></label>
          <label class="field">Account<input id="account" inputmode="numeric" autocomplete="off"></label>
          <label class="field field-wide">Goal<input id="goal" value="Get the available balance for savings account"></label>
        </div>
        <div class="action-dock" style="margin-top:22px">
          <div class="dock-copy"><span class="dock-label">Session</span><span id="session" class="dock-value">Not prepared</span></div>
          <div class="button-row"><button id="prepare" class="button button-dark" type="button">Prepare</button><button id="discover" class="button button-primary" type="button">Discover</button><button id="replay" class="button button-dark" type="button">Replay</button></div>
        </div>
        <div class="result-block"><div class="result-toolbar"><span>Run status</span><button id="read-result" class="button button-secondary" type="button">Read result</button></div><pre id="run-status" aria-live="polite"></pre><pre id="result" aria-live="polite" style="margin-top:10px"></pre></div>
      </div>
    </details>

    <details id="capabilities" class="card tool-panel">
      <summary><span>Capabilities</span><span class="summary-meta">Inspect, validate, approve</span></summary>
      <div class="tool-body">
        <div class="field-grid">
          <label class="field field-wide">Revision<select id="capability-choice"><option value="">Refresh capability list</option></select></label>
          <label class="field">Name<input id="capability-name" readonly></label>
          <label class="field">Version<input id="capability-version" readonly></label>
          <label class="field field-wide">Digest<input id="capability-digest" readonly></label>
        </div>
        <div class="secondary-actions"><button id="refresh" class="button button-secondary" type="button">Refresh</button><button id="inspect" class="button button-secondary" type="button">Inspect</button><button id="validate" class="button button-secondary" type="button">Validate</button><button id="approve" class="button button-primary" type="button">Approve</button></div>
        <output id="capability-ref" class="capability-ref"></output>
        <div class="capability-output"><span class="sr-only">Capability details</span><pre id="capability-list" aria-live="polite"></pre></div>
      </div>
    </details>
  </div>
</main>
<script>
let token = '';
let sessionId = '';
let runId = '';
let capabilities = [];
let intervention = null;
let interventionTimer = null;
const ACTIVE_INTERVENTION_STATES = new Set(['WAITING_FOR_HUMAN', 'HUMAN_CLAIMED', 'RESUMING']);
const $ = id => document.getElementById(id.startsWith('#') ? id.slice(1) : id);
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
function interventionInstruction(value) {
  const state = String(value.state || '');
  if (state === 'HUMAN_CLAIMED') return 'Resolve the blocker in the same window.';
  if (state === 'RESUMING') return 'Keep the same window open while we verify.';
  const instructions = {
    PRECONDITION_UNKNOWN: 'Confirm the page is ready in the same window.',
    POSTCONDITION_UNKNOWN: 'Check the page result in the same window.',
    ACCESS_DENIED: 'Review access in the same window.',
    ACCOUNT_NOT_FOUND: 'Check the requested account in the same window.',
    ACCOUNT_TYPE_MISMATCH: 'Choose the requested account type in the same window.',
    AUTHENTICATION_CHANGED: 'Sign in again with the original principal.',
    SESSION_EXPIRED: 'Sign in again with the original principal.',
    SUBJECT_MISMATCH: 'Confirm the original principal is signed in.',
  };
  return instructions[String(value.reason || '')] || 'Resolve the blocker in the same window.';
}
function showIntervention(value) {
  const active = Boolean(value && ACTIVE_INTERVENTION_STATES.has(value.state));
  intervention = active ? value : null;
  const card = $('#intervention');
  const state = $('#intervention-state');
  const epoch = $('#intervention-epoch');
  const detail = $('#intervention-detail');
  const instruction = $('#intervention-instruction');
  const dockStatus = $('#intervention-dock-status');
  const actionStatus = $('#intervention-action-status');
  const statusPill = document.querySelector('.status-pill');
  const buttons = ['intervention-claim', 'intervention-resume', 'intervention-abort'];
  card.dataset.state = active ? 'active' : 'idle';
  document.body.classList.toggle('has-intervention', active);
  if (!value) {
    state.textContent = 'No active intervention.';
    instruction.textContent = 'Your session is clear.';
    dockStatus.textContent = 'No active handoff.';
    epoch.textContent = '';
    detail.textContent = '';
    actionStatus.textContent = '';
    statusPill.textContent = 'Idle';
    statusPill.setAttribute('aria-label', 'Intervention status: idle');
    buttons.forEach(id => { $(id).disabled = true; });
    return;
  }
  state.textContent = `${value.state} · ${value.reason}`;
  instruction.textContent = active ? interventionInstruction(value) : 'No action needed.';
  dockStatus.textContent = !active
    ? 'No active handoff.'
    : value.state === 'RESUMING'
      ? 'Verification is in progress.'
      : 'Automation is paused until you resume.';
  epoch.textContent = `Epoch ${value.epoch}`;
  detail.textContent = JSON.stringify(value, null, 2);
  actionStatus.textContent = '';
  statusPill.textContent = !active ? 'Inactive' : value.state === 'RESUMING' ? 'Verifying' : 'Action needed';
  statusPill.setAttribute('aria-label', `Intervention status: ${active ? value.state : 'inactive'}`);
  $('#intervention-claim').disabled = !active || value.state !== 'WAITING_FOR_HUMAN';
  $('#intervention-resume').disabled = !active || value.state !== 'HUMAN_CLAIMED';
  $('#intervention-abort').disabled = !active || value.state === 'RESUMING';
  if (active) scheduleInterventionPoll();
}
function scheduleInterventionPoll() {
  if (interventionTimer !== null) return;
  interventionTimer = setTimeout(() => { interventionTimer = null; void refreshInterventions(); }, 2000);
}
function selectIntervention(value) {
  const items = Array.isArray(value) ? value : [];
  if (runId) {
    const current = items.find(item => item.run_id === runId) || null;
    if (current && !sessionId && typeof current.session_id === 'string' && current.session_id) {
      sessionId = current.session_id;
      $('#session').textContent = sessionId;
    }
    return current;
  }
  const candidates = items.filter(item =>
    item && ACTIVE_INTERVENTION_STATES.has(item.state) &&
    typeof item.run_id === 'string' && item.run_id &&
    typeof item.session_id === 'string' && item.session_id
  );
  if (candidates.length !== 1) return null;
  runId = candidates[0].run_id;
  sessionId = candidates[0].session_id;
  $('#session').textContent = sessionId;
  return candidates[0];
}
async function refreshInterventions() {
  const response = await api('/api/interventions');
  const value = await response.json();
  const current = selectIntervention(value);
  showIntervention(current || null);
  if (current && ['WAITING_FOR_HUMAN', 'HUMAN_CLAIMED', 'RESUMING'].includes(current.state)) scheduleInterventionPoll();
}
$('#token').addEventListener('change', () => {
  if ($('#token').value.trim()) void refreshInterventions();
});
async function interventionAction(action) {
  if (!intervention) return;
  const response = await api(`/api/interventions/${encodeURIComponent(intervention.intervention_id)}/${action}`, {
    method: 'POST', body: JSON.stringify({expected_epoch: intervention.epoch})
  });
  const value = await response.json();
  if (response.ok) showIntervention(value);
  else {
    $('#intervention-detail').textContent = JSON.stringify(value, null, 2);
    $('#intervention-action-status').textContent = value && typeof value.code === 'string' ? value.code : 'Action could not be completed.';
  }
  if (response.ok && value.state === 'RUNNING') void poll();
}
$('#intervention-claim').onclick = () => void interventionAction('claim');
$('#intervention-resume').onclick = () => void interventionAction('resume');
$('#intervention-abort').onclick = () => void interventionAction('abort');
$('#read-result').onclick = async () => {
  if (!runId) { $('#result').textContent = 'Start a run first.'; return; }
  const r = await api(`/api/runs/${runId}/result`); $('#result').textContent = JSON.stringify(await r.json(), null, 2);
};
</script></body></html>"""


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
