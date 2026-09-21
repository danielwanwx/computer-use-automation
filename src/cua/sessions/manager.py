"""Async Playwright sessions with isolated contexts and UI-only authentication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from cua.profiles.parabank import LOCATORS, PROFILE_ID, READINESS_RULES, ROUTES
from cua.sessions.actor import SessionActor


DEFAULT_ORIGIN = "http://127.0.0.1:8080/parabank"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$", re.ASCII)


class SessionError(RuntimeError):
    def __init__(self, code: str, session_id: str | None = None) -> None:
        self.code = code
        self.session_id = session_id
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PrincipalSpec:
    alias: str
    username_env: str
    password_env: str
    expected_display_name: str = field(repr=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.alias, re.ASCII):
            raise ValueError("principal alias is invalid")
        if not _ENV_NAME.fullmatch(self.username_env) or not _ENV_NAME.fullmatch(self.password_env):
            raise ValueError("credential environment variable name is invalid")
        if not self.expected_display_name.strip() or len(self.expected_display_name) > 120:
            raise ValueError("expected display name is invalid")


@dataclass(frozen=True, slots=True)
class PrincipalBinding:
    alias: str
    authentication_generation: int
    login_provenance: str
    username_fingerprint: str = field(repr=False)
    display_name_fingerprint: str = field(repr=False)
    cookie_fingerprint: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SessionHandle:
    session_id: str
    page_id: str
    principal_alias: str
    profile_id: str
    origin: str
    browser_version: str
    auth_generation: int


@dataclass(frozen=True, slots=True)
class SessionState:
    session_id: str
    principal_alias: str
    state: str
    auth_generation: int
    profile_id: str
    origin: str
    browser_version: str
    actor: SessionActor = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ValidationSessionBinding:
    """One-use in-memory proof that a session was prepared for isolated validation."""

    session_id: str
    principal_alias: str
    authentication_generation: int
    _token: str = field(repr=False)


@dataclass(slots=True, repr=False)
class ManagedSession:
    handle: SessionHandle
    context: BrowserContext = field(repr=False)
    page: Page = field(repr=False)
    target_lock_fd: int = field(repr=False)
    actor: SessionActor = field(default_factory=SessionActor, repr=False)
    binding: PrincipalBinding | None = field(default=None, repr=False)
    document_generation: int = 0
    frame_generations: dict[str, int] = field(default_factory=lambda: {"f_main": 0}, repr=False)
    state: str = "PREPARING"
    cookie_fingerprint: str = field(default="", repr=False)


class SessionManager:
    """Owns one nonpersistent browser context for every configured principal."""

    def __init__(
        self,
        principal_specs: tuple[PrincipalSpec, ...] | list[PrincipalSpec],
        *,
        origin: str | None = None,
        target_lock_path: Path | str | None = None,
        browser_channel: str = "chrome",
        headless: bool = True,
        timeout_ms: int = 8_000,
        validation_principal_aliases: tuple[str, ...] | list[str] = (),
    ) -> None:
        self.base_url = _validate_origin(origin or os.environ.get("PARABANK_ORIGIN", DEFAULT_ORIGIN))
        self.origin = _authority(self.base_url)
        self._specs = {item.alias: item for item in principal_specs}
        if not self._specs or len(self._specs) != len(principal_specs):
            raise ValueError("principal aliases must be nonempty and unique")
        validation_aliases = tuple(validation_principal_aliases)
        if (
            len(set(validation_aliases)) != len(validation_aliases)
            or any(alias not in self._specs for alias in validation_aliases)
        ):
            raise ValueError("validation principals must be unique configured aliases")
        self._validation_principal_aliases = frozenset(validation_aliases)
        self._validation_binding_tokens: dict[str, str] = {}
        root = Path(__file__).resolve().parents[3]
        self._lock_path = Path(target_lock_path) if target_lock_path else root / "testbed/.cache/target-use.lock"
        self._channel = browser_channel
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: dict[str, ManagedSession] = {}
        self._fingerprint_key = secrets.token_bytes(32)
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

    async def prepare(self, principal_alias: str) -> SessionHandle:
        async with self._lifecycle_lock:
            return await self._prepare_locked(principal_alias)

    async def _prepare_locked(self, principal_alias: str) -> SessionHandle:
        if self._closed:
            raise SessionError("SESSION_LOST")
        spec = self._specs.get(principal_alias)
        if spec is None:
            raise SessionError("INPUT_INVALID")
        username, password = self._credentials(spec)
        lock_fd = _acquire_shared_lock(self._lock_path)
        context: BrowserContext | None = None
        session: ManagedSession | None = None
        try:
            browser = await self._get_browser()
            context = await browser.new_context(accept_downloads=False, service_workers="block")
            page = await context.new_page()
            page.set_default_timeout(self._timeout_ms)
            session_id = "s_" + secrets.token_hex(16)
            handle = SessionHandle(
                session_id=session_id,
                page_id="p_" + secrets.token_hex(12),
                principal_alias=principal_alias,
                profile_id=PROFILE_ID,
                origin=self.origin,
                browser_version=browser.version,
                auth_generation=0,
            )
            session = ManagedSession(handle, context, page, lock_fd)
            self._sessions[session_id] = session
            self._track_navigation(session)
            await self._ui_login(session, spec, username, password)
            return session.handle
        except SessionError as error:
            if session is not None and error.code in {"SESSION_EXPIRED", "SUBJECT_MISMATCH"}:
                session.state = error.code
                raise SessionError(error.code, session.handle.session_id) from None
            if session is not None:
                await self._close_locked(session.handle.session_id, allow_active=True)
            elif context is not None:
                await _close_context(context)
                _release_lock(lock_fd)
                if not self._sessions:
                    await self._close_browser()
            else:
                _release_lock(lock_fd)
            raise
        except (PlaywrightError, PlaywrightTimeoutError, OSError):
            if session is not None:
                await self._close_locked(session.handle.session_id, allow_active=True)
            elif context is not None:
                await _close_context(context)
                _release_lock(lock_fd)
                if not self._sessions:
                    await self._close_browser()
            else:
                _release_lock(lock_fd)
            raise SessionError("SESSION_LOST", session.handle.session_id if session else None) from None
        finally:
            username = None
            password = None

    async def prepare_validation_session(
        self,
        principal_alias: str,
    ) -> tuple[SessionHandle, ValidationSessionBinding]:
        """Prepare an explicitly configured test principal and mint a one-use binding.

        Ordinary ``prepare`` never grants validation authority. The validation alias
        allowlist is supplied by trusted application configuration, not by a request.
        """
        if principal_alias not in self._validation_principal_aliases:
            raise SessionError("INPUT_INVALID")
        handle = await self.prepare(principal_alias)
        session = self._sessions.get(handle.session_id)
        if (
            session is None
            or session.state != "ACTIVE"
            or session.binding is None
            or session.binding.alias != session.handle.principal_alias
            or session.binding.authentication_generation != session.handle.auth_generation
            or session.handle.principal_alias not in self._validation_principal_aliases
            or session.handle.auth_generation != handle.auth_generation
        ):
            raise SessionError("SESSION_LOST", handle.session_id)
        token = secrets.token_urlsafe(32)
        self._validation_binding_tokens[handle.session_id] = self._fingerprint(token)
        return handle, ValidationSessionBinding(
            session_id=handle.session_id,
            principal_alias=handle.principal_alias,
            authentication_generation=handle.auth_generation,
            _token=token,
        )

    def consume_validation_session_binding(
        self,
        binding: ValidationSessionBinding,
        session_id: str,
    ) -> bool:
        """Consume a validation-purpose binding without touching the browser."""
        if not isinstance(binding, ValidationSessionBinding) or binding.session_id != session_id:
            return False
        session = self._sessions.get(session_id)
        expected_token_hash = self._validation_binding_tokens.get(session_id)
        if (
            session is None
            or expected_token_hash is None
            or session.state != "ACTIVE"
            or session.binding is None
            or session.binding.alias != session.handle.principal_alias
            or session.binding.authentication_generation != session.handle.auth_generation
            or session.handle.principal_alias not in self._validation_principal_aliases
            or binding.principal_alias != session.handle.principal_alias
            or binding.authentication_generation != session.handle.auth_generation
        ):
            return False
        if not hmac.compare_digest(expected_token_hash, self._fingerprint(binding._token)):
            return False
        self._validation_binding_tokens.pop(session_id, None)
        return True

    async def get(self, session_id: str) -> ManagedSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionError("SESSION_LOST")
        if session.state != "ACTIVE" or session.binding is None:
            raise SessionError(session.state if session.state in {"SESSION_EXPIRED", "SUBJECT_MISMATCH"} else "SESSION_LOST", session_id)
        if session.page.is_closed() or not self._browser or not self._browser.is_connected():
            await self._invalidate(session, "SESSION_LOST")
            raise SessionError("SESSION_LOST", session_id)
        try:
            cookies = await self._cookies_fingerprint(session.context)
            if not hmac.compare_digest(cookies, session.cookie_fingerprint):
                await self._invalidate(session, "SESSION_EXPIRED")
                raise SessionError("SESSION_EXPIRED", session_id)
            await self._verify_visible_identity(session)
        except SessionError:
            raise
        except (PlaywrightError, PlaywrightTimeoutError):
            await self._invalidate(session, "SESSION_EXPIRED")
            raise SessionError("SESSION_EXPIRED", session_id) from None
        return session

    async def get_state(self, session_id: str) -> SessionState:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionError("SESSION_LOST")
        return SessionState(
            session_id=session.handle.session_id,
            principal_alias=session.handle.principal_alias,
            state=session.state,
            auth_generation=session.handle.auth_generation,
            profile_id=session.handle.profile_id,
            origin=session.handle.origin,
            browser_version=session.handle.browser_version,
            actor=session.actor,
        )

    async def reauthenticate_existing(
        self,
        session_id: str,
        expected_epoch: int,
    ) -> SessionHandle:
        """Reconcile authentication only inside the actor's fenced RESUMING phase.

        This method may use the existing page's visible login form. It never
        navigates, creates a context, or replaces the page during reauthentication.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionError("SESSION_LOST")

        async def reconcile() -> SessionHandle:
            # The actor owns this task even if its caller is canceled. Keep the
            # page lifecycle lock inside that actor-owned task so forced shutdown
            # cannot close the context while reconciliation is still running.
            async with self._lifecycle_lock:
                if self._sessions.get(session_id) is not session:
                    raise SessionError("SESSION_LOST", session_id)
                return await self._reauthenticate_in_place(session)

        return await session.actor.submit_reconciliation(
            expected_epoch=expected_epoch,
            run_id=session.actor.snapshot.active_run_id or "",
            operation=reconcile,
        )

    async def _reauthenticate_in_place(self, session: ManagedSession) -> SessionHandle:
        session_id = session.handle.session_id
        if (
            session.state in {"CLOSED", "SESSION_LOST"}
            or session.page.is_closed()
            or self._browser is None
            or not self._browser.is_connected()
        ):
            await self._invalidate(session, "SESSION_LOST")
            raise SessionError("SESSION_LOST", session_id)
        if not _same_origin(session.page.url, self.origin):
            await self._invalidate(session, "SESSION_EXPIRED")
            raise SessionError("SESSION_EXPIRED", session_id)
        if session.state == "SUBJECT_MISMATCH":
            raise SessionError("SUBJECT_MISMATCH", session_id)

        spec = self._specs.get(session.handle.principal_alias)
        if spec is None:
            raise SessionError("INPUT_INVALID", session_id)

        displayed: str | None
        try:
            displayed = await _read_display_name(session.page)
        except SessionError as error:
            if error.code != "SESSION_EXPIRED":
                raise
            displayed = None
        except (PlaywrightError, PlaywrightTimeoutError):
            await self._invalidate(session, "SESSION_EXPIRED")
            raise SessionError("SESSION_EXPIRED", session_id) from None

        if displayed is not None:
            if not hmac.compare_digest(
                self._fingerprint(_normalize_text(displayed)),
                self._fingerprint(_normalize_text(spec.expected_display_name)),
            ):
                await self._invalidate(session, "SUBJECT_MISMATCH")
                raise SessionError("SUBJECT_MISMATCH", session_id)
            if session.state != "ACTIVE" or session.binding is None:
                # A greeting alone is not enough to restore an expired auth proof.
                raise SessionError("SESSION_EXPIRED", session_id)
            try:
                current_cookies = await self._cookies_fingerprint(session.context)
            except (PlaywrightError, PlaywrightTimeoutError):
                await self._invalidate(session, "SESSION_EXPIRED")
                raise SessionError("SESSION_EXPIRED", session_id) from None
            if (
                session.binding.alias != spec.alias
                or session.binding.login_provenance != "VISIBLE_UI_LOGIN"
                or session.binding.authentication_generation != session.handle.auth_generation
                or not hmac.compare_digest(
                    self._fingerprint(_normalize_text(displayed)),
                    session.binding.display_name_fingerprint,
                )
                or not hmac.compare_digest(current_cookies, session.cookie_fingerprint)
                or not hmac.compare_digest(current_cookies, session.binding.cookie_fingerprint)
            ):
                await self._invalidate(session, "SESSION_EXPIRED")
                raise SessionError("SESSION_EXPIRED", session_id)
            return session.handle

        if session.state == "ACTIVE":
            await self._invalidate(session, "SESSION_EXPIRED")
        if not await self._login_form_ready(session.page):
            raise SessionError("SESSION_EXPIRED", session_id)

        username, password = self._credentials(spec)
        old_context, old_page = session.context, session.page
        try:
            await self._ui_login(session, spec, username, password, navigate=False)
            if session.context is not old_context or session.page is not old_page:
                await self._invalidate(session, "SESSION_LOST")
                raise SessionError("SESSION_LOST", session_id)
            if session.state != "ACTIVE" or session.binding is None:
                raise SessionError("SESSION_EXPIRED", session_id)
            if session.binding.alias != spec.alias:
                await self._invalidate(session, "SUBJECT_MISMATCH")
                raise SessionError("SUBJECT_MISMATCH", session_id)
            return session.handle
        except SessionError:
            raise
        except (PlaywrightError, PlaywrightTimeoutError):
            await self._invalidate(session, "SESSION_EXPIRED")
            raise SessionError("SESSION_EXPIRED", session_id) from None
        finally:
            username = None
            password = None

    async def close(self, session_id: str, *, allow_active: bool = False) -> None:
        async with self._lifecycle_lock:
            await self._close_locked(session_id, allow_active=allow_active)

    async def _close_locked(self, session_id: str, *, allow_active: bool = False) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.actor.active_run_id is not None and not allow_active:
            raise SessionError("SESSION_BUSY", session_id)
        self._sessions.pop(session_id, None)
        self._validation_binding_tokens.pop(session_id, None)
        session.state = "CLOSED"
        await _close_context(session.context)
        _release_lock(session.target_lock_fd)
        if not self._sessions:
            await self._close_browser()

    async def close_all(self) -> None:
        async with self._lifecycle_lock:
            self._closed = True
            for session_id in tuple(self._sessions):
                await self._close_locked(session_id, allow_active=True)
            await self._close_browser()

    async def _ui_login(
        self,
        session: ManagedSession,
        spec: PrincipalSpec,
        username: str,
        password: str,
        *,
        navigate: bool = True,
    ) -> None:
        page = session.page
        if page.is_closed():
            raise SessionError("SESSION_LOST", session.handle.session_id)
        if navigate:
            await page.goto(self.base_url + ROUTES["home"], wait_until="domcontentloaded")
        elif not _same_origin(page.url, self.origin):
            raise SessionError("SESSION_EXPIRED", session.handle.session_id)
        login_rules = READINESS_RULES["LOGIN_READY"]
        if not all(rule in login_rules for rule in (
            "unique_visible:LOGIN_USERNAME",
            "unique_visible:LOGIN_PASSWORD",
            "unique_visible:LOGIN_SUBMIT",
            "visible:heading:Customer Login",
        )):
            raise SessionError("SESSION_EXPIRED")
        username_locator = page.locator(LOCATORS["LOGIN_USERNAME"]["selector"])
        password_locator = page.locator(LOCATORS["LOGIN_PASSWORD"]["selector"])
        submit_locator = page.locator(LOCATORS["LOGIN_SUBMIT"]["selector"])
        try:
            if not await self._login_form_ready(page):
                raise SessionError("SESSION_EXPIRED", session.handle.session_id)
            for locator in (username_locator, password_locator, submit_locator):
                await locator.wait_for(state="visible", timeout=self._timeout_ms)
                if await locator.count() != 1:
                    raise SessionError("SESSION_EXPIRED")
            await username_locator.fill(username)
            await password_locator.fill(password)
            await submit_locator.click()
            await page.locator(LOCATORS["AUTHENTICATED_GREETING"]["selector"]).wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )
            displayed = await _read_display_name(page)
        except SessionError:
            raise
        except (PlaywrightError, PlaywrightTimeoutError):
            raise SessionError("SESSION_EXPIRED") from None
        if _normalize_text(displayed) != _normalize_text(spec.expected_display_name):
            await self._invalidate(session, "SUBJECT_MISMATCH")
            raise SessionError("SUBJECT_MISMATCH", session.handle.session_id)
        cookie_fingerprint = await self._cookies_fingerprint(session.context)
        self._validation_binding_tokens.pop(session.handle.session_id, None)
        generation = session.handle.auth_generation + 1
        session.handle = replace(session.handle, auth_generation=generation)
        session.binding = PrincipalBinding(
            alias=spec.alias,
            authentication_generation=generation,
            login_provenance="VISIBLE_UI_LOGIN",
            username_fingerprint=self._fingerprint(username),
            display_name_fingerprint=self._fingerprint(_normalize_text(displayed)),
            cookie_fingerprint=cookie_fingerprint,
        )
        session.cookie_fingerprint = cookie_fingerprint
        session.handle = replace(session.handle, origin=_authority(page.url))
        session.state = "ACTIVE"
        displayed = ""

    async def _login_form_ready(self, page: Page) -> bool:
        if page.is_closed() or not _same_origin(page.url, self.origin):
            return False
        required_rules = (
            "unique_visible:LOGIN_USERNAME",
            "unique_visible:LOGIN_PASSWORD",
            "unique_visible:LOGIN_SUBMIT",
            "visible:heading:Customer Login",
        )
        if not all(rule in READINESS_RULES["LOGIN_READY"] for rule in required_rules):
            return False
        try:
            for key in ("LOGIN_USERNAME", "LOGIN_PASSWORD", "LOGIN_SUBMIT"):
                locator = page.locator(LOCATORS[key]["selector"])
                if await locator.count() != 1 or not await locator.is_visible():
                    return False
            heading = page.get_by_role("heading", name="Customer Login", exact=True)
            return await heading.count() == 1 and await heading.is_visible()
        except (PlaywrightError, PlaywrightTimeoutError):
            return False

    async def _verify_visible_identity(self, session: ManagedSession) -> None:
        if session.binding is None:
            await self._invalidate(session, "SESSION_EXPIRED")
            raise SessionError("SESSION_EXPIRED", session.handle.session_id)
        try:
            displayed = await _read_display_name(session.page)
        except (PlaywrightError, PlaywrightTimeoutError):
            await self._invalidate(session, "SESSION_EXPIRED")
            raise SessionError("SESSION_EXPIRED", session.handle.session_id) from None
        if not hmac.compare_digest(
            self._fingerprint(_normalize_text(displayed)),
            session.binding.display_name_fingerprint,
        ):
            await self._invalidate(session, "SUBJECT_MISMATCH")
            raise SessionError("SUBJECT_MISMATCH", session.handle.session_id)
        displayed = ""

    async def _invalidate(self, session: ManagedSession, reason: str) -> None:
        if session.state == "CLOSED":
            return
        if session.state == "SESSION_LOST" and reason != "CLOSED":
            return
        if session.state == "SUBJECT_MISMATCH" and reason not in {"SESSION_LOST", "CLOSED"}:
            return
        if session.state == reason:
            return
        self._validation_binding_tokens.pop(session.handle.session_id, None)
        session.state = reason
        session.binding = None
        session.handle = replace(
            session.handle,
            auth_generation=session.handle.auth_generation + 1,
        )
        session.cookie_fingerprint = ""

    def _track_navigation(self, session: ManagedSession) -> None:
        page = session.page

        def on_navigate(frame) -> None:
            if frame == page.main_frame:
                session.document_generation += 1
                session.frame_generations["f_main"] = session.document_generation
                if not _same_origin(frame.url, self.origin):
                    self._validation_binding_tokens.pop(session.handle.session_id, None)
                    session.state = "SESSION_EXPIRED"
                    session.binding = None
                    session.handle = replace(
                        session.handle,
                        auth_generation=session.handle.auth_generation + 1,
                    )

        page.on("framenavigated", on_navigate)

    async def _cookies_fingerprint(self, context: BrowserContext) -> str:
        cookies = await context.cookies([self.base_url])
        normalized = []
        for cookie in cookies:
            normalized.append({
                "name": self._fingerprint(str(cookie.get("name", ""))),
                "value": self._fingerprint(str(cookie.get("value", ""))),
                "domain": str(cookie.get("domain", "")),
                "path": str(cookie.get("path", "")),
                "httpOnly": bool(cookie.get("httpOnly")),
                "secure": bool(cookie.get("secure")),
                "sameSite": str(cookie.get("sameSite", "")),
                "expires": cookie.get("expires"),
            })
        normalized.sort(key=lambda item: (item["domain"], item["path"], item["name"]))
        return self._fingerprint(json.dumps(normalized, sort_keys=True, separators=(",", ":")))

    def _fingerprint(self, value: str) -> str:
        return hmac.new(self._fingerprint_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _credentials(self, spec: PrincipalSpec) -> tuple[str, str]:
        username = os.environ.get(spec.username_env, "")
        password = os.environ.get(spec.password_env, "")
        if not username.strip() or not password or len(username) > 20 or len(password) > 20:
            raise SessionError("INPUT_INVALID")
        return username, password

    async def _get_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                channel=self._channel,
                headless=self._headless,
            )
        except PlaywrightError:
            await self._close_browser()
            raise SessionError("SESSION_LOST") from None
        return self._browser

    async def _close_browser(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            try:
                await browser.close()
            except PlaywrightError:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except PlaywrightError:
                pass


def _validate_origin(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        raise ValueError("origin is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.username is not None
        or parsed.password is not None
        or port != 8080
        or parsed.path != "/parabank"
        or parsed.query
        or parsed.fragment
        or "?" in origin
        or "#" in origin
    ):
        raise ValueError("origin must be the loopback ParaBank test target")
    return "http://{}:8080/parabank".format(parsed.hostname)


def _authority(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.port != 8080
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("page origin is outside the loopback ParaBank target")
    return "http://{}:8080".format(parsed.hostname)


def _same_origin(url: str, origin: str) -> bool:
    try:
        parsed, expected = urlsplit(url), urlsplit(origin)
        if parsed.username is not None or parsed.password is not None:
            return False
        return (parsed.scheme, parsed.hostname, parsed.port) == (
            expected.scheme,
            expected.hostname,
            expected.port,
        )
    except ValueError:
        return False


async def _read_display_name(page: Page) -> str:
    definition = LOCATORS["AUTHENTICATED_GREETING"]
    container = page.locator(definition["selector"])
    marker = page.locator(definition["marker_selector"])
    if (
        await container.count() != 1
        or await marker.count() != 1
        or not await container.is_visible()
        or not await marker.is_visible()
    ):
        raise SessionError("SESSION_EXPIRED")
    marker_text = _normalize_text(await marker.inner_text())
    full_text = _normalize_text(await container.inner_text())
    if marker_text != definition["marker_text"] or not full_text.startswith(marker_text):
        raise SessionError("SESSION_EXPIRED")
    name = full_text[len(marker_text):].strip()
    if not name:
        raise SessionError("SESSION_EXPIRED")
    return name


async def _close_context(context: BrowserContext) -> None:
    try:
        await context.close()
    except PlaywrightError:
        pass


def _acquire_shared_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise SessionError("SESSION_EXPIRED")
    return descriptor


def _release_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _normalize_text(value: str) -> str:
    return " ".join(value.split())
