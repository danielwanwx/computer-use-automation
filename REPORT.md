# Report

## Architecture

The system is a single Python 3.12 process. It is small enough to reason about, and the boundaries that matter are enforced as module seams rather than services:

```text
goal ──► GoalBinder ──► DiscoveryRuntime ◄──► DecisionBackend (LLM: sees a value-free projection)
                               │ typed decisions
                               ▼
Surface adapter ◄──── ExecutionGateway ────► PolicyEngine, EvidenceSink
(Playwright + profile)         ▲    (every browser effect: authorize → re-resolve → dispatch → verify)
                               │
verified trace ──► Compiler ──► Registry (DRAFT → VALIDATED → APPROVED) ──► ReplayRuntime (no model)
                                                                               │ UNKNOWN_BLOCKER, lost session…
                                                                               ▼
                                                            HandoffService ◄──► SessionActor (owner + epoch)
```

Key decisions:

- **The model never sees the page.** The surface adapter turns the browser into a typed `Observation` and `NormalizedView`: route, page state from the profile's readiness rules, controls with opaque refs, and boolean signals such as `overview_complete` or `requested_account_matches`. The LLM receives only a projection of these with no values, and the requested account appears as `<requested_account>`. It answers with a strict-schema decision (`CLICK c_… | WAIT | DONE | BLOCKED`). Two trade-offs follow. A model that cannot see values cannot leak them, and it cannot be prompt-injected by page text. In exchange, it can only act on what the profile makes observable.
- **One gateway for every effect.** Discovery and replay go through the same `ExecutionGateway`. At dispatch time it re-checks policy against the live target, re-resolves the locator, and dispatches. It then verifies the effect, classifying it as `NOT_DISPATCHED`, `DISPATCHED`, `VERIFIED`, or `OUTCOME_UNKNOWN`, and writes evidence. Neither the LLM nor the artifact can reach the browser any other way.
- **A reviewer closes the capability; it does not come from the transcript.** The compiler accepts only *verified* observed events from the trace and merges them with a human-authored blueprint. The blueprint supplies the contract, a membership assertion, extraction, and the final verification. Every step records where it came from (`observed` / `declared` / `reviewer_added`). The artifact is therefore reviewable and not a transcript of whatever the model happened to do. The cost is honest: in this slice the model *discovered* the navigation (open the requested account from the overview, then decide it was done), while the checks around it are declared.
- **Validation needs an independent oracle.** A draft is replayed on a *different* synthetic customer, and the output must match ParaBank's REST API (`testbed/oracle.py`, never imported by `src/`). Approval binds to the exact digest plus a runtime fingerprint (source, lock file, parser/condition/profile hashes), the browser version, and the target revision. Changing any of them invalidates the approval.

Choices: Playwright with Chrome was chosen because the target is web. The seam that matters for other surfaces is the observation contract, not Playwright. The decision backend is pluggable, and reviewers should not need an API key. `auto` uses `OPENAI_API_KEY` when it is set (pinned snapshot `gpt-5.5-2026-04-23`). Otherwise it borrows a locally signed-in Claude Code, Codex, or Cursor CLI, calling it non-interactively once per decision: empty directory, no tools, no project rules or MCP servers, and the reply constrained to the decision schema. The committed evidence was recorded this way through Claude Code (`claude-opus-5-5`); the same lifecycle also passed on the OpenAI API and on Codex. Execution is synchronous and in-process; the HTTP service (`cua serve`) is a thin boundary over the same `ApplicationService`.

## Artifact schema

[`artifacts/get_savings_balance-1.0.0.json`](artifacts/get_savings_balance-1.0.0.json) is a `CapabilityBundle`, stored as canonical JSON and identified by its SHA-256 digest.

| Section | What it holds | Why |
|---|---|---|
| `capability`, `schema_version` | name + semver | stable identity for callers |
| `contract` | typed inputs (`account_id: string, ^[0-9]+$, sensitive`), typed outputs (`available_balance: decimal_string, sensitive`, `currency: enum[USD]`), declared `business_outcomes` (`ACCOUNT_NOT_FOUND`, `ACCESS_DENIED`) | what an agent must supply and can get back; sensitivity drives redaction |
| `targets` | `ref → locator key + allowed operations (+ input binding)`, e.g. `requested_account_link → TABLE_ACCOUNT_LINK_BY_INPUT`, CLICK, bound to `inputs.account_id` | steps name *semantic* targets; the profile resolves them (see below) |
| `steps` | ordered `CLICK / ASSERT / EXTRACT / VERIFY`, each with pre/postconditions, `recovery_ref`, `output_ref` + pinned parser, and `source` provenance with trace event ids | the executable flow plus how each step is checked and where it came from |
| `conditions` | named, typed expressions (`overview_complete`, `principal_matches`, `account_present(input)`, `field_equals(target, input)`, `page_state`, …) | checkpoints are data, evaluated tri-state (PASS/FAIL/UNKNOWN) |
| `recoveries` | e.g. `readonly_overview_anchor`: re-anchor on the overview, max 2 attempts | recovery is declared and bounded, never improvised |
| `compatibility`, `parsers` | profile id + hash, condition-runtime hash, parser id/version/implementation hash | detect drift in anything the steps depend on |
| `provenance` | trace id, completion-proof id, `verified: true` | ties the artifact to the run that justified it |

**Locator strategy.** An artifact never contains a CSS selector or XPath. It names a locator *key*, and the per-app profile (`src/cua/profiles/parabank.py`) defines each key as a role plus exact accessible name. When a key is resolved, the element must be unique and visible:

- a nav link is found by `role=link, name="Accounts Overview"`,
- an account link is found as a link in the `Account` column of `#accountTable` whose text equals the bound input,
- a detail field is found by a selector that must also be *label-related* to its expected label (`Available:`).

No match is positional or index-based. Ambiguity (several matches) fails closed as `TARGET_AMBIGUOUS`. This matters for legacy screens with no test IDs: labels and table structure survive re-skins much better than generated IDs or DOM position do.

## Determinism & error handling

Replay has no model dependency. `ReplayRuntime` takes no decision backend, and the demo scripts make any provider call a hard failure. Given the same inputs, it runs the same steps. Each step:

1. waits, up to a bounded condition timeout inside the run deadline, until its preconditions evaluate `PASS`;
2. dispatches through the gateway;
3. requires its postcondition *observed* before advancing (`OVERVIEW_READY`, `DETAIL_READY`, `output_valid`).

The final `VERIFY` step re-proves, from a fresh observation, that the principal is correct, that the account is on the complete overview and is the requested one, and that it has type `SAVINGS` and a parseable USD value. Outputs exist only after that check.

The result contract is `InvocationResult`:

| Class | Examples | Behaviour |
|---|---|---|
| `SUCCESS` | outputs | returned only after `VERIFY` |
| `BUSINESS_OUTCOME` | `ACCOUNT_NOT_FOUND` (account absent from a *complete* overview), `ACCESS_DENIED` (exact denial alert) | a legitimate answer for the caller; an *incomplete* overview never becomes "not found" |
| recoverable (internal) | loading states, stale observation, click with no effect, lost click receipt | wait inside the deadline; re-observe before any retry (`OUTCOME_UNKNOWN` is never blindly re-clicked); re-anchor via the declared recovery, at most 2 attempts |
| escalation | unclassified modal (`UNKNOWN_BLOCKER`), `SESSION_EXPIRED` / `SESSION_LOST`, `PRECONDITION_UNKNOWN`, `OUTCOME_UNKNOWN` | pause and request a human (below) |
| `FAILURE` | `PRECONDITION_FAILED` (e.g. a checking account passed in), `INPUT_INVALID`, `ACCOUNT_TYPE_MISMATCH`, `APP_ERROR`, `AMBIGUOUS_STATE`, `INVALID_BUNDLE` | stop, with `reason_code`, `step_id`, `effect_state`, and evidence refs to safe snapshots of what was observed |

The evidence shows each class on the real target: six replays in one reused session, plus the handoff run ([RUN_LOG.md](evidence/RUN_LOG.md)).

Writing the live evidence surfaced three defects that the offline fakes had missed, and all three are fixed with regression tests:

1. "Not found" was reported as a hard failure when it was detected by the membership assertion rather than by the account click.
2. An unexpected dialog failed a precondition instead of escalating.
3. In a reused session, a detail page left over from the previous invocation was treated as an identity mismatch.

UI drift is secondary here because the target is stable, but it is handled the same way: a readiness rule that no longer matches makes the page state `UNKNOWN`, and that escalates rather than acting on a page the profile does not recognise.

## Heterogeneity & multi-tenant

**The seam: the artifact talks in semantic targets and page states; the surface adapter plus profile turns a concrete screen into those.** Everything above the `Observation` / `NormalizedView` contract (conditions, steps, replay, verifier, policy, handoff) is surface-agnostic.

- **Legacy web** (framesets, nested tables, no semantics). This is the implemented case. ParaBank has no test IDs, and the profile anchors on roles, labels, and table columns. Observations already carry `frame_ref` and per-frame generations, so framesets become additional frames in the same model rather than a new design.
- **Desktop.** A second adapter would build the same `Observation` from the OS accessibility tree (UIA on Windows, AX on macOS). Profile locator keys would map to role, name, and ancestor path, dispatch would use accessibility actions, and readiness rules would become accessibility predicates. For controls with no accessibility data, a screenshot-plus-coordinates adapter can implement the same contract with a visual checkpoint as the postcondition. It would be the least trusted option and marked as such in `compatibility`. The artifact schema does not change.
- **Multi-tenant reuse.** A tenant is a (vendor product, version, configuration) triple. The artifact should be recorded once per vendor product and version and bound to a vendor profile. Each tenant adds a small overlay for labels, route names, branding, locale, and extra interstitials. The schema already has `profile_overrides` (`field → locator key`), which is the hook for per-tenant specialization without re-recording. Resolution order: tenant overlay → vendor/version profile → base.
- **Drift detection.** Approval is pinned to profile hash, target revision, and browser, so a tenant upgrade invalidates approval until the capability re-validates on that tenant's validation fixture against an oracle. Readiness rules fail closed at runtime. Beyond what is implemented, I would run per-tenant canary replays on a schedule and group failures by vendor version. That way one vendor upgrade shows up as one drift incident across its tenants rather than hundreds of separate failures.

Implemented: one profile, the pins, and fail-closed readiness. Overlays, a second adapter, and canaries are design only.

## Escalation & handoff

**Detect.** Replay raises an intervention on conditions it cannot resolve safely: an unclassified modal, session expiry or loss, a precondition that stays `UNKNOWN` past its timeout, or an effect whose outcome is unknown. It does not escalate on business outcomes or on definite failures. The request carries the capability, run, step id, reason code, and page id; a safe structural snapshot of the blocked page is already in evidence.

**Control-transfer model.** Each browser session has a `SessionActor` with an **owner** (`RUNTIME`, `HUMAN`, `RESUMING`, or `NONE`) and a monotonically increasing **epoch**. Every automation command carries the epoch it expects, so a stale command cannot touch the page.

1. **Claim.** The operator claims at the epoch they were shown. Ownership moves to `HUMAN` and the epoch is bumped, so automation is fenced out.
2. **Act.** The human works in the *same* Playwright page. There is no new session and no re-login.
3. **Resume.** Resume takes a `RESUMING` lease and reconciles. It re-observes the page and requires the same principal, authentication generation, origin, and page before answering `NEXT`, `RETRY_SAFE`, or `REMAIN_PAUSED`. If someone logged in as a different user or navigated off-target, the run stays paused. Only then does ownership return to `RUNTIME`, with the run's retry budget and deadline preserved.

**Record.** The evidence keeps every intervention state change (state, owner, epoch, step, reason), the operator's claim and resume, and a structural record of the human action: category, same-page check, dialog present before and after, and click targets by tag and role. No text is recorded.

**Shown.** In [`evidence/handoff_run/`](evidence/handoff_run/handoff_summary.json), replay of the approved artifact meets an injected unknown dialog. The run goes `WAITING_FOR_HUMAN` (epoch 1) → `HUMAN_CLAIMED` (2) → dismissal in the same page → `RESUMING` (3) → `RUNNING` (4) → `SUCCESS`, and the output matches the oracle. That run used a scripted operator for reproducibility. `--operator person --headed` runs the same flow for a person, and an earlier person-operated rehearsal is recorded. The operator surface is minimal: the loopback page from `cua serve` provides list, claim, resume, and abort, while the demo uses terminal prompts. Co-browsing is out of scope; the headed browser window *is* the live session.

## Safety

- **Allowlist.** `PolicyContext` holds three allowlists (deployment, capability, and run) for origins, routes, and operations. An action must be permitted by all three. The check runs *at dispatch* against the gateway's classification of the live target, not against labels in the artifact, so a tampered artifact cannot widen its own authority. The gateway also rejects any control the current observation did not offer.
- **Risky actions are blocked, not confirmed.** Only `READ_ONLY` targets are authorized; `WRITE` and `UNKNOWN` fail with `RISK_NOT_READ_ONLY`. The goal binder also rejects write intents ("transfer", "pay", "close", …) before any UI or model access. For a future write capability the plan is to:
  - keep blocking by default,
  - make each irreversible step an explicit, approved step that raises a confirmation intervention through the same handoff path,
  - require idempotency keys,
  - never auto-retry `OUTCOME_UNKNOWN` on a write.
- **Data handling.** Credentials are referenced only by environment-variable name and typed into the target's login form. Account numbers and balances live in memory as `SecretStr`. The model sees none of them, and its free-text rationale is stored only with digits masked. Evidence is built from an allow-list: typed events, reason codes, and structural snapshots with route, page state, and control roles and counts. Text, URLs with query strings, and DOM content are never stored. Exported evidence and artifacts are scanned for every synthetic value from the run, and the export fails if one is found. The HTTP service binds to loopback and requires a bearer token and an exact Host/Origin match; results are read only through an explicit call.
- **Limits.**
  - There are no screenshots in the evidence: the redaction guarantee costs some debuggability.
  - Model rationale is free text; masking digits is a heuristic.
  - A borrowed local agent CLI is a larger surface than an API call. It gets no tools, an empty directory, no project rules or MCP servers, and only the value-free request; the isolation still relies on the CLI honouring those flags.
  - The operator is a single local token with no roles.
  - The value scan checks only this run's synthetic values; it is not a general PII detector.

## Cuts

- **Narrow discovery action space.** This is the biggest cut. The model chooses among the controls the profile exposes for this intent, which is effectively the navigation link and the requested account link. The discovery is real, but the choices are few. Next step: have the profile expose every allowlisted control on a page, and enable `TYPE_TEXT` and `SELECT` with value references. The typed decision models already define them; the gateway and replay do not execute them yet.
- **One read-only capability, one surface.** Multi-tenant overlays, a desktop adapter, and canary replays are design only (see above).
- **Discovery does not escalate.** A stuck discovery ends `BLOCKED` with a reason code; only replay routes to a human.
- **Approval does not persist across environments.** Approval happens in a temporary registry during each run. The committed artifact is a DRAFT, and `review_native_bundle.py` re-validates it before replay.
- **No screenshots or recording.** Evidence is structural only (see Safety). Next: redacted screenshots with masked value regions.
- **Minimal operator console.** Next steps are a proper intervention queue, per-operator identity, and live-view streaming.
- **With more time:** a confidence score from multi-run stability (the evidence already has 10 runs); a bounded, policy-checked single-step LLM recovery during replay, recorded as evidence; and generating a runnable Playwright test from an artifact.
