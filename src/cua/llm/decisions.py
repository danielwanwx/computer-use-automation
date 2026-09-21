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
from typing import Awaitable, Callable, Literal, Mapping, Protocol
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
