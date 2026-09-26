# Computer-use automation

An LLM works out how to complete a task in a legacy banking UI that has no API. The successful run is compiled into a typed, versioned capability, and that capability replays deterministically with no model in the loop. When replay meets something it cannot classify, it pauses and hands the same live browser session to a human, then resumes.

- **Target:** [Parasoft ParaBank](https://github.com/parasoft/parabank), pinned to commit `ee82474b`, built and run locally on loopback. It is a server-rendered JSP banking app with table layouts and no test IDs, which makes it a fair stand-in for a legacy back-office screen.
- **Capability:** `get_savings_balance(account_id) -> {available_balance, currency}`, read-only.
- **Design write-up:** [REPORT.md](REPORT.md). **Run evidence:** [evidence/RUN_LOG.md](evidence/RUN_LOG.md).

## Where each requirement is shown

| Brief requirement | Where to look |
|---|---|
| Real LLM-driven discovery on a live UI | [`evidence/RUN_LOG.md` §1](evidence/RUN_LOG.md). It shows what the model saw at each step, what it chose, and why (Claude Code as the decision backend, `claude-opus-5-5`). The raw records are in `evidence/live_run/`. |
| Structured, versioned artifact | [`artifacts/get_savings_balance-1.0.0.json`](artifacts/get_savings_balance-1.0.0.json) (canonical JSON, SHA-256 digest in [`artifacts/index.json`](artifacts/index.json)) |
| Deterministic replay, no model | 10 replays across two customers the model never saw, with a deposit in between; 0 model calls |
| Business outcomes vs. failures | Six replays in one reused session: success, `ACCOUNT_NOT_FOUND` ×2 (business outcome), wrong account type, malformed input (hard failures with the failing step), success again |
| Human escalation on the live session | [`evidence/handoff_run/`](evidence/handoff_run/handoff_summary.json): an unexpected dialog → `WAITING_FOR_HUMAN` → claim → dismissal on the same page → resume → `SUCCESS`. There is also an [earlier person-operated rehearsal](evidence/handoff_person_rehearsal.json). |
| Guardrails and redaction | Policy checked at every dispatch. The model never sees account numbers, balances, or names. Evidence holds only typed events and value-free structural snapshots. See [REPORT.md § Safety](REPORT.md#safety). |

## Setup

Requirements: [uv](https://docs.astral.sh/uv/), Google Chrome, and a JDK 17+ to build and run the local ParaBank. Tested on macOS (arm64) with JDK 22.

```sh
uv python install 3.12
uv sync --locked
.venv/bin/python -m pytest -q                 # offline suite: no browser, no target, no key
```

Start the target. The first `prepare` downloads the pinned ParaBank source plus Maven and Tomcat archives, verifies their SHA-512 checksums, and builds the WAR:

```sh
.venv/bin/python -m testbed.parabank prepare
.venv/bin/python -m testbed.parabank start
.venv/bin/python -m testbed.parabank health    # "ParaBank healthy on loopback"
```

### Choosing the model: no API key required

Only discovery uses a model. Validation, replay, and handoff never do. Discovery needs **one** of the following, and `--provider auto` (the default) picks the first one available:

| Mode | What you need | Notes |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | pinned snapshot `gpt-5.5-2026-04-23`; used first when the key is set |
| `claude-code` | Claude Code installed and signed in (`claude`) | uses your existing login; how the committed evidence was produced |
| `codex` | Codex CLI installed and signed in (`codex`) | uses your ChatGPT login; falls back to `gpt-5.5` if the account does not offer the CLI's default model |
| `cursor` | Cursor Agent CLI installed and signed in (`cursor-agent`) | same contract; not exercised on the author's machine |

Whichever backend runs, it receives only the value-free decision request and returns one schema-checked decision. Each local agent CLI runs in an empty temporary directory with no tools and no project rules, hooks, or MCP servers; it sees only its own login, never another provider's key or the fixture credentials. Force a mode with `--provider` (scripts) or `CUA_PROVIDER` (service).

The demo scripts create throwaway synthetic customers with random passwords on each run. No credential is ever written to disk.

## Demo path

**1. Run the agent on a goal, then replay the result.** This command performs the whole lifecycle in one pass: it seeds four synthetic customers, runs LLM discovery, compiles the artifact, validates it on another customer against an independent oracle, approves it, and replays it with the model removed. Replay covers 10 success runs plus the error cases.

```sh
CUA_LIVE=1 .venv/bin/python scripts/discover_and_replay.py --no-export
```

It prints which backend it chose, the model's decisions, the compiled step list, and every replay result. Without `--no-export`, the same command writes `artifacts/` and `evidence/`; that is how the committed evidence was produced (`--provider auto` with no key set, which resolved to Claude Code).

**2. Replay the committed artifact with no model and no key.**

```sh
unset OPENAI_API_KEY
CUA_NATIVE_REVIEW=1 .venv/bin/python scripts/review_native_bundle.py
```

This loads `artifacts/get_savings_balance-1.0.0.json`, checks that it is canonical and value-free, validates it on one customer, approves it in a temporary registry, and replays it for two others. Every output is checked against ParaBank's REST API. Any model call fails the run.

**3. Human handoff on the live session.**

```sh
.venv/bin/python scripts/demo_handoff.py                            # scripted operator, reproducible
.venv/bin/python scripts/demo_handoff.py --operator person --headed # you dismiss the dialog yourself
```

**4. Agent-facing interface.** This drives the same lifecycle through the HTTP service and CLI (`cua serve`, `cua discover`, `cua capabilities validate|approve`, `cua replay`, `cua run result`):

```sh
scripts/demo_service.sh
```

The operator page at `http://127.0.0.1:8765/` exposes the same sessions, capabilities, runs, and intervention Claim/Resume/Abort.

**Without live services:** `.venv/bin/python -m pytest -q` runs ~300 offline tests covering the replay engine, conditions, parsers, registry, policy, evidence redaction, handoff protocol, HTTP and CLI. They use in-process fakes. Four more native tests run against the real target with `CUA_PARABANK_LIVE_TEST=1`.

## Repository map

```text
src/cua/
  surface/       Playwright adapter: page -> typed Observation / NormalizedView (no raw DOM leaves it)
  profiles/      per-app profile: routes, readiness rules, semantic locators  <- the per-surface seam
  llm/           decision backends: OpenAI API, or a signed-in Claude Code / Codex / Cursor CLI; strict JSON decisions
  discovery/     goal binding, observe -> decide -> act loop, reviewer blueprint
  compiler/      verified trace + blueprint -> CapabilityBundle
  registry/      canonical digest, DRAFT -> VALIDATED -> APPROVED, runtime fingerprint pins
  replay/        model-free executor, pre/postconditions, recovery, handoff seam
  execution/     the single gateway every browser effect passes through (policy, re-resolve, verify)
  conditions/    tri-state condition evaluator, USD parser
  verification/  completion verifier (identity, account, type, currency, parse)
  sessions/      isolated browser contexts, session actor (owner + epoch fencing)
  handoff/       intervention lifecycle: request, claim, resume, abort, reconcile
  policy/        origin/route/operation allowlist, runtime risk classification
  evidence/      typed safe events + structural snapshots (SQLite + JSONL)
  application/, web/, cli.py   service composition, loopback HTTP API + operator page, CLI
testbed/         pinned ParaBank build, synthetic seeding, independent REST oracle (never imported by src/)
scripts/         discover_and_replay, review_native_bundle, demo_handoff, demo_service, evidence tools
```

## Scope

One read-only capability on one surface. The model's action space during discovery is deliberately narrow. Multi-tenant overlays and a desktop adapter are designed but not built. [REPORT.md § Cuts](REPORT.md#cuts) lists what was left out and what comes next.
