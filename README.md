# Computer-use automation

This repository contains a local, reviewable UI capability runtime for the single capability `get_savings_balance(account_id)`. The target is the original Parasoft ParaBank application at immutable commit `ee82474be5f58bea3ddc8be0fd831072b00201cb`; the runtime does not replace or modify the target application. The current checkout has a deterministic kernel, native target testbed, safe application boundary, CLI, and release checks. An opt-in Codex saved-login lifecycle produced a value-safe temporary DRAFT and native replay diagnostics; V1 remains `NOT_RUN` because release approval and evidence promotion are incomplete. V9 has a matching current-source headed same-session evidence record from a real-person rehearsal.

## Setup

Use Python 3.12 through uv and keep the lockfile unchanged:

```sh
cd <project-root>
uv python install 3.12
uv sync --locked
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -p no:cacheprovider -q
```

The runtime uses `playwright==1.60.0` and the configured `chrome` channel by default. Install a supported local Chrome or set the server-owned browser channel to the separately qualified `msedge` option. The testbed may download only its pinned source and toolchain archives after their SHA-512 checks pass.

The verification entrypoint runs the offline checks and prints the complete acceptance matrix:

```sh
.venv/bin/python scripts/verify_release.py
```

It prints JSON with V1–V12 statuses, source and runtime fingerprints, exact check commands, fixture paths, evidence paths, and the distinction between offline checks and native/manual cases. V1 has a saved-login Codex lifecycle diagnostic whose temporary approval is intentionally rejected for release acceptance. V2 has a matching current-source native loopback record using an injected offline scripted backend. V9 has a matching current-source headed same-session evidence record from a real-person rehearsal. V11 remains `NOT_RUN`: its clean-checkout record is a partial diagnostic until native no-model replay runs from a clean checkout with an approved real artifact.

## Native ParaBank testbed

The testbed is optional for offline work. It is the only code allowed to seed or reset the target. Run from the project root:

```sh
.venv/bin/python -m testbed.parabank prepare
.venv/bin/python -m testbed.parabank start
.venv/bin/python -m testbed.parabank health
```

Before seeding, set the eight synthetic credential variables. Values are used only for the loopback target and are never written to manifests or logs:

```sh
export PARABANK_DEMO_ALPHA_USERNAME=cua_synthetic_alpha
export PARABANK_DEMO_ALPHA_PASSWORD='set-a-local-secret'
export PARABANK_DEMO_BETA_USERNAME=cua_synthetic_beta
export PARABANK_DEMO_BETA_PASSWORD='set-a-local-secret'
export PARABANK_DEMO_GAMMA_USERNAME=cua_synthetic_gamma
export PARABANK_DEMO_GAMMA_PASSWORD='set-a-local-secret'
export PARABANK_DEMO_DELTA_USERNAME=cua_synthetic_delta
export PARABANK_DEMO_DELTA_PASSWORD='set-a-local-secret'
.venv/bin/python -m testbed.parabank seed
```

Use `reset` to rebuild the upstream demo database and recreate the four synthetic customers. Use `stop` when finished:

```sh
.venv/bin/python -m testbed.parabank reset
.venv/bin/python -m testbed.parabank stop
```

The generated deployment and seed manifests are local files under `testbed/.cache/` and are ignored by Git. `testbed/fixtures/session_manifest.json` contains only environment variable names and synthetic fixture metadata. Runtime sessions use the same target lock path and cannot overlap a reset.

## Server configuration and operation page

`cua serve` composes one persistent `ApplicationService`, binds only to loopback, serves the operation page, and drains the service on shutdown. Credentials stay as environment references. The provider is disabled by default.

Configure two ordinary/validation principals with metadata only; the values for `username_env` and `password_env` name environment variables that the session manager reads when a session is prepared:

```sh
export CUA_PRINCIPALS_JSON='[{"alias":"synthetic_alpha","username_env":"PARABANK_DEMO_ALPHA_USERNAME","password_env":"PARABANK_DEMO_ALPHA_PASSWORD","expected_display_name":"Synthetic Alpha"},{"alias":"synthetic_beta","username_env":"PARABANK_DEMO_BETA_USERNAME","password_env":"PARABANK_DEMO_BETA_PASSWORD","expected_display_name":"Synthetic Beta"}]'
export CUA_VALIDATION_FIXTURES_JSON='{"synthetic_beta":"CUA_VALIDATION_ACCOUNT_ID"}'
export CUA_VALIDATION_ACCOUNT_ID='set-from-the-local-seed-manifest'
export CUA_VALIDATION_ORACLE_COMMAND_JSON='[".venv/bin/python","-m","testbed.oracle"]'
export CUA_OPERATOR_TOKEN='choose-a-local-token'
export CUA_PROVIDER=disabled
```

The validation account value must be a savings account belonging to the server-only validation principal and should be read from the local seed manifest without echoing it. Do not put credential or account values in `CUA_PRINCIPALS_JSON`, committed files, URLs, or shell history. Provider selection is server-owned:

```sh
export CUA_PROVIDER=disabled       # default; no discovery backend
# or:
export CUA_PROVIDER=codex          # uses the saved local Codex/ChatGPT login
# omit CUA_PROVIDER_MODEL to use the saved-login CLI default
# or:
export CUA_PROVIDER=openai         # requires the official API credential below
export CUA_PROVIDER_MODEL=<server-approved-model-id>
export OPENAI_API_KEY=<secret-in-the-process-environment>
```

`CUA_PROVIDER_ENABLED=false` remains accepted as a legacy alias for `CUA_PROVIDER=disabled`; enabling that legacy flag selects OpenAI and still requires a model. `CUA_CODEX_EXECUTABLE_JSON` can override the server-owned Codex executable argv. The Codex backend invokes the installed CLI with the saved login, a temporary empty workspace, read-only sandboxing, ignored user/project rules, and no `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, or `OPENAI_FEDERATION_RULE_ID` passed to the child. It does not read or copy the saved auth token.

The exact opt-in saved-login discovery command used for the native diagnostic is:

```sh
CUA_NATIVE_CODEX_LIVE=1 .venv/bin/python scripts/run_native_codex.py --json
```

It requires a healthy pinned loopback target and a pre-existing local `codex` login; it does not require or accept an API key. The installed CLI invocation uses `codex -a never exec ... --skip-git-repo-check -` internally. The discovery loop is constrained by the ParaBank profile, typed action space, policy, and current safe observation; it cannot invent arbitrary browser operations. The capability compiler also requires human-authored blueprint metadata, including the reviewer-added overview anchor and declared membership, extraction, and final verification checkpoints. The saved-login run produced two discovery decisions, a verified trace, a temporary DRAFT digest, and ten successful no-provider replay runs across two synthetic customers. Because the approval sidecar was temporary, this is a diagnostic and does not promote V1.

Start the service and open the page at `http://127.0.0.1:8765/`:

```sh
.venv/bin/cua serve
```

If `CUA_OPERATOR_TOKEN` is absent, `cua serve` generates one and prints it once to stderr. Enter that token in the page's in-memory token field. The page can prepare a session, start discovery when a provider is configured, refresh and inspect draft revisions, validate and approve a revision, replay an approved revision with a new account binding, poll safe status, and explicitly request the protected result. The page does not use `localStorage`.

For the headed-browser takeover rehearsal, set `CUA_BROWSER_HEADLESS=false` before `cua serve`. Prepare a session and start a run with an approved capability. When the run displays a safe blocker and enters `WAITING_FOR_HUMAN`, inspect the same headed page, use **Claim**, perform only the requested human action, and use **Resume**. Resume rechecks the original principal, authentication generation, page, target origin, and current readiness before replay continues; a wrong-principal login or changed page is expected to keep the run paused or abort it. The recorded V9 rehearsal used the same headed browser session and resumed after route verification. Its release-safe entry retains only a completion attestation and structural run facts, never credentials, tokens, account values, or browser contents.

## CLI

The CLI is an HTTP client for the same persistent service. It does not construct an `ApplicationService` for client commands. Set `CUA_OPERATOR_URL` when the service is not at its default and set `CUA_OPERATOR_TOKEN`; if the token is absent, the CLI asks with a hidden prompt.

```sh
export CUA_OPERATOR_URL=http://127.0.0.1:8765
.venv/bin/cua sessions list
.venv/bin/cua sessions prepare --principal synthetic_alpha
.venv/bin/cua discover --session-id SESSION_ID --goal 'Get the available balance' --account-id ACCOUNT_ID
.venv/bin/cua capabilities list
.venv/bin/cua capabilities inspect --name get_savings_balance --version 1.0.0 --digest DIGEST
.venv/bin/cua capabilities validate --name get_savings_balance --version 1.0.0 --digest DIGEST
.venv/bin/cua capabilities approve --name get_savings_balance --version 1.0.0 --digest DIGEST
.venv/bin/cua replay --session-id SESSION_ID --name get_savings_balance --version 1.0.0 --digest DIGEST --account-id ACCOUNT_ID
.venv/bin/cua run status RUN_ID
.venv/bin/cua run result RUN_ID --session-id SESSION_ID
```

JSON is printed by default. `run result` is an explicit command because results can contain authorized business outputs. Account IDs and goals are protected request values; prefer omitting `--account-id` (and `--goal` in an interactive terminal) to use the hidden prompt and avoid shell history. Flags remain available for controlled local tests.

## Offline path and tests

The offline path exercises typed traces, registry validation, model-free replay, policy/evidence boundaries, the independent oracle transport, the web contract, and CLI HTTP behavior:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -p no:cacheprovider -q
```

Native tests are opt-in and require the prepared target, seeded credentials, and a supported browser. `artifacts/index.json` contains the value-safe DRAFT exported by the saved-login Codex diagnostic; its temporary approval sidecar is not committed, so it is not an approved release capability. `evidence/index.json` records the V1 diagnostic, current-source V2 native loopback replay, V9 headed same-session real-person takeover, and partial V11 clean-checkout diagnostic. V2 and V9 are passing acceptance cases.

The shortest no-key replay check is the offline model-free suite:

```sh
unset OPENAI_API_KEY
export CUA_PROVIDER=disabled
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -p no:cacheprovider tests/test_replay.py
```

This does not create an approved artifact or prove native V3/V11. A native no-key replay requires a separately approved real artifact and a clean-checkout run; that evidence is still missing.
The no-model native reviewer path is separate from discovery and never imports a model backend:

```sh
CUA_NATIVE_REVIEW=1 .venv/bin/python scripts/review_native_bundle.py \
  --artifact artifacts/get_savings_balance-1.0.0.json
```

It validates and replays the value-safe DRAFT through a fresh temporary registry using the independent oracle. It is not V11 evidence until the artifact is approved, the checkout is clean, and the native no-model replay is recorded from that clean checkout. The shortest live-discovery attempt through the server is `CUA_PROVIDER=codex .venv/bin/cua serve`, after a saved local Codex login; OpenAI mode instead uses `CUA_PROVIDER=openai` and `OPENAI_API_KEY`. Neither command alone promotes V1.

## Status and scope

The seven-heading release report is in [REPORT.md](REPORT.md). The module map and deviations are in [DESIGN.md](DESIGN.md). The machine-readable acceptance report is produced by `scripts/verify_release.py`. V2 and V9 have matching current-source evidence for their required native loopback and real-person takeover cases. V1 remains `NOT_RUN` because its saved-login result is only a temporary DRAFT diagnostic. V11 remains `NOT_RUN` because its clean-checkout result is diagnostic only. V3–V8, V10, and V12 remain `NOT_RUN`. The report separately records offline contract-check results and never promotes them to native/live acceptance. Repository publication and visibility are external distribution state and do not count as V1–V12 acceptance evidence.
