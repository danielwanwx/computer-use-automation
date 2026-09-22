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

Use the original Parasoft ParaBank repository at immutable commit ee82474be5f58bea3ddc8be0fd831072b00201cb. The moving selenium-demo-baseline tag is not the selected pin. The upstream build path uses Maven and deploys the resulting WAR to Tomcat; upstream recommends Java 17 and Tomcat 10.1. A native browser smoke is reported passing on this host; end-to-end discovery, artifact qualification, and approved replay remain unrun. See PROGRESS.md.

Pinned native DOM differences are mapped without changing the target: the overview heading is `Balance*` (footnote marker), and the Available Balance value row is labeled `Available:`. The profile consumes those exact native labels while exposing the stable business field `PROFILE_AVAILABLE_BALANCE`.

Sources checked 2026-09-21: [official pinned commit](https://github.com/parasoft/parabank/commit/ee82474be5f58bea3ddc8be0fd831072b00201cb), [official repository/build instructions](https://github.com/parasoft/parabank), [official image tags](https://hub.docker.com/r/parasoft/parabank/tags). ParaBank remains the real target; the custom application does not replace it.

## Implemented module and deviation index

The initial mapping above is now backed by these concrete boundaries:

| Boundary | Current implementation | Release caveat |
|---|---|---|
| Application composition | src/cua/application/config.py, service.py, oracle.py | cua serve composes one service from server-owned environment metadata; live provider discovery remains opt-in and was not used. |
| HTTP and operator page | src/cua/web/app.py | Static UI supports prepare, discovery submission, capability inspect/validate/approve, approved replay, status polling, and explicit result reads. Handoff controls are not wired to the application service. |
| CLI | src/cua/cli.py, src/cua/__main__.py | Client commands use the persistent HTTP service; only cua serve imports the composition layer. Account and goal prompts are memory-only process inputs. |
| Native testbed and oracle | testbed/, testbed/fixtures/ | Testbed is isolated from runtime imports. Generated manifests and target processes are local; no live capability artifact is checked in. |
| Safe release evidence | artifacts/, evidence/, scripts/verify_release.py | The indexes are intentionally empty until a real trace, qualification, and approved replay produce evidence. |

The native profile records two pinned DOM facts discovered from the upstream page: overview uses Balance*, and detail uses the exact Available: label inside #accountDetails > table. These are profile mappings, not target changes. The supported runtime contract is one Savings-balance capability, with typed CLICK/EXTRACT/ASSERT/WAIT/VERIFY behavior and bounded recovery.

Qualification compares a composite runtime fingerprint covering executable Python source, `pyproject.toml`, `uv.lock`, and the Python implementation major/minor marker, plus separate parser/condition/profile hashes. Browser version and target revision remain separate qualification pins. The release verification fingerprint additionally covers testbed Python sources; dependency or native qualification changes therefore require a fresh release check.

The verification entrypoint preserves NOT_RUN for every unsupported V1–V12 case until a case-specific fingerprinted evidence manifest exists. The current evidence promotes only V2 and V11; offline contract test results appear separately in its JSON output and do not create a live artifact or alter unrelated statuses.
