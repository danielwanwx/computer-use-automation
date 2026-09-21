from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from types import SimpleNamespace

import pytest

import cua.sessions.manager as manager_module
from cua.sessions.actor import ResumeDisposition
from cua.sessions import (
    ManagedSession,
    PrincipalBinding,
    PrincipalSpec,
    SessionActor,
    SessionError,
    SessionHandle,
    SessionManager,
)


def _principal() -> PrincipalSpec:
    return PrincipalSpec(
        alias="alpha",
        username_env="PARABANK_DEMO_ALPHA_USERNAME",
        password_env="PARABANK_DEMO_ALPHA_PASSWORD",
        expected_display_name="Synthetic Alpha",
    )


class _FakePage:
    def __init__(self, url="http://127.0.0.1:8080/parabank/index.htm"):
        self.url = url
        self.closed = False
        self.goto_calls = []
        self.handlers = {}

    def is_closed(self):
        return self.closed

    def set_default_timeout(self, _timeout):
        pass

    def on(self, event, handler):
        self.handlers[event] = handler

    async def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        self.url = url


class _FakeContext:
    def __init__(self):
        self.pages = []
        self.closed = False

    async def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class _FakeBrowser:
    version = "test-browser"

    def __init__(self):
        self.contexts = []
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def new_context(self, **_kwargs):
        context = _FakeContext()
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


def _set_demo_credentials(monkeypatch):
    monkeypatch.setenv("PARABANK_DEMO_ALPHA_USERNAME", "synthetic-user")
    monkeypatch.setenv("PARABANK_DEMO_ALPHA_PASSWORD", "synthetic-pass")


def test_origin_exposes_authority_separately_from_navigation_base_url():
    manager = SessionManager((_principal(),))

    if manager.origin != "http://127.0.0.1:8080":
        raise AssertionError("session origin must be authority-only")
    if manager.base_url != "http://127.0.0.1:8080/parabank":
        raise AssertionError("navigation base must retain the application context path")


@pytest.mark.parametrize(
    "origin",
    (
        "http://127.0.0.1:8080@evil.example/parabank",
        "http://user@127.0.0.1:8080/parabank",
        "http://127.0.0.1:8081/parabank",
        "http://127.0.0.1:8080/parabank?next=http://evil.example",
        "http://127.0.0.1:8080/parabank#fragment",
    ),
)
def test_session_manager_rejects_confused_or_nonlocal_origins(origin: str):
    with pytest.raises(ValueError):
        SessionManager((_principal(),), origin=origin)


def test_expected_identity_is_not_in_principal_repr():
    spec = _principal()

    if spec.expected_display_name in repr(spec):
        raise AssertionError("expected display name leaked through repr")


def test_get_state_exposes_browser_version_without_ui_access():
    manager = SessionManager((_principal(),))
    manager._sessions["s_test"] = SimpleNamespace(
        handle=SimpleNamespace(
            session_id="s_test",
            principal_alias="alpha",
            state="ACTIVE",
            auth_generation=2,
            profile_id="parabank-native-v1",
            origin="http://127.0.0.1:8080",
            browser_version="1.60.0",
        ),
        state="ACTIVE",
        actor=SessionActor(),
    )

    state = asyncio.run(manager.get_state("s_test"))

    assert state.browser_version == "1.60.0"


def test_validation_binding_is_issued_only_for_configured_test_principal_and_is_one_use(
    monkeypatch,
):
    manager = SessionManager(
        (_principal(),),
        validation_principal_aliases=("alpha",),
    )
    handle = SimpleNamespace(
        session_id="s_validation",
        principal_alias="alpha",
        profile_id="parabank-native-v1",
        origin="http://127.0.0.1:8080",
        browser_version="1.60.0",
        auth_generation=3,
    )
    manager._sessions[handle.session_id] = SimpleNamespace(
        handle=handle,
        state="ACTIVE",
        binding=SimpleNamespace(alias="alpha", authentication_generation=3),
    )

    async def prepared(alias):
        assert alias == "alpha"
        return handle

    monkeypatch.setattr(manager, "prepare", prepared)
    returned, binding = asyncio.run(manager.prepare_validation_session("alpha"))

    assert returned is handle
    assert binding.session_id == handle.session_id
    assert binding.principal_alias == "alpha"
    assert binding.authentication_generation == 3
    assert binding._token not in repr(binding)
    assert manager.consume_validation_session_binding(
        replace(binding, _token="forged-token"),
        handle.session_id,
    ) is False
    assert manager.consume_validation_session_binding(
        replace(binding, session_id="s_another_session"),
        handle.session_id,
    ) is False
    assert manager.consume_validation_session_binding(binding, handle.session_id) is True
    assert manager.consume_validation_session_binding(binding, handle.session_id) is False


def test_ordinary_prepare_cannot_issue_validation_binding_or_touch_browser_for_non_test_alias(
    monkeypatch,
):
    manager = SessionManager((_principal(),))
    calls = []

    async def prepared(alias):
        calls.append(alias)
        raise AssertionError("untrusted alias must be rejected before browser preparation")

    monkeypatch.setattr(manager, "prepare", prepared)

    with pytest.raises(SessionError) as error:
        asyncio.run(manager.prepare_validation_session("alpha"))

    assert error.value.code == "INPUT_INVALID"
    assert calls == []


def test_concurrent_prepare_launches_one_browser_and_creates_isolated_contexts(
    monkeypatch,
    tmp_path,
):
    manager = SessionManager((_principal(),), target_lock_path=tmp_path / "target.lock")
    _set_demo_credentials(monkeypatch)
    launches = []

    async def get_browser():
        if manager._browser is not None and manager._browser.is_connected():
            return manager._browser
        candidate = _FakeBrowser()
        launches.append(candidate)
        await asyncio.sleep(0)
        manager._browser = candidate
        return candidate

    async def login(session, _spec, _username, _password, *, navigate=True):
        session.state = "ACTIVE"
        session.handle = replace(session.handle, auth_generation=1)
        session.binding = SimpleNamespace(
            alias="alpha",
            authentication_generation=1,
        )

    async def scenario():
        manager._get_browser = get_browser
        manager._ui_login = login
        first, second = await asyncio.gather(
            manager.prepare("alpha"),
            manager.prepare("alpha"),
        )
        assert first.session_id != second.session_id
        assert len(launches) == 1
        assert len(launches[0].contexts) == 2
        assert launches[0].contexts[0] is not launches[0].contexts[1]
        await manager.close_all()
        assert launches[0].closed

    asyncio.run(scenario())


def test_close_all_waits_for_prepare_then_closes_all_sessions_and_prevents_restart(
    monkeypatch,
    tmp_path,
):
    manager = SessionManager((_principal(),), target_lock_path=tmp_path / "target.lock")
    _set_demo_credentials(monkeypatch)
    browser = _FakeBrowser()
    login_started = asyncio.Event()
    release_login = asyncio.Event()

    async def get_browser():
        manager._browser = browser
        return browser

    async def login(session, _spec, _username, _password, *, navigate=True):
        login_started.set()
        await release_login.wait()
        session.state = "ACTIVE"
        session.handle = replace(session.handle, auth_generation=1)
        session.binding = SimpleNamespace(
            alias="alpha",
            authentication_generation=1,
        )

    async def scenario():
        manager._get_browser = get_browser
        manager._ui_login = login
        preparing = asyncio.create_task(manager.prepare("alpha"))
        await login_started.wait()
        closing = asyncio.create_task(manager.close_all())
        await asyncio.sleep(0)
        assert not closing.done()
        release_login.set()
        handle = await preparing
        await closing
        assert handle.session_id not in manager._sessions
        assert browser.contexts[0].closed
        assert browser.closed
        with pytest.raises(SessionError) as error:
            await manager.prepare("alpha")
        assert error.value.code == "SESSION_LOST"

    asyncio.run(scenario())


def test_failed_prepare_cleans_up_under_lifecycle_lock_without_deadlock(
    monkeypatch,
    tmp_path,
):
    manager = SessionManager((_principal(),), target_lock_path=tmp_path / "target.lock")
    _set_demo_credentials(monkeypatch)
    browser = _FakeBrowser()

    async def get_browser():
        manager._browser = browser
        return browser

    async def fail_login(_session, _spec, _username, _password, *, navigate=True):
        raise SessionError("INPUT_INVALID")

    async def scenario():
        manager._get_browser = get_browser
        manager._ui_login = fail_login
        with pytest.raises(SessionError) as error:
            await asyncio.wait_for(manager.prepare("alpha"), timeout=1)
        assert error.value.code == "INPUT_INVALID"
        assert manager._sessions == {}
        assert browser.contexts[0].closed
        assert browser.closed
        assert manager._browser is None

    asyncio.run(scenario())


def test_last_close_serializes_with_next_prepare(monkeypatch, tmp_path):
    manager = SessionManager((_principal(),), target_lock_path=tmp_path / "target.lock")
    _set_demo_credentials(monkeypatch)
    old_browser = _FakeBrowser()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    new_browsers = []

    async def slow_browser_close():
        close_started.set()
        await release_close.wait()
        old_browser.closed = True

    old_browser.close = slow_browser_close
    old_context = _FakeContext()
    old_page = _FakePage()
    old_handle = SessionHandle(
        session_id="s_old",
        page_id="p_old",
        principal_alias="alpha",
        profile_id="parabank-native-v1",
        origin="http://127.0.0.1:8080",
        browser_version="test-browser",
        auth_generation=1,
    )
    manager._browser = old_browser
    manager._sessions[old_handle.session_id] = ManagedSession(
        handle=old_handle,
        context=old_context,
        page=old_page,
        target_lock_fd=os.open(tmp_path / "old-lock", os.O_CREAT | os.O_RDWR, 0o600),
        state="ACTIVE",
    )

    async def get_browser():
        if manager._browser is not None and manager._browser.is_connected():
            return manager._browser
        browser = _FakeBrowser()
        new_browsers.append(browser)
        manager._browser = browser
        return browser

    async def login(session, _spec, _username, _password, *, navigate=True):
        session.state = "ACTIVE"
        session.handle = replace(session.handle, auth_generation=1)
        session.binding = SimpleNamespace(
            alias="alpha",
            authentication_generation=1,
        )

    async def scenario():
        manager._get_browser = get_browser
        manager._ui_login = login
        closing = asyncio.create_task(manager.close(old_handle.session_id))
        await close_started.wait()
        preparing = asyncio.create_task(manager.prepare("alpha"))
        await asyncio.sleep(0)
        assert not preparing.done()
        assert manager._sessions == {}
        release_close.set()
        await closing
        new_handle = await preparing
        assert new_handle.session_id in manager._sessions
        assert old_browser.closed
        assert len(new_browsers) == 1
        assert manager._browser is new_browsers[0]
        assert len(new_browsers[0].contexts) == 1
        await manager.close_all()

    asyncio.run(scenario())


async def _resuming_session(manager, tmp_path, *, state="ACTIVE", page=None):
    actor = SessionActor()
    await actor.begin_run("run-resume")
    paused = await actor.pause_and_drain(expected_epoch=0)
    human = await actor.claim_human(expected_epoch=paused.epoch)
    resuming = await actor.begin_resume(expected_epoch=human.epoch)
    handle = SessionHandle(
        session_id="s_reauth",
        page_id="p_original",
        principal_alias="alpha",
        profile_id="parabank-native-v1",
        origin="http://127.0.0.1:8080",
        browser_version="test-browser",
        auth_generation=1,
    )
    context = _FakeContext()
    page = page or _FakePage("http://127.0.0.1:8080/parabank/login.htm")
    binding = None
    if state == "ACTIVE":
        binding = PrincipalBinding(
            alias="alpha",
            authentication_generation=1,
            login_provenance="VISIBLE_UI_LOGIN",
            username_fingerprint="user-fingerprint",
            display_name_fingerprint="name-fingerprint",
            cookie_fingerprint="cookie-fingerprint",
        )
    session = ManagedSession(
        handle=handle,
        context=context,
        page=page,
        target_lock_fd=os.open(tmp_path / "fake-lock", os.O_CREAT | os.O_RDWR, 0o600),
        actor=actor,
        binding=binding,
        state=state,
        cookie_fingerprint="cookie-fingerprint" if binding else "",
    )
    manager._browser = _FakeBrowser()
    manager._sessions[handle.session_id] = session
    return session, resuming


def test_reauthenticate_existing_logs_in_only_on_current_page_and_keeps_same_context_page(
    monkeypatch,
    tmp_path,
):
    manager = SessionManager((_principal(),), target_lock_path=tmp_path / "target.lock")
    credential_reads = []
    login_calls = []

    async def missing_greeting(_page):
        raise SessionError("SESSION_EXPIRED")

    async def login_ready(_page):
        return True

    def credentials(_spec):
        credential_reads.append(True)
        return "synthetic-user", "synthetic-pass"

    async def login(actual_session, _spec, _username, _password, *, navigate=True):
        login_calls.append((actual_session.context, actual_session.page, navigate))
        actual_session.state = "ACTIVE"
        actual_session.handle = replace(
            actual_session.handle,
            auth_generation=actual_session.handle.auth_generation + 1,
        )
        actual_session.binding = PrincipalBinding(
            alias="alpha",
            authentication_generation=actual_session.handle.auth_generation,
            login_provenance="VISIBLE_UI_LOGIN",
            username_fingerprint="user-fingerprint",
            display_name_fingerprint="name-fingerprint",
            cookie_fingerprint="cookie-fingerprint",
        )
        actual_session.cookie_fingerprint = "cookie-fingerprint"

    monkeypatch.setattr(manager_module, "_read_display_name", missing_greeting)

    async def scenario():
        session, resuming = await _resuming_session(
            manager,
            tmp_path,
            state="SESSION_EXPIRED",
        )
        original_context, original_page, original_page_id = (
            session.context,
            session.page,
            session.handle.page_id,
        )
        manager._login_form_ready = login_ready
        manager._credentials = credentials
        manager._ui_login = login
        returned = await manager.reauthenticate_existing(
            session.handle.session_id,
            expected_epoch=resuming.epoch,
        )
        assert returned.page_id == original_page_id
        assert session.context is original_context
        assert session.page is original_page
        assert session.page.goto_calls == []
        assert credential_reads == [True]
        assert login_calls == [(original_context, original_page, False)]
        assert session.actor.snapshot.owner == "RESUMING"
        await manager.close_all()

    asyncio.run(scenario())


def test_reauthenticate_existing_wrong_member_stays_fenced_without_login_or_credentials(
    monkeypatch,
    tmp_path,
):
    manager = SessionManager((_principal(),), target_lock_path=tmp_path / "target.lock")
    calls = []

    async def wrong_greeting(_page):
        return "Synthetic Beta"

    monkeypatch.setattr(manager_module, "_read_display_name", wrong_greeting)
    manager._credentials = lambda _spec: calls.append("credentials")
    manager._ui_login = lambda *args, **kwargs: calls.append("login")

    async def scenario():
        session, resuming = await _resuming_session(manager, tmp_path)
        with pytest.raises(SessionError) as error:
            await manager.reauthenticate_existing(
                session.handle.session_id,
                expected_epoch=resuming.epoch,
            )
        assert error.value.code == "SUBJECT_MISMATCH"
        assert calls == []
        assert session.page.goto_calls == []
        assert session.state == "SUBJECT_MISMATCH"
        assert session.actor.snapshot.owner == "RESUMING"
        assert session.context.closed is False
        await manager.close_all()

    asyncio.run(scenario())


def test_reauthenticate_existing_rejects_closed_page_as_lost_without_login(
    monkeypatch,
    tmp_path,
):
    manager = SessionManager((_principal(),), target_lock_path=tmp_path / "target.lock")
    credential_reads = []
    manager._credentials = lambda _spec: credential_reads.append(True)

    async def scenario():
        page = _FakePage()
        page.closed = True
        session, resuming = await _resuming_session(manager, tmp_path, page=page)
        with pytest.raises(SessionError) as error:
            await manager.reauthenticate_existing(
                session.handle.session_id,
                expected_epoch=resuming.epoch,
            )
        assert error.value.code == "SESSION_LOST"
        assert session.state == "SESSION_LOST"
        assert session.actor.snapshot.owner == "RESUMING"
        assert credential_reads == []
        await manager.close_all()

    asyncio.run(scenario())
