import asyncio
import json
import os
from pathlib import Path
import stat
import textwrap

import pytest

from cua.application import ApplicationConfig, ApplicationService, ProviderMode
from cua.llm import CodexDecisionBackend, DecisionProviderError
from cua.llm.decisions import SafeDecisionRequest
from cua.sessions import PrincipalSpec


def _request() -> SafeDecisionRequest:
    from cua.llm.decisions import (
        SafeControlChoice,
        SafeObservationSummary,
        SafeSignals,
    )

    return SafeDecisionRequest(
        intent="get_savings_balance",
        safe_goal="Get the available balance for the requested savings account.",
        requested_account_alias="<requested_account>",
        observation=SafeObservationSummary(
            observation_id="obs_1",
            route="accounts_overview",
            page_state="OVERVIEW_READY",
            controls=(
                SafeControlChoice(
                    control_ref="c_1",
                    frame_ref="f_main",
                    role="link",
                    safe_name="<requested_account>",
                ),
            ),
        ),
        signals=SafeSignals(
            principal_matches=True,
            overview_complete=True,
            requested_account_present=True,
            membership_valid=True,
            requested_account_matches=None,
            account_type_is_savings=None,
            available_balance_parseable=False,
        ),
        recent=(),
    )


def _fake_codex(tmp_path: Path, *, mode: str = "success") -> tuple[str, Path]:
    inspect_path = tmp_path / "codex-inspect.json"
    child_pid_path = tmp_path / "codex-child.pid"
    script = tmp_path / "fake-codex"
    source = textwrap.dedent(
        """
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys
            import time

            inspect = Path(__INSPECT_PATH__)
            child_pid_path = Path(__CHILD_PID_PATH__)
            args = sys.argv[1:]
            output = args[args.index("--output-last-message") + 1]
            inspect.write_text(json.dumps({
                "argv": args,
                "cwd": os.getcwd(),
                "cwd_entries": sorted(os.listdir(".")),
                "openai_key": "OPENAI_API_KEY" in os.environ,
                "codex_key": "CODEX_API_KEY" in os.environ,
                "codex_access_token": "CODEX_ACCESS_TOKEN" in os.environ,
                "federation_rule_id": "OPENAI_FEDERATION_RULE_ID" in os.environ,
                "parabank_username": "PARABANK_DEMO_ALPHA_USERNAME" in os.environ,
                "parabank_password": "PARABANK_DEMO_ALPHA_PASSWORD" in os.environ,
                "cua_provider": "CUA_PROVIDER" in os.environ,
                "cua_secret": "CUA_TEST_SECRET" in os.environ,
                "generic_token": "CUSTOM_TOKEN" in os.environ,
                "generic_secret": "CUSTOM_SECRET" in os.environ,
                "generic_password": "CUSTOM_PASSWORD" in os.environ,
                "home": os.environ.get("HOME"),
                "codex_home": os.environ.get("CODEX_HOME"),
                "path_present": bool(os.environ.get("PATH")),
                "tmpdir": os.environ.get("TMPDIR"),
                "locale": os.environ.get("LANG"),
            }))
            mode = __MODE__
            if mode == "sleep":
                time.sleep(30)
            if mode == "fork":
                child = os.fork()
                if child == 0:
                    signal = __import__("signal")
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    time.sleep(30)
                    os._exit(0)
                child_pid_path.write_text(str(child))
                raise SystemExit(7)
            if mode == "close_stdin":
                os.close(0)
                time.sleep(0.1)
                raise SystemExit(7)
            if mode == "auth":
                print("not logged in", file=sys.stderr)
                raise SystemExit(7)
            if mode == "quota":
                print("rate limit quota exceeded", file=sys.stderr)
                raise SystemExit(8)
            if mode == "invalid":
                Path(output).write_text('{"operation":"CLICK"}')
                raise SystemExit(0)
            decision = {
                "operation": "CLICK",
                "observation_id": "obs_1",
                "reason_code": "CONTINUE",
                "rationale": "Open the requested account.",
                "control_ref": "c_1",
                "timeout_ms": None,
            }
            if mode == "stdout":
                print(json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": json.dumps(decision)
                }}))
                print(json.dumps({"type": "turn.completed", "usage": {
                    "input_tokens": 123, "output_tokens": 45
                }}))
            else:
                Path(output).write_text(json.dumps(decision))
        """
    ).lstrip()
    source = source.replace("__INSPECT_PATH__", repr(str(inspect_path)))
    source = source.replace("__CHILD_PID_PATH__", repr(str(child_pid_path)))
    source = source.replace("__MODE__", repr(mode))
    script.write_text(
        source,
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), inspect_path


def test_codex_backend_uses_safe_cli_contract_and_scrubs_api_keys(tmp_path, monkeypatch):
    executable, inspect_path = _fake_codex(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-process")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-cross-process")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "must-not-cross-process")
    monkeypatch.setenv("OPENAI_FEDERATION_RULE_ID", "must-not-cross-process")
    monkeypatch.setenv("PARABANK_DEMO_ALPHA_USERNAME", "must-not-cross-process")
    monkeypatch.setenv("PARABANK_DEMO_ALPHA_PASSWORD", "must-not-cross-process")
    monkeypatch.setenv("CUA_PROVIDER", "codex")
    monkeypatch.setenv("CUA_TEST_SECRET", "must-not-cross-process")
    monkeypatch.setenv("CUSTOM_TOKEN", "must-not-cross-process")
    monkeypatch.setenv("CUSTOM_SECRET", "must-not-cross-process")
    monkeypatch.setenv("CUSTOM_PASSWORD", "must-not-cross-process")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    reply = asyncio.run(
        CodexDecisionBackend("codex-test", executable=executable).choose(
            _request(), timeout_seconds=2
        )
    )

    assert reply.model_id == "codex-test"
    assert reply.decision.operation == "CLICK"
    details = json.loads(inspect_path.read_text(encoding="utf-8"))
    argv = details["argv"]
    assert argv[:2] == ["-a", "never"]
    assert argv.index("exec") > 1
    stdin_marker = argv.index("-")
    assert stdin_marker == len(argv) - 1
    for flag in ("--output-schema", "--output-last-message", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
        assert flag in argv
        assert argv.index(flag) < stdin_marker
    for feature in (
        "shell_tool",
        "unified_exec",
        "apps",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "computer_use",
        "plugins",
        "multi_agent",
        "hooks",
    ):
        disable_index = argv.index(feature) - 1
        assert argv[disable_index] == "--disable"
        assert disable_index < argv.index("exec")
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert Path(argv[argv.index("--cd") + 1]).resolve() == Path(details["cwd"]).resolve()
    assert details["cwd_entries"] == []
    assert details["openai_key"] is False
    assert details["codex_key"] is False
    assert details["codex_access_token"] is False
    assert details["federation_rule_id"] is False
    assert details["parabank_username"] is False
    assert details["parabank_password"] is False
    assert details["cua_provider"] is False
    assert details["cua_secret"] is False
    assert details["generic_token"] is False
    assert details["generic_secret"] is False
    assert details["generic_password"] is False
    assert details["home"] == str(tmp_path / "home")
    assert details["codex_home"] == str(tmp_path / "codex-home")
    assert details["path_present"] is True
    assert details["tmpdir"] == str(tmp_path)
    assert not Path(details["cwd"]).exists()


def test_codex_backend_accepts_structured_jsonl_fallback(tmp_path, monkeypatch):
    executable, _ = _fake_codex(tmp_path, mode="stdout")
    reply = asyncio.run(
        CodexDecisionBackend(executable=executable).choose(_request(), timeout_seconds=2)
    )
    assert reply.decision.control_ref == "c_1"
    assert reply.model_id == "codex-cli"
    assert reply.input_tokens == 123
    assert reply.output_tokens == 45


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("auth", "PROVIDER_CREDENTIAL_MISSING"),
        ("quota", "PROVIDER_QUOTA_EXCEEDED"),
        ("invalid", "MODEL_RESPONSE_INVALID"),
    ],
)
def test_codex_backend_maps_auth_quota_and_invalid_output(tmp_path, monkeypatch, mode, code):
    executable, _ = _fake_codex(tmp_path, mode=mode)
    with pytest.raises(DecisionProviderError) as raised:
        asyncio.run(CodexDecisionBackend(executable=executable).choose(_request(), timeout_seconds=2))
    assert raised.value.code == code


def test_codex_backend_timeout_and_cancel_cleanup(tmp_path, monkeypatch):
    executable, inspect_path = _fake_codex(tmp_path, mode="sleep")
    backend = CodexDecisionBackend(executable=executable)
    # 2s leaves room for the fake CLI's interpreter start-up on a loaded machine.
    with pytest.raises(DecisionProviderError) as raised:
        asyncio.run(backend.choose(_request(), timeout_seconds=2.0))
    assert raised.value.code == "PROVIDER_TIMEOUT"
    assert not Path(json.loads(inspect_path.read_text())["cwd"]).exists()
    inspect_path.unlink()

    async def cancelled():
        task = asyncio.create_task(backend.choose(_request(), timeout_seconds=10))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if inspect_path.exists():
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())
    assert not Path(json.loads(inspect_path.read_text())["cwd"]).exists()


def test_codex_backend_kills_descendants_after_leader_exit(tmp_path):
    executable, _ = _fake_codex(tmp_path, mode="fork")
    child_pid_path = tmp_path / "codex-child.pid"
    with pytest.raises(DecisionProviderError) as raised:
        asyncio.run(CodexDecisionBackend(executable=executable).choose(_request(), timeout_seconds=2.0))
    assert raised.value.code == "PROVIDER_TIMEOUT"
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        asyncio.run(asyncio.sleep(0.02))
    else:
        pytest.fail("Codex subprocess descendant survived cleanup")


def test_codex_backend_early_stdin_close_maps_to_safe_failure(tmp_path):
    executable, _ = _fake_codex(tmp_path, mode="close_stdin")
    with pytest.raises(DecisionProviderError) as raised:
        asyncio.run(CodexDecisionBackend(executable=executable).choose(_request(), timeout_seconds=2))
    assert raised.value.code == "PROVIDER_UNAVAILABLE"


def test_codex_backend_reports_unavailable_binary():
    with pytest.raises(DecisionProviderError) as raised:
        asyncio.run(
            CodexDecisionBackend(executable="/definitely/missing/codex").choose(
                _request(), timeout_seconds=1
            )
        )
    assert raised.value.code == "PROVIDER_UNAVAILABLE"


def _principal() -> PrincipalSpec:
    return PrincipalSpec(
        alias="synthetic_alpha",
        username_env="PARABANK_DEMO_ALPHA_USERNAME",
        password_env="PARABANK_DEMO_ALPHA_PASSWORD",
        expected_display_name="Synthetic Alpha",
    )


def test_provider_modes_compose_codex_without_openai_key(tmp_path):
    config = ApplicationConfig(
        data_root=tmp_path,
        principal_specs=(_principal(),),
        provider=ProviderMode.CODEX,
        codex_executable=(os.environ.get("PYTHON", "python3"), "codex"),
    )
    assert config.provider_mode is ProviderMode.CODEX
    assert config.provider_enabled is True
    service = ApplicationService.from_config(config)
    try:
        assert isinstance(service._decision_backend, CodexDecisionBackend)
    finally:
        asyncio.run(service.shutdown())


def test_openai_mode_still_requires_model_but_codex_model_is_optional(tmp_path):
    with pytest.raises(ValueError):
        ApplicationConfig(
            data_root=tmp_path,
            principal_specs=(_principal(),),
            provider=ProviderMode.OPENAI,
        )
    config = ApplicationConfig(
        data_root=tmp_path,
        principal_specs=(_principal(),),
        provider=ProviderMode.CODEX,
    )
    assert config.provider_mode is ProviderMode.CODEX
