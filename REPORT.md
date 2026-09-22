## Architecture

The runtime is a Python 3.12 modular monolith. `ApplicationService` owns prepared sessions, run lifetimes, idempotency, capability lifecycle, validation, and safe result access. The FastAPI adapter and the stdlib `cua` client are thin boundaries over that service; the CLI does not construct a browser or service except for its explicit `cua serve` composition path.

A session uses an isolated Playwright `BrowserContext`, a serial epoch-fenced actor, and a profile-driven surface. Discovery and replay share one `ExecutionGateway`. Discovery may use an injected decision backend, while replay has no model dependency. The only target profile is the original ParaBank application at immutable commit `ee82474be5f58bea3ddc8be0fd831072b00201cb`, reached through loopback.

The testbed is separate from runtime code. It prepares the pinned upstream WAR, seeds synthetic customers under an exclusive lock, and provides the independent oracle process. Runtime validation communicates with a configured oracle executable through bounded stdin/stdout transport.

## Artifact schema

A capability is a typed, versioned `CapabilityBundle`. Its canonical digest covers the executable steps, target definitions, conditions, parser and profile references, recovery, declared checkpoints, and runtime compatibility data. The registry lifecycle is `DRAFT` → `VALIDATED` → `APPROVED`; approval is bound to the exact digest and current qualification fingerprint.

Evidence uses typed safe events, SQLite run metadata, JSONL event records, and value-free structural snapshots. Goals, account values, balances, credentials, tokens, raw URLs, and model payloads are excluded from persisted evidence. `artifacts/index.json` remains empty because no live provider discovery has produced a releasable capability; `evidence/index.json` records the fingerprinted V2 native loopback replay and V11 clean-checkout evidence.

## Determinism & error handling

Replay uses fixed typed actions, strong-Kleene condition results, strict finite USD parsing, explicit preconditions and postconditions, and a runtime-owned completion verifier. Effects are tracked as `NOT_DISPATCHED`, `DISPATCHED`, `VERIFIED`, or `OUTCOME_UNKNOWN`; an uncertain effect is observed before any retry. Recovery is bounded and preserves the run deadline and retry count.

Application and HTTP boundaries convert expected failures to stable safe codes. The web surface requires bearer authentication, exact loopback Host/Origin checks for writes, a same-origin CSRF marker, and `Cache-Control: no-store`. The CLI sends JSON to the same service and reads tokens from the environment or a hidden prompt. The provider is disabled by default and no provider credential was used for this release state.

Offline unit and contract checks cover condition logic, parser boundaries, registry invalidation, policy denial, evidence redaction, actor fencing, replay without model calls, oracle transport, the web boundary, and CLI HTTP behavior. These checks do not substitute for the unrun native/live acceptance cases listed by the verification entrypoint.

## Heterogeneity & multi-tenant

The target profile pins the ParaBank route, DOM readiness markers, control operations, detail labels, and USD field semantics. The native page uses `Balance*` and `Available:` labels; those observed differences are encoded in the profile without modifying ParaBank. Browser version, target revision, profile, and runtime fingerprint are checked before approved execution.

Principal specifications and validation fixture bindings are server-owned configuration. Each ordinary session has an isolated browser context and target lock ownership. The current release supports one local target profile and synthetic principals; it does not claim production multi-tenant isolation, remote browsers, arbitrary sites, or cross-target capability portability.

## Escalation & handoff

The handoff package defines session-bound requests, a stable session/intervention-scoped operator credential, actor ownership, epoch fencing, reconciliation outcomes, and safe state callbacks. The application handoff wiring and UI are complete, and actor/protocol tests cover stale claims, duplicate claims, draining, and wrong-principal reconciliation. A browser-backed operator intervention remains pending.

A waiting run must remain safe and bounded. Automatic replay cannot resume from a stale epoch or a changed principal. V9, which requires a real person to operate the same browser session and recover successfully, is explicitly `NOT_RUN`.

## Safety

Policy authorization is applied immediately before browser actions and reclassifies the live destination independently of artifact risk labels. Unknown or unsafe controls fail closed. Account membership is derived from a complete visible overview and the final result is released only after current identity, target, route, account type, currency, and parser checks pass.

Credentials are referenced by environment variable names and entered through the target UI. Protected request values remain in memory as `SecretStr`; safe evidence never receives their raw values. The testbed uses an exclusive reset lock while runtime sessions hold the shared lock. The control server binds to loopback and generated operator tokens are printed once to stderr when no configured token exists.

## Cuts

The following limits are deliberate and are recorded as `NOT_RUN` or pending rather than presented as completed product behavior: live model discovery (V1), zero-model native replay and recovery (V3), native boundary, safety, and recovery cases (V4–V7 and V10), native takeover protocol qualification (V8), real-person handoff (V9), and browser end-to-end operation-page completion (V12). V2 has a fingerprinted native loopback replay record, and V11 has a fingerprinted clean-checkout record. The user deferred provider credentials, so no live-qualified capability digest or real approved replay artifact is included.

Only `get_savings_balance(account_id)` is supported. Transfer, account opening, arbitrary DSL operations, remote deployment, desktop adapters, and production multi-tenant administration are out of scope. Qualification binds a composite runtime fingerprint covering executable Python sources, `pyproject.toml`, `uv.lock`, and the Python implementation major/minor marker; parser, condition, and profile hashes remain separate pins, while browser and target revisions remain separate qualification fields. The release verifier additionally fingerprints testbed Python sources. Public publication and email delivery were not performed.
