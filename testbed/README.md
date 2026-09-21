# Native ParaBank testbed

Run commands from the project root with Python 3.9 or newer:

```sh
python3 -m testbed.parabank prepare
python3 -m testbed.parabank start
python3 -m testbed.parabank health
```

`prepare` fetches immutable upstream commit `ee82474be5f58bea3ddc8be0fd831072b00201cb`, verifies Maven 3.9.9 and Tomcat 10.1.60 archives against their pinned SHA-512 digests, builds the original WAR, and applies only loopback listener configuration. Generated sources, dependencies, deployment metadata, and seed manifests live in ignored `testbed/.cache/`.

Before running `seed` or `reset`, set the eight synthetic credential variables listed in `fixtures/session_manifest.json`. The following creates throwaway values in the current shell without printing generated passwords:

```sh
export PARABANK_DEMO_ALPHA_USERNAME=cua_synthetic_alpha
export PARABANK_DEMO_ALPHA_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
export PARABANK_DEMO_BETA_USERNAME=cua_synthetic_beta
export PARABANK_DEMO_BETA_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
export PARABANK_DEMO_GAMMA_USERNAME=cua_synthetic_gamma
export PARABANK_DEMO_GAMMA_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
export PARABANK_DEMO_DELTA_USERNAME=cua_synthetic_delta
export PARABANK_DEMO_DELTA_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
```

`reset` recreates the pinned upstream demo database and then creates four synthetic customers; `seed` performs the same deterministic operation. Credential values are posted only to the loopback target and are omitted from the generated manifest. Both commands take an exclusive OS lock at `testbed/.cache/target-use.lock`; runtime code must hold a shared lock at this path for every active session/run, so reset fails while those exist and new sessions cannot start mid-reset. `health` reports reachability only; a successful `prepare` writes the build pin and WAR digest to `testbed/.cache/deployment_manifest.json`.

`testbed.seed.deposit_savings_for_test(account_id, amount, manifest_path=...)` is reserved for local lifecycle tests. It permits only a savings account listed in the generated synthetic manifest, uses the pinned loopback target, verifies the backend effect, and takes the same exclusive target lock; close all runtime sessions before calling it.

```sh
python3 -m testbed.parabank seed
python3 -m testbed.parabank reset
```

The independent account-balance oracle is in `evaluator.py` and must remain testbed-only. It uses the pinned ParaBank UI rule `available = max(ledger_balance, 0)` and requires result money as a canonical, finite decimal string with exactly two fractional digits.
