import asyncio
import io
import json
import sys

from pydantic import SecretStr

from cua.application import SubprocessValidationOracle, ValidationFixture
from cua.execution import InvocationResult, InvocationStatus
import testbed.oracle as oracle_entrypoint


def _payload(balance="41.23"):
    return {
        "result": {
            "status": "SUCCESS",
            "outputs": {"available_balance": "41.23", "currency": "USD"},
        },
        "fixture": {"principal_alias": "synthetic_beta", "account_id": "7048162359"},
        "backend": {"balance": balance},
    }


def _run_main(monkeypatch, payload, account):
    monkeypatch.setattr(
        oracle_entrypoint,
        "_read_backend_account",
        lambda _: {"id": account, "type": "SAVINGS", "balance": payload["backend"]["balance"]},
    )
    stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stream)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    assert oracle_entrypoint.main() == 0
    return json.loads(output.getvalue())


def test_testbed_entrypoint_returns_stable_match_or_mismatch(monkeypatch):
    assert _run_main(monkeypatch, _payload(), "7048162359") == {
        "passed": True,
        "code": "ORACLE_PASS",
    }
    assert _run_main(monkeypatch, _payload(balance="1.00"), "7048162359") == {
        "passed": False,
        "code": "ORACLE_MISMATCH",
    }


def test_testbed_entrypoint_maps_backend_transport_to_unavailable(monkeypatch):
    monkeypatch.setattr(oracle_entrypoint, "_read_backend_account", lambda _: (_ for _ in ()).throw(OSError()))
    stream = io.TextIOWrapper(io.BytesIO(json.dumps(_payload()).encode()), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stream)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    assert oracle_entrypoint.main() == 0
    assert json.loads(output.getvalue()) == {
        "passed": False,
        "code": "ORACLE_UNAVAILABLE",
    }


def test_runtime_oracle_can_call_testbed_entrypoint_as_a_process():
    script = (
        "import testbed.oracle as o; "
        "o._read_backend_account=lambda _: {'id':'7048162359','type':'SAVINGS','balance':'41.23'}; "
        "raise SystemExit(o.main())"
    )
    result = InvocationResult(
        run_id="run_aaaaaaaaaaaaaaaa",
        status=InvocationStatus.SUCCESS,
        outputs={
            "available_balance": SecretStr("41.23"),
            "currency": SecretStr("USD"),
        },
    )
    report = asyncio.run(
        SubprocessValidationOracle((sys.executable, "-c", script)).check(
            result,
            ValidationFixture("synthetic_beta", SecretStr("7048162359")),
        )
    )
    assert report.passed is True
