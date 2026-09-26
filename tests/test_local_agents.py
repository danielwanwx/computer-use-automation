"""Key-free decision backends that borrow a locally signed-in agent CLI."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import pytest

from cua.llm import (
    ClaudeCodeDecisionBackend,
    CodexDecisionBackend,
    CursorAgentDecisionBackend,
    DecisionProviderError,
    OpenAIResponsesDecisionBackend,
    resolve_decision_backend,
)
from cua.llm.decisions import (
    SafeControlChoice,
    SafeDecisionRequest,
    SafeObservationSummary,
    SafeSignals,
)

_DECISION = {
    "operation": "CLICK",
    "observation_id": "o_1",
    "reason_code": "OPEN_REQUESTED_ACCOUNT",
    "rationale": "Open the requested account.",
    "control_ref": "c_1",
    "timeout_ms": None,
}


def _request() -> SafeDecisionRequest:
    return SafeDecisionRequest(
        intent="get_savings_balance",
        safe_goal="Get the available balance for the requested savings account.",
        requested_account_alias="<requested_account>",
        observation=SafeObservationSummary(
            observation_id="o_1",
            route="accounts_overview",
            page_state="OVERVIEW_READY",
            controls=(SafeControlChoice(control_ref="c_1", frame_ref="f_main", role="link", safe_name="<requested_account>"),),
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


def _fake_cli(tmp_path, stdout: str, *, exit_code: int = 0):
    """A stand-in CLI that records its argv/env/stdin and prints ``stdout``."""
    record = tmp_path / "record.json"
    script = tmp_path / "fake_cli.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json, os, sys
            json.dump({{"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd(),
                       "stdin": sys.stdin.read()}}, open({str(record)!r}, "w"))
            sys.stdout.write({stdout!r})
            sys.exit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    return (sys.executable, str(script)), record


def test_auto_prefers_an_api_key_then_local_agents_in_order():
    installed = {"claude", "codex", "cursor-agent"}
    which = lambda name: f"/bin/{name}" if name in installed else None
    backend, mode = resolve_decision_backend("auto", environ={"OPENAI_API_KEY": "x"}, which=which)
    assert (mode, type(backend)) == ("openai", OpenAIResponsesDecisionBackend)
    assert backend.model_id == "gpt-5.5-2026-04-23"
    assert resolve_decision_backend("auto", environ={}, which=which)[1] == "claude-code"
    installed.discard("claude")
    backend, mode = resolve_decision_backend("auto", environ={}, which=which)
    assert (mode, type(backend)) == ("codex", CodexDecisionBackend)
    installed.discard("codex")
    backend, mode = resolve_decision_backend("auto", environ={}, which=which)
    assert (mode, type(backend)) == ("cursor", CursorAgentDecisionBackend)
    installed.clear()
    with pytest.raises(DecisionProviderError, match="MODEL_NOT_CONFIGURED"):
        resolve_decision_backend("auto", environ={}, which=which)


def test_auto_ignores_a_model_meant_for_a_named_provider():
    backend, _ = resolve_decision_backend("auto", model="sonnet", environ={}, which=lambda name: "/bin/x")
    assert backend.model_id == "claude-code"
    backend, _ = resolve_decision_backend("claude-code", model="sonnet", environ={})
    assert backend.model_id == "sonnet"


def test_claude_code_runs_tool_less_in_an_empty_directory_and_reads_structured_output(tmp_path, monkeypatch):
    envelope = {
        "type": "result",
        "is_error": False,
        "result": json.dumps(_DECISION),
        "structured_output": _DECISION,
        "usage": {"input_tokens": 12, "output_tokens": 34},
        "modelUsage": {"claude-opus-5-5": {}},
    }
    executable, record = _fake_cli(tmp_path, json.dumps(envelope))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-the-child")
    monkeypatch.setenv("PARABANK_DEMO_ALPHA_PASSWORD", "must-not-reach-the-child")
    reply = asyncio.run(ClaudeCodeDecisionBackend(executable=executable).choose(_request(), timeout_seconds=10))

    assert reply.decision.control_ref == "c_1"
    assert (reply.model_id, reply.input_tokens, reply.output_tokens) == ("claude-opus-5-5", 12, 34)
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--json-schema"):
        assert flag in argv
    assert "OPENAI_API_KEY" not in seen["env"] and "PARABANK_DEMO_ALPHA_PASSWORD" not in seen["env"]
    assert "<requested_account>" in seen["stdin"]
    assert "cua-claude-code-" in seen["cwd"]


def test_claude_code_error_envelope_and_bad_output_fail_closed(tmp_path):
    executable, _ = _fake_cli(tmp_path, json.dumps({"is_error": True, "result": "Not logged in"}), exit_code=1)
    with pytest.raises(DecisionProviderError, match="PROVIDER_CREDENTIAL_MISSING"):
        asyncio.run(ClaudeCodeDecisionBackend(executable=executable).choose(_request(), timeout_seconds=10))
    bad = dict(_DECISION, control_ref=None)
    executable, _ = _fake_cli(tmp_path, json.dumps({"is_error": False, "structured_output": bad}))
    with pytest.raises(DecisionProviderError, match="MODEL_RESPONSE_INVALID"):
        asyncio.run(ClaudeCodeDecisionBackend(executable=executable).choose(_request(), timeout_seconds=10))


def test_cursor_agent_accepts_a_fenced_json_result(tmp_path):
    envelope = {"type": "result", "result": "```json\n" + json.dumps(_DECISION) + "\n```"}
    executable, record = _fake_cli(tmp_path, json.dumps(envelope))
    reply = asyncio.run(CursorAgentDecisionBackend(executable=executable).choose(_request(), timeout_seconds=10))
    assert reply.decision.operation == "CLICK"
    argv = json.loads(record.read_text())["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"] and "--force" not in argv
    assert "JSON Schema" in argv[-1]


def test_cursor_agent_rejects_prose(tmp_path):
    executable, _ = _fake_cli(tmp_path, json.dumps({"result": "I clicked the account for you."}))
    with pytest.raises(DecisionProviderError, match="MODEL_RESPONSE_INVALID"):
        asyncio.run(CursorAgentDecisionBackend(executable=executable).choose(_request(), timeout_seconds=10))


def test_missing_cli_is_unavailable():
    with pytest.raises(DecisionProviderError, match="PROVIDER_UNAVAILABLE"):
        asyncio.run(
            ClaudeCodeDecisionBackend(executable="/definitely/missing/claude").choose(_request(), timeout_seconds=5)
        )
