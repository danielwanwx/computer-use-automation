# Capability artifacts

`get_savings_balance-1.0.0.json` is the capability compiled from the live discovery run recorded in [`../evidence/live_lifecycle.json`](../evidence/live_lifecycle.json). It is canonical JSON; its SHA-256 digest is its identity and is listed in `index.json` with the target, runtime fingerprint, and qualification it was validated against. [REPORT.md § Artifact schema](../REPORT.md#artifact-schema) explains each section.

The committed file is a **DRAFT**. Validation and approval are bound to a runtime fingerprint, a browser version, and a target revision, so they are performed where the artifact runs:

```sh
CUA_NATIVE_REVIEW=1 .venv/bin/python scripts/review_native_bundle.py   # validate, approve, replay; no model
```

The artifact contains no account numbers, balances, names, or credentials. Inputs are bound per invocation.
