from dataclasses import replace

from fastapi.testclient import TestClient
from pydantic import SecretStr

from cua.application import (
    CapabilitySummary,
    DiscoveryRequest,
    RunHandle,
    RunMode,
    RunView,
    ServiceError,
    SessionView,
)
from cua.evidence.models import RunState
from cua.evidence.models import SafeReasonCode
from cua.handoff import HandoffState, InterventionView
from cua.models.bundles import BundleReference
from cua.web import create_app


REFERENCE = BundleReference(name="get_savings_balance", version="1.0.0", digest="a" * 64)


class _FakeService:
    def __init__(self):
        self.prepare_calls = 0
        self.discovery_calls = 0
        self.intervention_calls = []
        self.intervention = InterventionView(
            intervention_id="iv_0123456789abcdef01234567",
            session_id="s_0123456789abcdef",
            run_id="run_0123456789abcdef",
            step_id="read_balance",
            reason=SafeReasonCode.POSTCONDITION_UNKNOWN,
            state=HandoffState.WAITING_FOR_HUMAN,
            owner="NONE",
            epoch=2,
            page_id="p_0123456789ab",
        )

    async def list_sessions(self):
        return (
            SessionView(
                session_id="s_0123456789abcdef",
                principal_alias="synthetic_alpha",
                profile_id="parabank-native-v1",
                origin="http://127.0.0.1:8080",
                authentication_generation=1,
            ),
        )

    async def prepare_session(self, principal):
        self.prepare_calls += 1
        return (await self.list_sessions())[0]

    async def close_session(self, session_id):
        return None

    async def start_discovery(self, request: DiscoveryRequest):
        self.discovery_calls += 1
        return RunHandle(run_id="run_0123456789abcdef", mode=RunMode.DISCOVERY)

    async def invoke(self, request):
        return RunHandle(run_id="run_0123456789abcdef", mode=RunMode.REPLAY)

    async def get_run(self, run_id):
        return RunView(
            run_id=run_id,
            mode=RunMode.DISCOVERY,
            state=RunState.SUCCESS,
            created_at_ms=1,
            updated_at_ms=1,
        )

    async def read_result(self, run_id, session_id):
        raise ServiceError(409, "RESULT_PENDING")

    async def list_capabilities(self):
        return (
            CapabilitySummary(
                reference=REFERENCE,
                lifecycle="DRAFT",
                step_count=4,
                approved=False,
            ),
        )

    async def inspect_capability(self, reference):
        raise ServiceError(404, "CAPABILITY_NOT_FOUND")

    async def validate_capability(self, reference, *, request_id):
        return RunHandle(run_id="run_0123456789abcdef", mode=RunMode.VALIDATION)

    async def approve_capability(self, reference, **kwargs):
        raise ServiceError(409, "CAPABILITY_NOT_VALIDATED")

    async def list_interventions(self, *, operator_ref, session_id=None):
        self.intervention_calls.append(("list", operator_ref, None))
        return (self.intervention,)

    async def get_intervention(self, intervention_id, *, operator_ref):
        self.intervention_calls.append(("get", operator_ref, None))
        return self.intervention

    async def claim_intervention(self, intervention_id, *, expected_epoch, operator_ref):
        self.intervention_calls.append(("claim", operator_ref, expected_epoch))
        if expected_epoch != self.intervention.epoch:
            raise ServiceError(409, "INTERVENTION_STALE_EPOCH")
        self.intervention = replace(
            self.intervention,
            state=HandoffState.HUMAN_CLAIMED,
            owner="HUMAN",
            epoch=3,
        )
        return self.intervention

    async def resume_intervention(self, intervention_id, *, expected_epoch, operator_ref):
        self.intervention_calls.append(("resume", operator_ref, expected_epoch))
        if expected_epoch != self.intervention.epoch:
            raise ServiceError(409, "INTERVENTION_STALE_EPOCH")
        self.intervention = replace(
            self.intervention,
            state=HandoffState.RUNNING,
            owner="RUNTIME",
            epoch=4,
        )
        return self.intervention

    async def abort_intervention(self, intervention_id, *, expected_epoch, operator_ref):
        self.intervention_calls.append(("abort", operator_ref, expected_epoch))
        if expected_epoch != self.intervention.epoch:
            raise ServiceError(409, "INTERVENTION_STALE_EPOCH")
        self.intervention = replace(
            self.intervention,
            state=HandoffState.ABORTED,
            owner="HUMAN",
            epoch=5,
        )
        return self.intervention


def _headers(token="test-token", host="127.0.0.1:8765"):
    return {
        "Authorization": f"Bearer {token}",
        "Host": host,
        "Origin": "http://127.0.0.1:8765",
        "X-CUA-CSRF": "same-origin",
    }


def test_web_requires_token_and_exact_loopback_host_and_disables_caching():
    service = _FakeService()
    client = TestClient(create_app(service, operator_token="test-token"))
    missing = client.get("/api/sessions", headers={"Host": "127.0.0.1:8765"})
    assert missing.status_code == 401
    assert missing.json() == {"code": "AUTH_REQUIRED"}
    wrong_host = client.get("/api/sessions", headers=_headers(host="evil.test:8765"))
    assert wrong_host.status_code == 403
    assert wrong_host.json() == {"code": "ORIGIN_NOT_ALLOWED"}
    good = client.get("/api/sessions", headers=_headers())
    assert good.status_code == 200
    assert good.headers["cache-control"] == "no-store"
    assert good.json()[0]["principal_alias"] == "synthetic_alpha"


def test_web_uses_one_service_and_redacts_invalid_body_values():
    service = _FakeService()
    client = TestClient(create_app(service, operator_token="test-token"))
    prepared = client.post(
        "/api/sessions",
        headers=_headers(),
        json={"target_id": "parabank-local", "principal_ref": "synthetic_alpha"},
    )
    assert prepared.status_code == 201
    assert service.prepare_calls == 1

    extra = client.post(
        "/api/sessions",
        headers=_headers(),
        json={
            "target_id": "parabank-local",
            "principal_ref": "synthetic_alpha",
            "secret_account": "7048162359",
        },
    )
    assert extra.status_code == 422
    assert extra.json() == {"code": "INPUT_INVALID"}
    assert "7048162359" not in extra.text

    invalid = client.post(
        "/api/discovery",
        headers=_headers(),
        json={
            "session_id": "bad",
            "goal": "Get savings balance 7048162359",
            "inputs": {"account_id": "7048162359"},
            "request_id": "web-1",
            "operator_approved": True,
        },
    )
    assert invalid.status_code == 422
    assert invalid.json() == {"code": "INPUT_INVALID"}
    assert "7048162359" not in invalid.text


def test_web_starts_discovery_and_polls_safe_run_view():
    service = _FakeService()
    client = TestClient(create_app(service, operator_token="test-token"))
    response = client.post(
        "/api/discovery",
        headers=_headers(),
        json={
            "session_id": "s_0123456789abcdef",
            "goal": "Get savings balance",
            "inputs": {"account_id": "7048162359"},
            "request_id": "web-1",
        },
    )
    assert response.status_code == 202
    assert response.json()["run_id"] == "run_0123456789abcdef"
    status_response = client.get(
        "/api/runs/run_0123456789abcdef", headers=_headers()
    )
    assert status_response.status_code == 200
    assert status_response.json()["state"] == "SUCCESS"
    assert service.discovery_calls == 1



def test_index_exposes_capability_review_replay_and_result_workflow():
    service = _FakeService()
    client = TestClient(create_app(service, operator_token="test-token"))
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    for control in ("prepare", "discover", "refresh", "inspect", "validate", "approve", "replay", "read-result", "capability-name", "capability-version", "capability-digest"):
        assert f'id="{control}"' in html
    for route in ("/api/capabilities", "capabilityPath('validate'", "capabilityPath('approve'", "/api/invocations", "/api/runs/", "X-CUA-Session-ID"):
        assert route in html
    assert "localStorage" not in html
    assert "Use the capability digest in the CLI" not in html


def test_web_requires_exact_origin_for_state_changes():
    service = _FakeService()
    client = TestClient(create_app(service, operator_token="test-token"))
    headers = _headers()
    headers.pop("Origin")
    response = client.post(
        "/api/sessions",
        headers=headers,
        json={"target_id": "parabank-local", "principal_ref": "synthetic_alpha"},
    )
    assert response.status_code == 403
    assert response.json() == {"code": "ORIGIN_NOT_ALLOWED"}


def test_web_requires_csrf_marker_for_state_changes():
    service = _FakeService()
    client = TestClient(create_app(service, operator_token="test-token"))
    headers = _headers()
    headers.pop("X-CUA-CSRF")
    response = client.post(
        "/api/sessions",
        headers=headers,
        json={"target_id": "parabank-local", "principal_ref": "synthetic_alpha"},
    )
    assert response.status_code == 403
    assert response.json() == {"code": "CSRF_REQUIRED"}


def test_interventions_are_authenticated_safe_and_uncached():
    service = _FakeService()
    client = TestClient(create_app(service, operator_token="test-token"))
    missing = client.get("/api/interventions", headers={"Host": "127.0.0.1:8765"})
    assert missing.status_code == 401
    response = client.get("/api/interventions", headers=_headers())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload[0]["state"] == "WAITING_FOR_HUMAN"
    assert payload[0]["epoch"] == 2
    assert "token" not in response.text.lower()
    assert "credential" not in response.text.lower()
    detail = client.get(
        "/api/interventions/iv_0123456789abcdef01234567",
        headers=_headers(),
    )
    assert detail.status_code == 200
    assert detail.json()["intervention_id"] == payload[0]["intervention_id"]


def test_intervention_actions_require_exact_body_csrf_and_epoch():
    service = _FakeService()
    client = TestClient(create_app(service, operator_token="test-token"))
    headers = _headers()
    no_csrf = dict(headers)
    no_csrf.pop("X-CUA-CSRF")
    response = client.post(
        "/api/interventions/iv_0123456789abcdef01234567/claim",
        headers=no_csrf,
        json={"expected_epoch": 2},
    )
    assert response.status_code == 403
    assert response.json() == {"code": "CSRF_REQUIRED"}

    extra = client.post(
        "/api/interventions/iv_0123456789abcdef01234567/claim",
        headers=headers,
        json={"expected_epoch": 2, "token": "private"},
    )
    assert extra.status_code == 422
    assert extra.json() == {"code": "INPUT_INVALID"}
    assert "private" not in extra.text

    stale = client.post(
        "/api/interventions/iv_0123456789abcdef01234567/claim",
        headers=headers,
        json={"expected_epoch": 1},
    )
    assert stale.status_code == 409
    assert stale.json() == {"code": "INTERVENTION_STALE_EPOCH"}

    claimed = client.post(
        "/api/interventions/iv_0123456789abcdef01234567/claim",
        headers=headers,
        json={"expected_epoch": 2},
    )
    assert claimed.status_code == 200
    assert claimed.json()["state"] == "HUMAN_CLAIMED"
    resumed = client.post(
        "/api/interventions/iv_0123456789abcdef01234567/resume",
        headers=headers,
        json={"expected_epoch": 3},
    )
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "RUNNING"
    assert service.intervention_calls[-2:] == [
        ("claim", "local_operator", 2),
        ("resume", "local_operator", 3),
    ]


def test_index_exposes_intervention_poll_and_epoch_actions():
    client = TestClient(create_app(_FakeService(), operator_token="test-token"))
    html = client.get("/").text
    for control in ("intervention-state", "intervention-epoch", "intervention-claim", "intervention-resume", "intervention-abort"):
        assert f'id="{control}"' in html
    for route in ("/api/interventions", "expected_epoch", "refreshInterventions", "interventionAction('claim')", "interventionAction('resume')", "interventionAction('abort')"):
        assert route in html
    assert "_token" not in html
