## Architecture

The runtime is a Python 3.12 modular monolith. `ApplicationService` owns prepared sessions, run lifetimes, idempotency, capability lifecycle, validation, and safe result access. The FastAPI adapter and the stdlib `cua` client are thin boundaries over that service; the CLI does not construct a browser or service except for its explicit `cua serve` composition path. `llm/decisions.py` supports disabled, OpenAI Responses, and saved-login Codex CLI modes; replay never constructs or requires a decision backend.

A session uses an isolated Playwright `BrowserContext`, a serial epoch-fenced actor, and a profile-driven surface. Discovery and replay share one `ExecutionGateway`. Discovery may use an injected decision backend, while replay has no model dependency. The only target profile is the original ParaBank application at immutable commit `ee82474be5f58bea3ddc8be0fd831072b00201cb`, reached through loopback.

The testbed is separate from runtime code. It prepares the pinned upstream WAR, seeds synthetic customers under an exclusive lock, and provides the independent oracle process. Runtime validation communicates with a configured oracle executable through bounded stdin/stdout transport.

## Artifact schema

A capability is a typed, versioned `CapabilityBundle`. Its canonical digest covers the executable steps, target definitions, conditions, parser and profile references, recovery, declared checkpoints, and runtime compatibility data. The registry lifecycle is `DRAFT` → `VALIDATED` → `APPROVED`; approval is bound to the exact digest and current qualification fingerprint.

Evidence uses typed safe events, SQLite run metadata, JSONL event records, and value-free structural snapshots. Goals, account values, balances, credentials, tokens, raw URLs, and model payloads are excluded from persisted evidence. `artifacts/index.json` contains the value-safe DRAFT exported by the saved-login Codex lifecycle; its approval sidecar was temporary and is not a releasable capability. `evidence/index.json` records that V1 diagnostic, the current-source V2 native loopback replay, the V9 real-person headed same-session takeover, and a partial V11 clean-checkout diagnostic.

## Determinism & error handling

Replay uses fixed typed actions, strong-Kleene condition results, strict finite USD parsing, explicit preconditions and postconditions, and a runtime-owned completion verifier. Effects are tracked as `NOT_DISPATCHED`, `DISPATCHED`, `VERIFIED`, or `OUTCOME_UNKNOWN`; an uncertain effect is observed before any retry. Recovery is bounded and preserves the run deadline and retry count.

Application and HTTP boundaries convert expected failures to stable safe codes. The web surface requires bearer authentication, exact loopback Host/Origin checks for writes, a same-origin CSRF marker, and `Cache-Control: no-store`. The CLI sends JSON to the same service and reads tokens from the environment or a hidden prompt. The provider is disabled by default. The saved-login Codex diagnostic used the installed CLI in an empty read-only workspace and scrubbed API-key, access-token, and federation-rule environment variables from the child; OpenAI mode was not used.

Offline unit and contract checks cover condition logic, parser boundaries, registry invalidation, policy denial, evidence redaction, actor fencing, replay without model calls, oracle transport, the web boundary, and CLI HTTP behavior. These checks do not substitute for the unrun native/live acceptance cases listed by the verification entrypoint.

## Heterogeneity & multi-tenant

The target profile pins the ParaBank route, DOM readiness markers, control operations, detail labels, and USD field semantics. The native page uses `Balance*` and `Available:` labels; those observed differences are encoded in the profile without modifying ParaBank. Browser version, target revision, profile, and runtime fingerprint are checked before approved execution.

Principal specifications and validation fixture bindings are server-owned configuration. Each ordinary session has an isolated browser context and target lock ownership. The current release supports one local target profile and synthetic principals; it does not claim production multi-tenant isolation, remote browsers, arbitrary sites, or cross-target capability portability.

## Escalation & handoff

The handoff package defines session-bound requests, a stable session/intervention-scoped operator credential, actor ownership, epoch fencing, reconciliation outcomes, and safe state callbacks. The application API and UI are wired for list, inspect, claim, resume, and abort; actor and protocol tests cover stale claims, duplicate claims, draining, and wrong-principal reconciliation. A real person completed the headed-browser V9 rehearsal in the same session; its safe entry contains a completion attestation and structural run facts only.

A waiting run must remain safe and bounded. The headed-browser procedure is: observe the safe blocker, claim the displayed intervention epoch, act in the same browser page, then resume. Resume must revalidate the original identity, authentication generation, page, target origin, and readiness; a wrong-principal login or changed page is expected to keep the run paused or abort it. Automatic replay cannot resume from a stale epoch or a changed principal. V9 is `PASS`: a real person operated the same headed browser session, the route was verified, and the run resumed successfully.

## Safety

Policy authorization is applied immediately before browser actions and reclassifies the live destination independently of artifact risk labels. Unknown or unsafe controls fail closed. Account membership is derived from a complete visible overview and the final result is released only after current identity, target, route, account type, currency, and parser checks pass.

Credentials are referenced by environment variable names and entered through the target UI. Protected request values remain in memory as `SecretStr`; safe evidence never receives their raw values. The testbed uses an exclusive reset lock while runtime sessions hold the shared lock. The control server binds to loopback and generated operator tokens are printed once to stderr when no configured token exists.

## Cuts

The following limits are deliberate and are recorded as `NOT_RUN` or pending rather than presented as completed product behavior: release-qualified live discovery (V1), zero-model native replay and recovery (V3), native boundary, safety, and recovery cases (V4–V7 and V10), native takeover protocol qualification (V8), clean-checkout native no-model replay (V11), and browser end-to-end operation-page completion (V12). V2 is passing current-source native-loopback evidence, and V9 is passing same-session real-person takeover evidence. An opt-in saved-login Codex lifecycle produced a verified trace and temporary DRAFT, but V1 remains `NOT_RUN` because the artifact sidecar was not committed and the release verifier rejects the diagnostic schema. V11 has only a partial clean-checkout/offline diagnostic and no approved real artifact. No OpenAI API credential was used, and no live-qualified capability digest is included.

Only `get_savings_balance(account_id)` is supported. Transfer, account opening, arbitrary DSL operations, remote deployment, desktop adapters, and production multi-tenant administration are out of scope. Qualification binds a composite runtime fingerprint covering executable Python sources, `pyproject.toml`, `uv.lock`, and the Python implementation major/minor marker; parser, condition, and profile hashes remain separate pins, while browser and target revisions remain separate qualification fields. The release verifier additionally fingerprints testbed Python sources. Repository publication and visibility are external distribution state and do not count as V1–V12 acceptance evidence.
