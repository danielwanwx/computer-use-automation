# DESIGN.md — Phase 1 map

This is a module and acceptance index for the copied playbook and Spec, not a replacement design. The copied source documents are normative. Implemented behavior and current verification state are tracked in `PROGRESS.md`.

| Responsibility | Initial module area | Spec |
|---|---|---|
| Shared use cases and web/CLI entry points | app.py, web/, cli.py | §§2–3, 11 |
| Isolated sessions, observations, controls, browser actions | sessions/, surface/ | §§2–5, 9 |
| Bounded real-model exploration | discovery/, llm/ | §§3, 6 |
| Shared action authorization and safe event capture | policy/, execution/, evidence/ | §§3, 10 |
| Typed capability, condition/parser registry, validation and approval | compiler/, registry/, conditions/, verification/ | §§3–4, 7 |
| Model-free replay and recovery | replay/, execution/ | §§2–3, 8 |
| Same-session operator control | handoff/, sessions/ | §§3, 9 |
| Native target and isolated fixtures/oracle | profiles/, testbed/, tests/ | §§2, 5, 12–13 |

Use the suggested layout in Spec §12.2 as the starting boundary. The runtime must not import fixture, fault-injection, or evaluator code. Discovery and replay use one execution gateway; ReplayRuntime has no model-backend dependency. Current replay accepts only the pinned `get_savings_balance` contract and CLICK/EXTRACT/ASSERT/WAIT/VERIFY steps; other browser operations fail closed before UI access.

## Acceptance invariants

- Capability approval covers the complete executable closure: schema and capability revision, contract, steps, locator targets, condition definitions, recovery, parser identity and implementation version, executable profile content, and verifier/runtime compatibility. Validation also identifies browser and target revisions. A change to executable content or qualification invalidates the prior approval.
- Each invocation builds a new MembershipProof bound to the session, authentication generation, account binding, and complete overview observation. Recheck it before detail access and completion. Authentication change or handoff invalidates it; recovery must prove the same principal and membership again.
- Replay tracks NOT_DISPATCHED, DISPATCHED, VERIFIED, and OUTCOME_UNKNOWN. After uncertain effect, observe before retry. Anchor recovery and handoff retain retry counts and the run deadline. Only verified postconditions advance the step.
- The session actor drains or marks an in-flight action unknown before human control, fences stale requests by epoch, and reconciles resume as NEXT, RETRY_SAFE, or REMAIN_PAUSED using current identity and page evidence.
- V1–V12 remain the acceptance matrix. Runtime UI evidence and an independent evaluator are both required; injected faults are labeled separately from native target behavior.

## Target pin

Use the original Parasoft ParaBank repository at immutable commit ee82474be5f58bea3ddc8be0fd831072b00201cb. The moving selenium-demo-baseline tag is not the selected pin. The upstream build path uses Maven and deploys the resulting WAR to Tomcat; upstream recommends Java 17 and Tomcat 10.1. A native browser smoke, a saved-login Codex lifecycle diagnostic, a V2 scripted offline-backend loopback replay, and a V9 headed same-session real-person takeover are recorded. The Codex run produced a verified trace and temporary DRAFT only; the release verifier keeps V1 `NOT_RUN` until committed approval, digest, provenance, and target evidence are present. V9 is PASS through fingerprint-bound safe evidence. See PROGRESS.md.

Pinned native DOM differences are mapped without changing the target: the overview heading is `Balance*` (footnote marker), and the Available Balance value row is labeled `Available:`. The profile consumes those exact native labels while exposing the stable business field `PROFILE_AVAILABLE_BALANCE`.

Sources checked 2026-09-21: [official pinned commit](https://github.com/parasoft/parabank/commit/ee82474be5f58bea3ddc8be0fd831072b00201cb), [official repository/build instructions](https://github.com/parasoft/parabank), [official image tags](https://hub.docker.com/r/parasoft/parabank/tags). ParaBank remains the real target; the custom application does not replace it.

## Implemented module and deviation index

The initial mapping above is now backed by these concrete boundaries:

| Boundary | Current implementation | Release caveat |
|---|---|---|
| Application composition | src/cua/application/config.py, service.py, oracle.py | cua serve composes one service from server-owned environment metadata; provider modes are disabled, saved-login Codex, and OpenAI. |
| Decision backends | src/cua/llm/decisions.py | Codex uses `codex -a never exec` with structured output, an empty read-only workspace, saved CLI login, and scrubbed API-key/access-token environment variables; replay has no backend dependency. |
| HTTP and operator page | src/cua/web/app.py | Static UI supports prepare, discovery submission, capability inspect/validate/approve, approved replay, status polling, explicit result reads, and intervention claim/resume/abort. Browser-backed V9 takeover evidence is recorded; full browser end-to-end V12 evidence remains pending. |
| CLI | src/cua/cli.py, src/cua/__main__.py | Client commands use the persistent HTTP service; only cua serve imports the composition layer. Account and goal prompts are memory-only process inputs. |
| Native testbed and oracle | testbed/, testbed/fixtures/ | Testbed is isolated from runtime imports. Generated manifests and target processes are local; no live capability artifact is checked in. |
| Safe release evidence | artifacts/, evidence/, scripts/verify_release.py | `artifacts/index.json` contains a temporary Codex DRAFT; `evidence/index.json` records its rejected V1 diagnostic, fingerprint-bound V2 scripted native-loopback facts, passing V9 headed same-session human-takeover evidence, and a partial V11 clean-checkout diagnostic. |

The native profile records two pinned DOM facts discovered from the upstream page: overview uses Balance*, and detail uses the exact Available: label inside #accountDetails > table. These are profile mappings, not target changes. The supported runtime contract is one Savings-balance capability, with typed CLICK/EXTRACT/ASSERT/WAIT/VERIFY behavior and bounded recovery.

Discovery is profile-guided and bounded: a decision backend may choose only typed actions exposed by the current safe observation, while policy, target resolution, and the actor-owned gateway authorize every browser effect. Capability formation also contains human-authored blueprint metadata, including the reviewer-added overview anchor and declared membership, extraction, and final verification checkpoints. The saved-login Codex lifecycle produced two decisions and one verified observed event, then replayed the temporary capability without provider calls. Its DRAFT-only provenance does not establish release-qualified V1. The V2 loopback record uses an injected scripted backend and has matching current-source evidence.

Qualification compares a composite runtime fingerprint covering executable Python source, `pyproject.toml`, `uv.lock`, and the Python implementation major/minor marker, plus separate parser/condition/profile hashes. Browser version and target revision remain separate qualification pins. The release verification fingerprint additionally covers testbed Python sources; dependency or native qualification changes therefore require a fresh release check.

The verification entrypoint preserves NOT_RUN for every unsupported V1–V12 case until a case-specific fingerprinted evidence manifest exists. The current evidence promotes V2 and V9. V1's saved-login record is intentionally rejected because the artifact remains DRAFT with an uncommitted approval sidecar. V11's clean-checkout record is a partial diagnostic and remains NOT_RUN until native no-model replay runs from a clean checkout with an approved real artifact. Offline contract test results appear separately in its JSON output and do not create a live artifact or alter unrelated statuses.
