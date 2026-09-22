"""Safe structured-output requests and an opt-in OpenAI Responses adapter.

The provider adapter uses the Responses API's strict ``text.format`` JSON Schema
surface. It is intentionally not invoked by the offline test suite. The API key is
looked up only when ``choose`` is called, and only ``OPENAI_API_KEY`` is accepted.
See https://developers.openai.com/api/docs/guides/structured-outputs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import re
from pathlib import Path
import signal
import tempfile
from typing import Awaitable, Callable, Literal, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import Field, SecretStr, TypeAdapter, ValidationError

from cua.models.actions import (
    ClickDecision,
    Decision,
    DoneDecision,
    WaitDecision,
    BlockedDecision,
)
from cua.models.base import StrictModel


RESPONSES_URL = "https://api.openai.com/v1/responses"
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,96}$", re.ASCII)
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 32 * 1024


DECISION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["CLICK", "WAIT", "DONE", "BLOCKED"],
        },
        "observation_id": {"type": "string"},
        "reason_code": {"type": "string"},
        "rationale": {"type": "string"},
        "control_ref": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
        },
        "timeout_ms": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
        },
    },
    "required": [
        "operation",
        "observation_id",
        "reason_code",
        "rationale",
        "control_ref",
        "timeout_ms",
    ],
    "additionalProperties": False,
}


class SafeControlChoice(StrictModel):
    control_ref: str = Field(min_length=1, max_length=64)
    frame_ref: str = Field(min_length=1, max_length=96)
    role: str = Field(min_length=1, max_length=48)
    safe_name: str = Field(max_length=120)


class SafeObservationSummary(StrictModel):
    observation_id: str = Field(min_length=1, max_length=96)
    route: Literal["home", "accounts_overview", "account_details", "other"]
    page_state: str = Field(min_length=1, max_length=48)
    controls: tuple[SafeControlChoice, ...] = Field(max_length=8)


class SafeSignals(StrictModel):
    principal_matches: bool | None
    overview_complete: bool | None
    requested_account_present: bool | None
    membership_valid: bool
    requested_account_matches: bool | None
    account_type_is_savings: bool | None
    available_balance_parseable: bool


class SafeHistoryItem(StrictModel):
    decision: Literal["CLICK", "WAIT", "DONE", "BLOCKED", "REJECTED"]
    target_alias: Literal["requested_account", "accounts_overview", "none"]
    effect_state: Literal["VERIFIED", "NOT_DISPATCHED", "OUTCOME_UNKNOWN"]
    reason_code: str = Field(min_length=1, max_length=64)


class SafeDecisionRequest(StrictModel):
    intent: Literal["get_savings_balance"]
    safe_goal: Literal[
        "Get the available balance for the requested savings account."
    ]
    requested_account_alias: Literal["<requested_account>"]
    observation: SafeObservationSummary
    signals: SafeSignals
    recent: tuple[SafeHistoryItem, ...] = Field(max_length=5)


@dataclass(frozen=True, slots=True)
class DecisionReply:
    decision: Decision
    model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not _MODEL_ID.fullmatch(self.model_id):
            raise ValueError("model identifier is invalid")
        if self.input_tokens is not None and self.input_tokens < 0:
            raise ValueError("input token count is invalid")
        if self.output_tokens is not None and self.output_tokens < 0:
            raise ValueError("output token count is invalid")

    def __repr__(self) -> str:
        return (
            "DecisionReply("
            f"model_id={self.model_id!r}, input_tokens={self.input_tokens!r}, "
            f"output_tokens={self.output_tokens!r})"
        )


class DecisionBackend(Protocol):
    @property
    def model_id(self) -> str: ...

    async def choose(
        self,
        request: SafeDecisionRequest,
        *,
        timeout_seconds: float,
    ) -> DecisionReply: ...


class DecisionProviderError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


Transport = Callable[
    [str, Mapping[str, str], bytes, float], Awaitable[Mapping[str, object]]
]
CredentialProvider = Callable[[], str | None]


class OpenAIResponsesDecisionBackend:
    """Structured-output backend pinned to the official OpenAI Responses endpoint.

    Inject ``transport`` and ``credential_provider`` for offline tests. This class
    performs no key lookup or HTTP request during construction.
    """

    def __init__(
        self,
        model: str,
        *,
        transport: Transport | None = None,
        credential_provider: CredentialProvider | None = None,
    ) -> None:
        if not _MODEL_ID.fullmatch(model):
            raise ValueError("model identifier is invalid")
        self._model = model
        self._transport = transport
        self._credential_provider = credential_provider or _openai_api_key

    @property
    def model_id(self) -> str:
        return self._model

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._model!r}, endpoint='official-responses')"

    async def choose(
        self,
        request: SafeDecisionRequest,
        *,
        timeout_seconds: float,
    ) -> DecisionReply:
        try:
            request = SafeDecisionRequest.model_validate(request.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            raise DecisionProviderError("REQUEST_INVALID") from None
        if timeout_seconds <= 0:
            raise DecisionProviderError("PROVIDER_TIMEOUT")
        api_key = self._credential_provider()
        if not api_key or not api_key.strip() or "\r" in api_key or "\n" in api_key:
            raise DecisionProviderError("PROVIDER_CREDENTIAL_MISSING")

        user_content = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        body = json.dumps(
            {
                "model": self._model,
                "store": False,
                "max_output_tokens": 512,
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "Choose one next decision for this read-only savings-balance task. "
                            "Use only listed control_ref values. Click only the requested account "
                            "or the Accounts Overview link. Choose DONE only when the safe signals "
                            "show a matching savings account and parseable available balance. "
                            "Choose WAIT for transient loading or BLOCKED when progress is unsafe. "
                            "Never invent selectors, account values, or additional work."
                        ),
                    },
                    {"role": "user", "content": user_content},
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "ui_decision",
                        "strict": True,
                        "schema": DECISION_RESPONSE_SCHEMA,
                    }
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > _MAX_REQUEST_BYTES:
            raise DecisionProviderError("REQUEST_TOO_LARGE")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            if self._transport is not None:
                response = await asyncio.wait_for(
                    self._transport(RESPONSES_URL, headers, body, timeout_seconds),
                    timeout=timeout_seconds,
                )
            else:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        _post_json,
                        RESPONSES_URL,
                        headers,
                        body,
                        timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
        except (TimeoutError, asyncio.TimeoutError):
            raise DecisionProviderError("PROVIDER_TIMEOUT") from None
        except DecisionProviderError:
            raise
        except Exception:
            raise DecisionProviderError("PROVIDER_UNAVAILABLE") from None
        finally:
            api_key = ""
            headers["Authorization"] = "Bearer <cleared>"

        return _parse_response(response, self._model)


def _openai_api_key() -> str | None:
    """Read only the explicitly named official-provider credential, on demand."""
    return os.environ.get("OPENAI_API_KEY")


def _post_json(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> Mapping[str, object]:
    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, file, code, message, response_headers, new_url):
            return None

    request = Request(url, data=body, headers=dict(headers), method="POST")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError):
        raise DecisionProviderError("PROVIDER_UNAVAILABLE") from None
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise DecisionProviderError("RESPONSE_TOO_LARGE")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DecisionProviderError("MODEL_RESPONSE_INVALID") from None
    if not isinstance(decoded, dict):
        raise DecisionProviderError("MODEL_RESPONSE_INVALID")
    return decoded


def _parse_response(response: Mapping[str, object], model_fallback: str) -> DecisionReply:
    if not isinstance(response, Mapping) or response.get("status") != "completed":
        raise DecisionProviderError("MODEL_RESPONSE_INCOMPLETE")
    output = response.get("output")
    if not isinstance(output, list):
        raise DecisionProviderError("MODEL_RESPONSE_INVALID")
    texts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "refusal":
                raise DecisionProviderError("MODEL_REFUSED")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
    if len(texts) != 1:
        raise DecisionProviderError("MODEL_RESPONSE_INVALID")
    try:
        raw_decision = json.loads(texts[0])
    except (TypeError, json.JSONDecodeError):
        raise DecisionProviderError("MODEL_RESPONSE_INVALID") from None
    decision = _validate_decision_envelope(raw_decision)

    usage = response.get("usage")
    input_tokens = output_tokens = None
    if isinstance(usage, Mapping):
        input_tokens = _nonnegative_int(usage.get("input_tokens"))
        output_tokens = _nonnegative_int(usage.get("output_tokens"))
    returned_model = response.get("model")
    model_id = returned_model if isinstance(returned_model, str) and _MODEL_ID.fullmatch(returned_model) else model_fallback
    return DecisionReply(
        decision=decision,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _validate_decision_envelope(value: object) -> Decision:
    if not isinstance(value, dict):
        raise DecisionProviderError("MODEL_RESPONSE_INVALID")
    allowed = {
        "operation",
        "observation_id",
        "reason_code",
        "rationale",
        "control_ref",
        "timeout_ms",
    }
    if set(value) != allowed:
        raise DecisionProviderError("MODEL_RESPONSE_INVALID")
    operation = value.get("operation")
    fields = {
        key: value[key]
        for key in ("observation_id", "reason_code", "rationale")
    }
    if operation == "CLICK":
        if not isinstance(value.get("control_ref"), str) or value.get("timeout_ms") is not None:
            raise DecisionProviderError("MODEL_RESPONSE_INVALID")
        fields.update(operation="CLICK", control_ref=value["control_ref"])
    elif operation == "WAIT":
        if value.get("control_ref") is not None or type(value.get("timeout_ms")) is not int:
            raise DecisionProviderError("MODEL_RESPONSE_INVALID")
        fields.update(operation="WAIT", timeout_ms=value["timeout_ms"])
    elif operation in {"DONE", "BLOCKED"}:
        if value.get("control_ref") is not None or value.get("timeout_ms") is not None:
            raise DecisionProviderError("MODEL_RESPONSE_INVALID")
        fields.update(operation=operation)
    else:
        raise DecisionProviderError("MODEL_RESPONSE_INVALID")
    try:
        parsed = TypeAdapter(Decision).validate_python(fields)
    except ValidationError:
        raise DecisionProviderError("MODEL_RESPONSE_INVALID") from None
    if not isinstance(parsed, (ClickDecision, WaitDecision, DoneDecision, BlockedDecision)):
        raise DecisionProviderError("MODEL_RESPONSE_INVALID")
    return parsed


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class CodexDecisionBackend:
    """Use the installed Codex CLI and its saved ChatGPT login for one choice.

    The subprocess receives only a redacted :class:`SafeDecisionRequest`.  It is
    started in an empty temporary directory with user configuration and project
    rules disabled.  Authentication is resolved by the CLI from its saved login;
    provider API-key variables are deliberately removed from the child environment.
    """

    _DEFAULT_MODEL_ID = "codex-cli"
    _MAX_PROMPT_BYTES = 64 * 1024
    _MAX_OUTPUT_BYTES = 32 * 1024
    _MAX_RETRIES = 2
    # The CLI uses these as global options, so they must remain before the
    # ``exec`` subcommand.  The browser subfeatures are included explicitly:
    # the installed CLI exposes them separately and disabling only the parent
    # feature is not a sufficient capability boundary.
    _DISABLED_FEATURES = (
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
    )
    # Saved-login execution needs the user's Codex home and the ordinary
    # process/runtime locations.  An allowlist is safer than trying to keep a
    # growing denylist of credential-like environment variable names.
    _CHILD_ENV_KEYS = frozenset(
        {
            "HOME",
            "CODEX_HOME",
            "PATH",
            "TMPDIR",
            "TMP",
            "TEMP",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "LC_MESSAGES",
            "LC_COLLATE",
            "LC_MONETARY",
            "LC_NUMERIC",
            "LC_TIME",
            "TERM",
            "NO_COLOR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
        }
    )

    def __init__(
        self,
        model: str | None = None,
        *,
        executable: str | Sequence[str] = "codex",
        max_retries: int = 0,
        max_timeout_seconds: float = 180.0,
        max_output_bytes: int = _MAX_OUTPUT_BYTES,
    ) -> None:
        if model is not None and not _MODEL_ID.fullmatch(model):
            raise ValueError("model identifier is invalid")
        if isinstance(executable, str):
            executable_parts = (executable,)
        else:
            executable_parts = tuple(executable)
        if not executable_parts or any(not isinstance(item, str) or not item for item in executable_parts):
            raise ValueError("codex executable is invalid")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= self._MAX_RETRIES:
            raise ValueError("codex retry limit is invalid")
        if isinstance(max_timeout_seconds, bool) or not isinstance(max_timeout_seconds, (int, float)) or not 0 < max_timeout_seconds <= 1_800:
            raise ValueError("codex timeout limit is invalid")
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or not 256 <= max_output_bytes <= 1_048_576:
            raise ValueError("codex output limit is invalid")
        self._model = model or self._DEFAULT_MODEL_ID
        self._model_argument = model
        self._executable = executable_parts
        self._max_retries = max_retries
        self._max_timeout_seconds = float(max_timeout_seconds)
        self._max_output_bytes = max_output_bytes

    @property
    def model_id(self) -> str:
        return self._model

    def __repr__(self) -> str:
        executable = self._executable[0]
        return f"{type(self).__name__}(model={self._model!r}, executable={executable!r})"

    async def choose(
        self,
        request: SafeDecisionRequest,
        *,
        timeout_seconds: float,
    ) -> DecisionReply:
        try:
            request = SafeDecisionRequest.model_validate(request.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            raise DecisionProviderError("REQUEST_INVALID") from None
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise DecisionProviderError("PROVIDER_TIMEOUT")
        prompt = self._prompt(request)
        if len(prompt) > self._MAX_PROMPT_BYTES:
            raise DecisionProviderError("REQUEST_TOO_LARGE")
        deadline = asyncio.get_running_loop().time() + min(
            float(timeout_seconds), self._max_timeout_seconds
        )
        last_error: DecisionProviderError | None = None
        for attempt in range(self._max_retries + 1):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise DecisionProviderError("PROVIDER_TIMEOUT")
            try:
                decision, input_tokens, output_tokens = await self._run_once(prompt, remaining)
            except DecisionProviderError as error:
                last_error = error
                if error.code not in {"PROVIDER_UNAVAILABLE", "PROVIDER_TIMEOUT"} or attempt >= self._max_retries:
                    raise
                continue
            return DecisionReply(
                decision=decision,
                model_id=self._model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        raise last_error or DecisionProviderError("PROVIDER_UNAVAILABLE")

    def _prompt(self, request: SafeDecisionRequest) -> bytes:
        safe_payload = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            "Choose exactly one next typed decision for this read-only savings-balance task. "
            "Use only the listed control_ref values and return one JSON object matching the "
            "provided output schema. Never invent selectors, account values, credentials, or work.\n"
            + safe_payload
        ).encode("utf-8")

    async def _run_once(
        self,
        prompt: bytes,
        timeout_seconds: float,
    ) -> tuple[Decision, int | None, int | None]:
        schema_path: str | None = None
        output_path: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="cua-codex-work-") as work_dir:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".json", delete=False
                ) as schema_file:
                    json.dump(DECISION_RESPONSE_SCHEMA, schema_file, sort_keys=True)
                    schema_path = schema_file.name
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".json", delete=False
                ) as output_file:
                    output_path = output_file.name
                # ``-a never`` is the installed CLI's approval flag.  It is a
                # global option and must precede the ``exec`` subcommand.  Keep
                # the stdin marker last; some CLI versions parse options after
                # the marker as prompt input rather than exec options.
                command = [*self._executable, "-a", "never"]
                for feature in self._DISABLED_FEATURES:
                    command.extend(("--disable", feature))
                command.append("exec")
                if self._model_argument is not None:
                    command.extend(("--model", self._model_argument))
                command.extend(
                    (
                        "--output-schema",
                        schema_path,
                        "--output-last-message",
                        output_path,
                        "--json",
                        "--ephemeral",
                        "--ignore-user-config",
                        "--ignore-rules",
                        "--skip-git-repo-check",
                        "--sandbox",
                        "read-only",
                        "--cd",
                        work_dir,
                    )
                )
                command.append("-")
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if key in self._CHILD_ENV_KEYS
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
                        _communicate_bounded(process, prompt, self._max_output_bytes),
                        timeout=timeout_seconds,
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
                if process.returncode != 0:
                    raise DecisionProviderError(
                        _classify_codex_failure(stderr.decode("utf-8", "replace"), stdout)
                    )
                raw = _read_last_message(output_path, self._max_output_bytes)
                if raw is None:
                    raw = _extract_codex_output(stdout)
                input_tokens, output_tokens = _extract_codex_usage(stdout)
                if raw is None:
                    raise DecisionProviderError("MODEL_RESPONSE_INVALID")
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                    raise DecisionProviderError("MODEL_RESPONSE_INVALID") from None
                try:
                    return _validate_decision_envelope(value), input_tokens, output_tokens
                except DecisionProviderError:
                    raise
        finally:
            for path in (schema_path, output_path):
                if path:
                    try:
                        Path(path).unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass


async def _communicate_bounded(
    process: asyncio.subprocess.Process,
    prompt: bytes,
    max_bytes: int,
) -> tuple[bytes, bytes]:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise DecisionProviderError("PROVIDER_UNAVAILABLE")
    try:
        process.stdin.write(prompt)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        # A CLI that closes stdin before reading the prompt must not leave a
        # process group behind, and the transport error must not escape as a
        # raw BrokenPipeError to callers.
        await _terminate_process_group(process)
        raise DecisionProviderError("PROVIDER_UNAVAILABLE") from None
    finally:
        try:
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, max_bytes))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, max_bytes))
    try:
        await process.wait()
        return await stdout_task, await stderr_task
    except BaseException:
        for task in (stdout_task, stderr_task):
            task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise


async def _read_bounded(stream: asyncio.StreamReader, max_bytes: int) -> bytes:
    captured = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return bytes(captured)
        if len(captured) < max_bytes:
            captured.extend(chunk[: max_bytes - len(captured)])


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    pid = process.pid
    if pid is None:
        return
    leader_running = process.returncode is None
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        if leader_running:
            try:
                process.terminate()
            except (ProcessLookupError, OSError):
                pass
    try:
        if leader_running:
            await asyncio.wait_for(process.wait(), timeout=0.5)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    # The leader may have exited while descendants still hold the process
    # group and the stdio pipes.  Always attempt SIGKILL after the grace
    # period; checking returncode first would leak exactly that case.
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        if process.returncode is None:
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                return
    if process.returncode is None:
        try:
            await process.wait()
        except (ProcessLookupError, OSError):
            pass


def _read_last_message(path: str | None, max_bytes: int) -> bytes | None:
    if not path:
        return None
    try:
        with open(path, "rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        raise DecisionProviderError("RESPONSE_TOO_LARGE")
    return raw.strip() or None


def _extract_codex_output(stdout: bytes) -> bytes | None:
    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict) and "operation" in value:
        return json.dumps(value).encode("utf-8")
    for line in reversed(text.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate = _find_decision_payload(event)
        if candidate is not None:
            return candidate
    return None


def _extract_codex_usage(stdout: bytes) -> tuple[int | None, int | None]:
    """Read token usage from the CLI's terminal ``turn.completed`` event."""
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            continue
        return (
            _nonnegative_int(usage.get("input_tokens")),
            _nonnegative_int(usage.get("output_tokens")),
        )
    return None, None


def _find_decision_payload(value: object) -> bytes | None:
    if isinstance(value, dict) and set(value) == set(DECISION_RESPONSE_SCHEMA["properties"]):
        return json.dumps(value).encode("utf-8")
    if isinstance(value, dict):
        for key in ("text", "output_text", "message", "content", "item", "result"):
            if key in value:
                found = _find_decision_payload(value[key])
                if found is not None:
                    return found
    if isinstance(value, list):
        for item in reversed(value):
            found = _find_decision_payload(item)
            if found is not None:
                return found
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return _find_decision_payload(parsed)
    return None


def _classify_codex_failure(stderr: str, stdout: bytes) -> str:
    text = (stderr + "\n" + stdout.decode("utf-8", "replace")).lower()
    if any(
        token in text
        for token in (
            "not logged in",
            "login required",
            "please log in",
            "run codex login",
            "no access token",
            "not authenticated",
            "authentication required",
            "unauthorized",
            "401",
        )
    ):
        return "PROVIDER_CREDENTIAL_MISSING"
    if any(token in text for token in ("quota", "rate limit", "too many requests", "usage limit", "insufficient quota")):
        return "PROVIDER_QUOTA_EXCEEDED"
    if any(token in text for token in ("timed out", "timeout")):
        return "PROVIDER_TIMEOUT"
    if any(token in text for token in ("invalid output", "invalid json", "schema validation", "structured output")):
        return "MODEL_RESPONSE_INVALID"
    return "PROVIDER_UNAVAILABLE"
