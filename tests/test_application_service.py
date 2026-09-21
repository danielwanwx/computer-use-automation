import asyncio
from types import SimpleNamespace

from pydantic import SecretStr
import pytest

from cua.application import ApplicationConfig, DiscoveryRequest, ReplayRequest, ServiceError
from cua.application.service import ApplicationService
from cua.discovery.runtime import DiscoveryOutcome, DiscoveryStatus
from cua.evidence import EvidenceSink
from cua.evidence.models import RunState
from cua.execution import InvocationResult, InvocationStatus
from cua.models.bundles import BundleReference
from cua.sessions import PrincipalSpec, SessionActor, SessionError, SessionHandle, SessionState


ACCOUNT = "9135802468"
SESSION_ID = "s_0123456789abcdef"


def _principal() -> PrincipalSpec:
    return PrincipalSpec(
        alias="synthetic_alpha",
        username_env="PARABANK_DEMO_ALPHA_USERNAME",
        password_env="PARABANK_DEMO_ALPHA_PASSWORD",
        expected_display_name="Synthetic Alpha",
    )


def _request(request_id="client-1", account=ACCOUNT):
    return DiscoveryRequest(
        session_id=SESSION_ID,
        goal=SecretStr(f"Get savings balance for {account}"),
        inputs={"account_id": SecretStr(account)},
        request_id=request_id,
    )


def _reference():
    return BundleReference(name="get_savings_balance", version="1.0.0", digest="a" * 64)


class _Sessions:
    def __init__(self):
        self.calls = 0
        self.closed = []
        self.actor = SessionActor()
        self.handle = SessionHandle(
            session_id=SESSION_ID,
            page_id="p_0123456789ab",
            principal_alias="synthetic_alpha",
            profile_id="parabank-native-v1",
            origin="http://127.0.0.1:8080",
            browser_version="1.60.0",
            auth_generation=1,
        )

    async def prepare(self, alias):
        assert alias == "synthetic_alpha"
        return self.handle

    async def get_state(self, session_id):
        self.calls += 1
        if session_id != SESSION_ID:
            raise SessionError("SESSION_LOST")
        return SessionState(
            session_id=SESSION_ID,
            principal_alias="synthetic_alpha",
            state="ACTIVE",
            auth_generation=1,
            profile_id="parabank-native-v1",
            origin="http://127.0.0.1:8080",
            browser_version="1.60.0",
            actor=self.actor,
        )

    async def close(self, session_id, *, allow_active=False):
        self.closed.append((session_id, allow_active))


class _Discovery:
    def __init__(self, *, gate=False):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not gate:
            self.release.set()
        self.intent = None
        self.context = None
        self.calls = 0

    async def run(self, context, intent, contract, **kwargs):
        self.calls += 1
        self.intent = intent
        self.context = context
        self.started.set()
        await self.release.wait()
        return DiscoveryOutcome(
            status=DiscoveryStatus.SUCCESS,
            reason_code="VERIFIED",
            decisions_used=1,
            verified_action_count=1,
            outputs={
                "available_balance": SecretStr("0.00"),
                "currency": SecretStr("USD"),
            },
        )


class _Backend:
    model_id = "offline-test-model"


class _Registry:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def prepare_execution(self, reference, **qualification):
        self.calls.append((reference, qualification))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            capability=SimpleNamespace(name=reference.name, version=reference.version)
        )


class _Replay:
    async def run(self, reference, context):
        return InvocationResult(
            run_id=context.run_alias,
            status=InvocationStatus.SUCCESS,
            outputs={
                "available_balance": SecretStr("0.00"),
                "currency": SecretStr("USD"),
            },
        )


class _UnexpectedReplay:
    async def run(self, reference, context):
            raise AssertionError("preflight-rejected replay must not start")


class _BlockingActorDiscovery:
    def __init__(self, actor):
        self.actor = actor
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, context, intent, contract, **kwargs):
        async def blocked_effect():
            self.started.set()
            await self.release.wait()

        await self.actor.submit(
            expected_epoch=context.expected_epoch,
            run_id=context.run_alias,
            operation=blocked_effect,
        )
        return DiscoveryOutcome(
            status=DiscoveryStatus.SUCCESS,
            reason_code="VERIFIED",
            decisions_used=1,
            verified_action_count=1,
            outputs={
                "available_balance": SecretStr("0.00"),
                "currency": SecretStr("USD"),
            },
        )


def _service(tmp_path, *, provider_enabled=False, gate=False, registry=None, clock=None):
    config = ApplicationConfig(
        data_root=tmp_path,
        principal_specs=(_principal(),),
        provider_enabled=provider_enabled,
        provider_model="offline-test-model" if provider_enabled else None,
    )
    sessions = _Sessions()
    discovery = _Discovery(gate=gate)
    evidence = EvidenceSink(config.evidence_root)
    service = ApplicationService(
        config,
        sessions=sessions,
        discovery=discovery,
            replay=_Replay(),
            registry=registry or _Registry(),
            evidence=evidence,
            decision_backend=_Backend() if provider_enabled else None,
            clock=clock,
    )
    return service, sessions, discovery, evidence


def test_disabled_discovery_fails_before_session_state_or_browser_work(tmp_path):
    async def scenario():
        service, sessions, _, _ = _service(tmp_path)
        await service.prepare_session("synthetic_alpha")
        with pytest.raises(ServiceError) as error:
            await service.start_discovery(_request())
        assert error.value.code == "MODEL_NOT_CONFIGURED"
        assert sessions.calls == 0
        assert not tuple((tmp_path / "evidence").glob("run_*/manifest.json"))

    asyncio.run(scenario())


def test_invalid_or_conflicting_inputs_fail_before_a_run_is_registered(tmp_path):
    async def scenario():
        service, sessions, _, _ = _service(tmp_path, provider_enabled=True)
        await service.prepare_session("synthetic_alpha")
        request = DiscoveryRequest(
            session_id=SESSION_ID,
            goal=SecretStr("Get savings balance for 9135802468"),
            inputs={"account_id": SecretStr("00001")},
            request_id="conflict",
        )
        with pytest.raises(ServiceError) as error:
            await service.start_discovery(request)
        assert error.value.code == "INPUT_CONFLICT"
        assert sessions.calls == 0
        assert not tuple((tmp_path / "evidence").glob("run_*/manifest.json"))

    asyncio.run(scenario())


def test_start_is_idempotent_and_copies_nested_secret_inputs_before_scheduling(tmp_path):
    async def scenario():
        service, _, discovery, _ = _service(tmp_path, provider_enabled=True, gate=True)
        await service.prepare_session("synthetic_alpha")
        original = _request()
        first = await service.start_discovery(original)
        await discovery.started.wait()
        original.inputs["account_id"] = SecretStr("00002")
        duplicate = await service.start_discovery(_request())
        assert duplicate.run_id == first.run_id
        assert discovery.calls == 1
        assert discovery.intent.requested_account_id.get_secret_value() == ACCOUNT
        assert discovery.context.input_bindings[
            "inputs.account_id"
        ].get_secret_value() == ACCOUNT

        changed = _request(account="00002")
        with pytest.raises(ServiceError) as conflict:
            await service.start_discovery(changed)
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        with pytest.raises(ServiceError) as busy:
            await service.start_discovery(_request(request_id="different"))
        assert busy.value.code == "SESSION_BUSY"

        discovery.release.set()
        for _ in range(20):
            if (await service.get_run(first.run_id)).state is RunState.SUCCESS:
                break
            await asyncio.sleep(0)
        assert (await service.get_run(first.run_id)).state is RunState.SUCCESS
        result = await service.read_result(first.run_id, SESSION_ID)
        assert isinstance(result, InvocationResult)
        assert result.status is InvocationStatus.SUCCESS
        assert result.outputs["available_balance"].get_secret_value() == "0.00"

    asyncio.run(scenario())


def test_invocation_preflights_approval_before_registering_or_returning_a_run(tmp_path):
    async def scenario():
        registry = _Registry(error=RuntimeError("private approval detail"))
        service, sessions, _, _ = _service(
            tmp_path,
            registry=registry,
        )
        await service.prepare_session("synthetic_alpha")
        request = ReplayRequest(
            session_id=SESSION_ID,
            reference=_reference(),
            inputs={"account_id": SecretStr(ACCOUNT)},
            request_id="replay-1",
        )
        with pytest.raises(ServiceError) as error:
            await service.invoke(request)
        assert error.value.code == "INVALID_BUNDLE"
        assert "private approval detail" not in str(error.value)
        assert len(registry.calls) == 1
        assert sessions.actor.active_run_id is None
        assert not tuple((tmp_path / "evidence").glob("run_*/manifest.json"))

    asyncio.run(scenario())


def test_result_is_session_bound_memory_only_and_expires(tmp_path):
    now = [100.0]

    async def scenario():
        service, _, discovery, _ = _service(
            tmp_path,
            provider_enabled=True,
            clock=lambda: now[0],
        )
        await service.prepare_session("synthetic_alpha")
        handle = await service.invoke(
            ReplayRequest(
                session_id=SESSION_ID,
                reference=_reference(),
                inputs={"account_id": SecretStr(ACCOUNT)},
                request_id="result-1",
            )
        )
        for _ in range(20):
            if (await service.get_run(handle.run_id)).state is RunState.SUCCESS:
                break
            await asyncio.sleep(0)
        with pytest.raises(ServiceError) as forbidden:
            await service.read_result(handle.run_id, "s_fedcba9876543210")
        assert forbidden.value.status == 403
        result = await service.read_result(handle.run_id, SESSION_ID)
        assert result.status is InvocationStatus.SUCCESS
        assert "0.00" not in repr(result)
        persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
        assert ACCOUNT.encode() not in persisted
        assert b"Get savings balance" not in persisted

        now[0] += 1_000
        with pytest.raises(ServiceError) as expired:
            await service.read_result(handle.run_id, SESSION_ID)
        assert expired.value.status == 410
        assert expired.value.code == "RESULT_EXPIRED"

    asyncio.run(scenario())


def test_close_drains_cancelled_actor_effect_before_closing_session(tmp_path):
    async def scenario():
        service, sessions, _, _ = _service(tmp_path, provider_enabled=True)
        discovery = _BlockingActorDiscovery(sessions.actor)
        service._discovery = discovery
        await service.prepare_session("synthetic_alpha")

        handle = await service.start_discovery(_request())
        await discovery.started.wait()
        record = service._runs[handle.run_id]
        record.task.cancel()
        await asyncio.sleep(0)

        assert not record.task.done()
        assert sessions.actor.active_run_id == handle.run_id
        close = asyncio.create_task(service.close_session(SESSION_ID))
        await asyncio.sleep(0)
        assert sessions.closed == []

        discovery.release.set()
        await close
        assert sessions.closed == [(SESSION_ID, False)]
        assert sessions.actor.active_run_id is None

    asyncio.run(scenario())
