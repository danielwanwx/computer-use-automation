# Computer-use automation

This repository contains a local, reviewable UI capability runtime for the single capability `get_savings_balance(account_id)`. The target is the original Parasoft ParaBank application at immutable commit `ee82474be5f58bea3ddc8be0fd831072b00201cb`; the runtime does not replace or modify the target application. The current checkout has a deterministic kernel, native target testbed, safe application boundary, CLI, and release checks. Live provider discovery, a genuinely qualified native artifact, and real-person handoff remain explicitly unrun.

## Setup

Use Python 3.12 through uv and keep the lockfile unchanged:

```sh
cd /Users/danielwan/Projects/computer-use-automation
uv python install 3.12
uv sync --locked
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -p no:cacheprovider -q
```

The runtime uses `playwright==1.60.0` and the configured `chrome` channel by default. Install a supported local Chrome or set the server-owned browser channel to the separately qualified `msedge` option. The testbed may download only its pinned source and toolchain archives after their SHA-512 checks pass.

The verification entrypoint runs the offline checks and prints the complete acceptance matrix:

```sh
.venv/bin/python scripts/verify_release.py
```

It prints JSON with V1–V12 statuses, source and runtime fingerprints, exact check commands, fixture paths, evidence paths, and the distinction between offline checks and native/manual cases. The current evidence records V2 and V11 as `PASS`; `NOT_RUN` is retained for every unsupported live or manual case.

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
export CUA_PROVIDER_ENABLED=false
```

The validation account value must be a savings account belonging to the server-only validation principal and should be read from the local seed manifest without echoing it. Do not put credential or account values in `CUA_PRINCIPALS_JSON`, committed files, URLs, or shell history. To enable live discovery, configure a server-owned provider model and credential explicitly; no provider credential is required for the offline test path and none was used for this release state.

Start the service and open the page at `http://127.0.0.1:8765/`:

```sh
.venv/bin/cua serve
```

If `CUA_OPERATOR_TOKEN` is absent, `cua serve` generates one and prints it once to stderr. Enter that token in the page's in-memory token field. The page can prepare a session, start discovery when a provider is configured, refresh and inspect draft revisions, validate and approve a revision, replay an approved revision with a new account binding, poll safe status, and explicitly request the protected result. The page does not use `localStorage`.

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

No live discovery is claimed without a provider. The offline path exercises typed traces, registry validation, model-free replay, policy/evidence boundaries, the independent oracle transport, the web contract, and CLI HTTP behavior:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -p no:cacheprovider -q
```

Native tests are opt-in and require the prepared target, seeded credentials, and a supported browser. The committed `artifacts/index.json` remains empty because no live provider discovery has produced a releasable capability. `evidence/index.json` records the case-specific V2 native loopback replay and V11 clean-checkout evidence; neither entry is a live provider artifact or a real-person takeover.

## Status and scope

The seven-heading release report is in [REPORT.md](REPORT.md). The module map and deviations are in [DESIGN.md](DESIGN.md). The machine-readable acceptance report is produced by `scripts/verify_release.py`. V2 and V11 have matching, fingerprinted release evidence; V1, V3–V10, and V12 remain `NOT_RUN`, including V9 for the required real-person takeover. The report separately records offline contract-check results and never promotes them to native/live acceptance. There is no public repository publication or email delivery from this checkout.
