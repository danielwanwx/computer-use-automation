#!/usr/bin/env bash
# Agent-facing path: the same lifecycle driven through `cua serve` and the `cua` CLI.
#
#   discover (LLM) -> list -> validate (oracle) -> approve -> replay by name
#   with a typed account_id -> read result -> replay an unknown account.
#
# Needs: a healthy local ParaBank (`python -m testbed.parabank start`) and one
# decision backend: OPENAI_API_KEY, or a signed-in Claude Code / Codex / Cursor CLI
# (CUA_PROVIDER=auto picks one). Creates throwaway synthetic customers; reseeds the target.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
CUA=.venv/bin/cua

json() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
secret() { "$PY" -c 'import secrets; print(secrets.token_hex(8))'; }

for who in ALPHA BETA GAMMA DELTA; do
  export "PARABANK_DEMO_${who}_USERNAME=cua_$(echo "$who" | tr 'A-Z' 'a-z')_$("$PY" -c 'import secrets; print(secrets.token_hex(2))')"
  export "PARABANK_DEMO_${who}_PASSWORD=$(secret)"
done
"$PY" -m testbed.parabank seed >/dev/null
account() {  # account <alias> <TYPE>: read from the local, git-ignored seed manifest
  "$PY" -c "import json; d=json.load(open('testbed/.cache/seed_manifest.json')); print(next(a['account_id'] for p in d['principals'] if p['alias']=='$1' for a in p['accounts'] if a['type']=='$2'))"
}

principal() { printf '{"alias":"%s","username_env":"PARABANK_DEMO_%s_USERNAME","password_env":"PARABANK_DEMO_%s_PASSWORD","expected_display_name":"Synthetic %s"}' "$1" "$2" "$2" "$3"; }
export CUA_PRINCIPALS_JSON="[$(principal alpha ALPHA Alpha),$(principal beta BETA Beta),$(principal gamma GAMMA Gamma)]"
export CUA_VALIDATION_ACCOUNT_ID="$(account beta SAVINGS)"
export CUA_VALIDATION_FIXTURES_JSON='{"beta":"CUA_VALIDATION_ACCOUNT_ID"}'
export CUA_VALIDATION_ORACLE_COMMAND_JSON='[".venv/bin/python","-m","testbed.oracle"]'
export CUA_OPERATOR_TOKEN="$(secret)"
export CUA_PROVIDER="${CUA_PROVIDER:-auto}"
export CUA_DATA_ROOT="$(mktemp -d)/cua"

"$CUA" serve >"$CUA_DATA_ROOT.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 50); do curl -sf -o /dev/null http://127.0.0.1:8765/ && break; sleep 0.2; done
echo "operator page: http://127.0.0.1:8765/  (token: \$CUA_OPERATOR_TOKEN)"

wait_run() {
  for _ in $(seq 180); do
    status=$("$CUA" run status "$1")
    state=$(echo "$status" | json 'd["state"]')
    case $state in SUCCESS|FAILURE|BUSINESS_OUTCOME|ABORTED|SESSION_LOST)
      echo "  run $1: $state ($(echo "$status" | json 'd["outcome_code"]'))"; return;; esac
    sleep 1
  done
  echo "  run $1 did not finish"; exit 1
}

echo "1. discover with the LLM (customer alpha)"
alpha=$("$CUA" sessions prepare --principal alpha | json 'd["session_id"]')
run=$("$CUA" discover --session-id "$alpha" --goal "What is the available balance of my savings account?" \
  --account-id "$(account alpha SAVINGS)" | json 'd["run_id"]')
wait_run "$run"

echo "2. the recorded capability"
"$CUA" capabilities list | json 'json.dumps(d, indent=2)'
digest=$("$CUA" capabilities list | json 'd[0]["reference"]["digest"]')

echo "3. validate on another customer against the independent oracle, then approve"
run=$("$CUA" capabilities validate --name get_savings_balance --version 1.0.0 --digest "$digest" | json 'd["run_id"]')
wait_run "$run"
"$CUA" capabilities approve --name get_savings_balance --version 1.0.0 --digest "$digest" >/dev/null && echo "  approved"

echo "4. replay by name, no model, for customer gamma"
gamma=$("$CUA" sessions prepare --principal gamma | json 'd["session_id"]')
run=$("$CUA" replay --session-id "$gamma" --name get_savings_balance --version 1.0.0 --digest "$digest" \
  --account-id "$(account gamma SAVINGS)" | json 'd["run_id"]')
wait_run "$run"
"$CUA" run result "$run" --session-id "$gamma" | json 'json.dumps({"status": d["status"], "outputs": d.get("outputs")})'

echo "5. same session, an account this customer does not have"
run=$("$CUA" replay --session-id "$gamma" --name get_savings_balance --version 1.0.0 --digest "$digest" \
  --account-id 99999999 | json 'd["run_id"]')
wait_run "$run"
"$CUA" run result "$run" --session-id "$gamma" | json 'json.dumps({"status": d["status"], "code": d.get("code")})'
