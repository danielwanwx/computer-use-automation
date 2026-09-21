# Safe evidence

`index.json` is the release-safe evidence index and is intentionally empty. Runtime evidence is written beneath the configured local evidence root as typed metadata, JSONL safe events, and value-free structural snapshots. Private/generated evidence belongs under `evidence/private/`, which is ignored.

Do not copy credentials, account IDs, balances, goals, raw DOM, URLs with query values, tokens, HAR files, videos, or model payloads into this directory. A future verified entry must identify whether evidence came from the native target or an injected test harness.
