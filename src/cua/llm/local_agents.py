"""Decision backends that borrow a locally installed coding agent's login.

A reviewer without a model API key usually has Claude Code, Codex, or Cursor
signed in. These backends call that CLI non-interactively for one decision at a
time, exactly like :class:`~cua.llm.decisions.CodexDecisionBackend`:

* only the value-free :class:`SafeDecisionRequest` is sent;
* the CLI runs in a fresh empty directory with no tools, no project rules, no
  hooks or MCP servers, and an allowlisted environment;
* its reply must validate as one typed decision, or it is rejected.

``resolve_decision_backend`` picks a backend: an explicit mode, or ``auto`` —
``OPENAI_API_KEY`` if set, otherwise the first of ``claude``, ``codex``,
``cursor-agent`` found on ``PATH``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from typing import Callable, Mapping, Sequence

from pydantic import ValidationError

from cua.llm.decisions import (
    DECISION_RESPONSE_SCHEMA,
    CodexDecisionBackend,
    DecisionBackend,
    DecisionProviderError,
    DecisionReply,
    OpenAIResponsesDecisionBackend,
    SafeDecisionRequest,
    _MODEL_ID,
    _communicate_bounded,
    _find_decision_payload,
    _nonnegative_int,
    _terminate_process_group,
    _validate_decision_envelope,
)

DEFAULT_OPENAI_MODEL = "gpt-5.5-2026-04-23"
PROVIDER_MODES = ("auto", "openai", "claude-code", "codex", "cursor")

_INSTRUCTIONS = (
    "Choose exactly one next typed decision for this read-only savings-balance task. "
    "Use only the listed control_ref values. Click only the requested account or the "
    "Accounts Overview link. Choose DONE only when the safe signals show a matching "
    "savings account and a parseable available balance. Choose WAIT for transient "
    "loading or BLOCKED when progress is unsafe. Never invent selectors, account "
    "values, credentials, or additional work. Keep rationale to one short sentence "
    "of at most 120 characters."
)
_BASE_ENV_KEYS = frozenset(
    {
        "HOME", "USER", "LOGNAME", "PATH", "SHELL", "TMPDIR", "TMP", "TEMP",
        "LANG", "LC_ALL", "LC_CTYPE", "TERM", "NO_COLOR",
        "SSL_CERT_FILE", "SSL_CERT_DIR",
        "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    }
)


class _LocalAgentBackend:
    """Shared subprocess handling for one-shot, tool-less agent CLI calls."""

    _NAME = "local-agent"
    _MAX_PROMPT_BYTES = 64 * 1024
    _MAX_OUTPUT_BYTES = 256 * 1024
    _EXTRA_ENV_KEYS: frozenset[str] = frozenset()

    def __init__(
        self,
        model: str | None = None,
        *,
        executable: str | Sequence[str],
        max_timeout_seconds: float = 180.0,
    ) -> None:
        if model is not None and not _MODEL_ID.fullmatch(model):
            raise ValueError("model identifier is invalid")
        parts = (executable,) if isinstance(executable, str) else tuple(executable)
        if not parts or any(not isinstance(item, str) or not item for item in parts):
            raise ValueError("agent executable is invalid")
        if isinstance(max_timeout_seconds, bool) or not 0 < float(max_timeout_seconds) <= 1_800:
            raise ValueError("agent timeout limit is invalid")
        self._model_argument = model
        self._executable = parts
        self._max_timeout_seconds = float(max_timeout_seconds)
        self._last_model: str | None = None

    @property
    def model_id(self) -> str:
        return self._last_model or self._model_argument or self._NAME

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._model_argument!r}, executable={self._executable[0]!r})"

    async def choose(self, request: SafeDecisionRequest, *, timeout_seconds: float) -> DecisionReply:
        try:
            request = SafeDecisionRequest.model_validate(request.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            raise DecisionProviderError("REQUEST_INVALID") from None
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise DecisionProviderError("PROVIDER_TIMEOUT")
        payload = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        timeout = min(float(timeout_seconds), self._max_timeout_seconds)
        with tempfile.TemporaryDirectory(prefix=f"cua-{self._NAME}-") as work_dir:
            command, stdin = self._command(payload)
            if len(stdin) > self._MAX_PROMPT_BYTES:
                raise DecisionProviderError("REQUEST_TOO_LARGE")
            environment = {
                key: value
                for key, value in os.environ.items()
                if key in _BASE_ENV_KEYS or key in self._EXTRA_ENV_KEYS
            }
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=work_dir,
                    env=environment,
                    start_new_session=True,
                )
            except (FileNotFoundError, OSError):
                raise DecisionProviderError("PROVIDER_UNAVAILABLE") from None
            try:
                stdout, stderr = await asyncio.wait_for(
                    _communicate_bounded(process, stdin, self._MAX_OUTPUT_BYTES), timeout=timeout
                )
            except (TimeoutError, asyncio.TimeoutError):
                await _terminate_process_group(process)
                raise DecisionProviderError("PROVIDER_TIMEOUT") from None
            except asyncio.CancelledError:
                await _terminate_process_group(process)
                raise
            except DecisionProviderError:
                raise
            except Exception:
                await _terminate_process_group(process)
                raise DecisionProviderError("PROVIDER_UNAVAILABLE") from None
        return self._reply(process.returncode, stdout, stderr)

    def _command(self, payload: str) -> tuple[list[str], bytes]:
        raise NotImplementedError

    def _reply(self, returncode: int | None, stdout: bytes, stderr: bytes) -> DecisionReply:
        raise NotImplementedError

    def _decision_reply(self, value: object, input_tokens=None, output_tokens=None) -> DecisionReply:
        decision = _validate_decision_envelope(value)
        return DecisionReply(
            decision=decision,
            model_id=self.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _classify_cli_failure(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("not logged in", "please log in", "login", "unauthorized", "authentication", "401")):
        return "PROVIDER_CREDENTIAL_MISSING"
    if any(token in lowered for token in ("rate limit", "usage limit", "quota", "too many requests", "429")):
        return "PROVIDER_QUOTA_EXCEEDED"
    return "PROVIDER_UNAVAILABLE"


class ClaudeCodeDecisionBackend(_LocalAgentBackend):
    """One decision from ``claude -p`` using the signed-in Claude Code account.

    ``--safe-mode`` drops CLAUDE.md, skills, plugins, hooks, and MCP servers;
    ``--tools ""`` removes every tool; ``--json-schema`` constrains the reply.
    """

    _NAME = "claude-code"
    # Claude Code's own auth: the macOS keychain via HOME/USER, or these.
    _EXTRA_ENV_KEYS = frozenset({"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})

    def __init__(self, model: str | None = None, *, executable: str | Sequence[str] = "claude", max_timeout_seconds: float = 180.0) -> None:
        super().__init__(model, executable=executable, max_timeout_seconds=max_timeout_seconds)

    def _command(self, payload: str) -> tuple[list[str], bytes]:
        command = [
            *self._executable,
            "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(DECISION_RESPONSE_SCHEMA, sort_keys=True),
            "--tools", "",
            "--safe-mode",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--system-prompt", _INSTRUCTIONS,
        ]
        if self._model_argument is not None:
            command.extend(("--model", self._model_argument))
        return command, payload.encode("utf-8")

    def _reply(self, returncode: int | None, stdout: bytes, stderr: bytes) -> DecisionReply:
        try:
            envelope = json.loads(stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            envelope = None
        if not isinstance(envelope, dict):
            raise DecisionProviderError(
                _classify_cli_failure(stderr.decode("utf-8", "replace")) if returncode else "MODEL_RESPONSE_INVALID"
            )
        if envelope.get("is_error") or returncode:
            raise DecisionProviderError(_classify_cli_failure(str(envelope.get("result", ""))))
        models = envelope.get("modelUsage")
        if isinstance(models, dict) and len(models) == 1:
            name = next(iter(models))
            if isinstance(name, str) and _MODEL_ID.fullmatch(name):
                self._last_model = name
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        value = envelope.get("structured_output")
        if not isinstance(value, dict):
            raw = _find_decision_payload(envelope.get("result"))
            value = json.loads(raw) if raw is not None else None
        if value is None:
            raise DecisionProviderError("MODEL_RESPONSE_INVALID")
        return self._decision_reply(
            value,
            _nonnegative_int(usage.get("input_tokens")),
            _nonnegative_int(usage.get("output_tokens")),
        )


class CursorAgentDecisionBackend(_LocalAgentBackend):
    """One decision from ``cursor-agent -p`` using the signed-in Cursor account.

    Print mode without ``--force`` does not execute commands, and the call runs
    in an empty directory. The CLI has no schema flag, so the schema travels in
    the prompt and the reply is validated as strictly as every other backend.
    """

    _NAME = "cursor-agent"
    _EXTRA_ENV_KEYS = frozenset({"CURSOR_API_KEY"})

    def __init__(self, model: str | None = None, *, executable: str | Sequence[str] = "cursor-agent", max_timeout_seconds: float = 180.0) -> None:
        super().__init__(model, executable=executable, max_timeout_seconds=max_timeout_seconds)

    def _command(self, payload: str) -> tuple[list[str], bytes]:
        prompt = (
            f"{_INSTRUCTIONS}\nReply with only one JSON object, no prose, matching this JSON Schema:\n"
            f"{json.dumps(DECISION_RESPONSE_SCHEMA, sort_keys=True)}\nRequest:\n{payload}"
        )
        command = [*self._executable, "-p", "--output-format", "json"]
        if self._model_argument is not None:
            command.extend(("--model", self._model_argument))
        command.append(prompt)
        return command, b""

    def _reply(self, returncode: int | None, stdout: bytes, stderr: bytes) -> DecisionReply:
        text = stdout.decode("utf-8", "replace")
        if returncode:
            raise DecisionProviderError(_classify_cli_failure(stderr.decode("utf-8", "replace") + text))
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            envelope = text
        result = envelope.get("result") if isinstance(envelope, dict) else envelope
        if isinstance(result, str):
            # Tolerate a fenced block around the JSON object.
            result = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        raw = _find_decision_payload(result)
        if raw is None:
            raise DecisionProviderError("MODEL_RESPONSE_INVALID")
        return self._decision_reply(json.loads(raw))


def resolve_decision_backend(
    mode: str = "auto",
    *,
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    codex_executable: Sequence[str] = ("codex",),
) -> tuple[DecisionBackend, str]:
    """Return ``(backend, resolved_mode)`` for a provider mode.

    ``auto`` never guesses a model for another provider: ``model`` applies only
    to an explicitly named mode.
    """
    values = os.environ if environ is None else environ
    if mode not in PROVIDER_MODES:
        raise ValueError(f"provider must be one of {', '.join(PROVIDER_MODES)}")
    if mode == "auto":
        if values.get("OPENAI_API_KEY"):
            mode = "openai"
        else:
            found = next(
                (name for name, binary in (("claude-code", "claude"), ("codex", codex_executable[0]), ("cursor", "cursor-agent")) if which(binary)),
                None,
            )
            if found is None:
                raise DecisionProviderError("MODEL_NOT_CONFIGURED")
            mode = found
        model = None
    if mode == "openai":
        return OpenAIResponsesDecisionBackend(model or DEFAULT_OPENAI_MODEL), mode
    if mode == "claude-code":
        return ClaudeCodeDecisionBackend(model), mode
    if mode == "codex":
        return CodexDecisionBackend(model, executable=tuple(codex_executable), max_timeout_seconds=180.0), mode
    return CursorAgentDecisionBackend(model), mode
