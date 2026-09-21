import asyncio
import json

import pytest

from cua.llm.decisions import (
    DECISION_RESPONSE_SCHEMA,
    RESPONSES_URL,
    DecisionProviderError,
    OpenAIResponsesDecisionBackend,
    SafeControlChoice,
    SafeDecisionRequest,
    SafeObservationSummary,
    SafeSignals,
)
from cua.models.actions import ClickDecision


def _request():
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


def _response(decision=None, **updates):
    encoded = decision or {
        "operation": "CLICK",
        "observation_id": "obs_1",
        "reason_code": "CONTINUE",
        "rationale": "Open the requested account.",
        "control_ref": "c_1",
        "timeout_ms": None,
    }
    response = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(encoded)}],
            }
        ],
        "model": "gpt-4.1-mini",
        "usage": {"input_tokens": 21, "output_tokens": 8},
    }
    response.update(updates)
    return response


def test_responses_backend_is_lazy_and_sends_strict_sanitized_request():
    observed = {}
    credential_reads = []

    async def transport(url, headers, body, timeout):
        observed.update(
            url=url,
            headers=dict(headers),
            body=json.loads(body),
            timeout=timeout,
        )
        return _response()

    backend = OpenAIResponsesDecisionBackend(
        "gpt-4.1-mini",
        transport=transport,
        credential_provider=lambda: credential_reads.append("read") or "offline-test-key",
    )
    assert credential_reads == []
    assert "offline-test-key" not in repr(backend)

    reply = asyncio.run(backend.choose(_request(), timeout_seconds=2.0))

    assert isinstance(reply.decision, ClickDecision)
    assert reply.decision.control_ref == "c_1"
    assert reply.model_id == "gpt-4.1-mini"
    assert (reply.input_tokens, reply.output_tokens) == (21, 8)
    assert credential_reads == ["read"]
    assert observed["url"] == RESPONSES_URL
    assert observed["headers"]["Authorization"] == "Bearer offline-test-key"
    assert observed["body"]["store"] is False
    assert observed["body"]["text"]["format"] == {
        "type": "json_schema",
        "name": "ui_decision",
        "strict": True,
        "schema": DECISION_RESPONSE_SCHEMA,
    }
    serialized_request = json.dumps(observed["body"], sort_keys=True)
    assert "12345" not in serialized_request
    assert "Daniel" not in serialized_request
    assert "SAVINGS" not in serialized_request


def test_responses_backend_rejects_missing_credential_before_transport():
    calls = []

    async def transport(*args):
        calls.append(args)
        return _response()

    backend = OpenAIResponsesDecisionBackend(
        "gpt-4.1-mini", transport=transport, credential_provider=lambda: None
    )

    with pytest.raises(DecisionProviderError) as raised:
        asyncio.run(backend.choose(_request(), timeout_seconds=2.0))

    assert raised.value.code == "PROVIDER_CREDENTIAL_MISSING"
    assert calls == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_response(status="incomplete"), "MODEL_RESPONSE_INCOMPLETE"),
        (
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}
                ],
            },
            "MODEL_REFUSED",
        ),
        (
            _response(
                {
                    "operation": "TYPE_TEXT",
                    "observation_id": "obs_1",
                    "reason_code": "CONTINUE",
                    "rationale": "Do extra work.",
                    "control_ref": "c_1",
                    "timeout_ms": None,
                }
            ),
            "MODEL_RESPONSE_INVALID",
        ),
        (
            _response(
                {
                    "operation": "CLICK",
                    "observation_id": "obs_1",
                    "reason_code": "CONTINUE",
                    "rationale": "Open it.",
                    "control_ref": "c_1",
                    "timeout_ms": None,
                    "account_number": "12345",
                }
            ),
            "MODEL_RESPONSE_INVALID",
        ),
    ],
)
def test_responses_backend_rejects_refusal_incomplete_and_out_of_contract_output(response, code):
    async def transport(*args):
        return response

    backend = OpenAIResponsesDecisionBackend(
        "gpt-4.1-mini", transport=transport, credential_provider=lambda: "offline-test-key"
    )

    with pytest.raises(DecisionProviderError) as raised:
        asyncio.run(backend.choose(_request(), timeout_seconds=2.0))

    assert raised.value.code == code
