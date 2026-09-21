import asyncio
import json
import sys

from pydantic import SecretStr

from cua.application import OracleReport, SubprocessValidationOracle, ValidationFixture
from cua.application import ApplicationConfig, ApplicationService
from cua.sessions import PrincipalSpec
from cua.execution import InvocationResult, InvocationStatus


FIXTURE = ValidationFixture("synthetic_beta", SecretStr("7048162359"))


def _result() -> InvocationResult:
    return InvocationResult(
        run_id="run_aaaaaaaaaaaaaaaa",
        status=InvocationStatus.SUCCESS,
        outputs={
            "available_balance": SecretStr("41.23"),
            "currency": SecretStr("USD"),
        },
    )


def _command(script: str) -> tuple[str, ...]:
    return (sys.executable, "-c", script)


def test_subprocess_oracle_sends_protected_payload_over_stdin_and_parses_typed_report():
    script = (
        "import json,sys; "
        "value=json.load(sys.stdin); "
        "assert value['fixture']['account_id']=='7048162359'; "
        "assert value['result']['outputs']['available_balance']=='41.23'; "
        "print(json.dumps({'passed':True,'code':'ORACLE_PASS'}))"
    )
    oracle = SubprocessValidationOracle(_command(script))
    report = asyncio.run(oracle.check(_result(), FIXTURE))
    assert report == OracleReport(passed=True, code="ORACLE_PASS")


def test_subprocess_oracle_suppresses_invalid_output_and_errors():
    oracle = SubprocessValidationOracle(_command("import sys; sys.stderr.write('secret'); sys.exit(3)"))
    report = asyncio.run(oracle.check(_result(), FIXTURE))
    assert report == OracleReport(passed=False, code="ORACLE_UNAVAILABLE")

    invalid = SubprocessValidationOracle(_command("print('{}')"))
    report = asyncio.run(invalid.check(_result(), FIXTURE))
    assert report == OracleReport(passed=False, code="ORACLE_UNAVAILABLE")


def test_subprocess_oracle_bounds_timeout_and_stdout():
    slow = SubprocessValidationOracle(
        _command("import time; time.sleep(2)"), timeout_seconds=0.1
    )
    report = asyncio.run(slow.check(_result(), FIXTURE))
    assert report.code == "ORACLE_UNAVAILABLE"

    noisy = SubprocessValidationOracle(_command("print('x'*10000)"), max_bytes=256)
    report = asyncio.run(noisy.check(_result(), FIXTURE))
    assert report.code == "ORACLE_UNAVAILABLE"


def test_oracle_request_does_not_place_protected_values_in_argv():
    script = (
        "import json,sys; "
        "assert len(sys.argv)==1; "
        "json.load(sys.stdin); "
        "print(json.dumps({'passed':False,'code':'ORACLE_MISMATCH'}))"
    )
    oracle = SubprocessValidationOracle(_command(script))
    report = asyncio.run(oracle.check(_result(), FIXTURE))
    assert report == OracleReport(passed=False, code="ORACLE_MISMATCH")


def test_from_config_composes_only_the_server_configured_oracle(tmp_path):
    config = ApplicationConfig(
        data_root=tmp_path,
        principal_specs=(
            PrincipalSpec(
                "synthetic_alpha",
                "PARABANK_DEMO_ALPHA_USERNAME",
                "PARABANK_DEMO_ALPHA_PASSWORD",
                "Synthetic Alpha",
            ),
            PrincipalSpec(
                "synthetic_beta",
                "PARABANK_DEMO_BETA_USERNAME",
                "PARABANK_DEMO_BETA_PASSWORD",
                "Synthetic Beta",
            ),
        ),
        validation_fixtures=(FIXTURE,),
        validation_oracle_command=_command("print('{}')"),
    )
    service = ApplicationService.from_config(config)
    assert isinstance(service._validation_oracle, SubprocessValidationOracle)
