# Capability artifacts

`index.json` is the release-safe artifact index. It is intentionally empty because this checkout has not run live provider discovery and therefore has no real trace-derived, validated, approved capability to ship. Generated private material belongs under `artifacts/private/`, which is ignored.

A future entry must include the immutable capability name/version/digest, qualification reference, target/profile revisions, and evidence index without including goals, credentials, account values, balances, or model payloads.
