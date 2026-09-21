"""Stdlib command line client for the long-running CUA operator service.

The CLI is deliberately a client boundary.  It sends safe JSON requests to the
already running operator process and never composes an ApplicationService or
opens a browser in the CLI process.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import inspect
import json
import os
import secrets
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


_DEFAULT_URL = "http://127.0.0.1:8765"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CLIError(RuntimeError):
    """A safe, user-facing CLI failure without raw server or input values."""

    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _SafeHTTPError:
    status: int
    code: str


class OperatorClient:
    """Small HTTP client for the persistent operator service."""

    def __init__(self, base_url: str | None = None, *, token: str | None = None) -> None:
        self.base_url = _normalise_url(base_url or os.environ.get("CUA_OPERATOR_URL", _DEFAULT_URL))
        self._token_value = token

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        session_id: str | None = None,
    ) -> object:
        if not isinstance(path, str) or not path.startswith("/"):
            raise CLIError(400, "INPUT_INVALID")
        if method not in {"GET", "POST", "DELETE"}:
            raise CLIError(400, "INPUT_INVALID")
        encoded = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {_resolve_token(self)}",
            # The operator server binds this origin and checks it for browser
            # and non-browser writes alike.
            "Origin": self.base_url,
        }
        if payload is not None:
            try:
                encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError):
                raise CLIError(400, "INPUT_INVALID") from None
            headers["Content-Type"] = "application/json"
        if method in {"POST", "DELETE", "PATCH", "PUT"}:
            headers["X-CUA-CSRF"] = "same-origin"
        if session_id:
            headers["X-CUA-Session-ID"] = session_id
        request = Request(
            f"{self.base_url}{path}",
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30.0) as response:
                return _decode_response(response.read(_MAX_RESPONSE_BYTES + 1))
        except HTTPError as error:
            raise _safe_http_error(error) from None
        except (TimeoutError, URLError, OSError):
            raise CLIError(503, "OPERATOR_UNAVAILABLE") from None

    def list_sessions(self) -> object:
        return self.request("GET", "/api/sessions")

    def prepare_session(self, principal_ref: str, *, target_id: str = "parabank-local") -> object:
        return self.request(
            "POST",
            "/api/sessions",
            {"target_id": target_id, "principal_ref": principal_ref},
        )

    def discover(
        self,
        *,
        session_id: str,
        goal: str,
        account_id: str,
        request_id: str,
        capability_version: str = "1.0.0",
    ) -> object:
        return self.request(
            "POST",
            "/api/discovery",
            {
                "target_id": "parabank-local",
                "session_id": session_id,
                "goal": goal,
                "inputs": {"account_id": account_id},
                "request_id": request_id,
                "capability_version": capability_version,
            },
            session_id=session_id,
        )

    def list_capabilities(self) -> object:
        return self.request("GET", "/api/capabilities")

    def inspect_capability(self, *, name: str, version: str, digest: str) -> object:
        query = urlencode({"digest": digest})
        return self.request("GET", f"/api/capabilities/{name}/{version}?{query}")

    def validate_capability(
        self,
        *,
        name: str,
        version: str,
        digest: str,
        request_id: str,
    ) -> object:
        return self.request(
            "POST",
            f"/api/capabilities/{name}/{version}/validate",
            {"digest": digest, "request_id": request_id},
        )

    def approve_capability(
        self,
        *,
        name: str,
        version: str,
        digest: str,
        reviewer_ref: str,
        reviewer_type: str,
    ) -> object:
        return self.request(
            "POST",
            f"/api/capabilities/{name}/{version}/approve",
            {
                "digest": digest,
                "reviewer_ref": reviewer_ref,
                "reviewer_type": reviewer_type,
            },
        )

    def replay(
        self,
        *,
        session_id: str,
        name: str,
        version: str,
        digest: str,
        account_id: str,
        request_id: str,
    ) -> object:
        return self.request(
            "POST",
            "/api/invocations",
            {
                "reference": {"name": name, "version": version, "digest": digest},
                "session_id": session_id,
                "inputs": {"account_id": account_id},
                "request_id": request_id,
            },
            session_id=session_id,
        )

    def run_status(self, run_id: str) -> object:
        return self.request("GET", f"/api/runs/{run_id}")

    def result(self, run_id: str, *, session_id: str) -> object:
        return self.request("GET", f"/api/runs/{run_id}/result", session_id=session_id)


def build_service_from_environment(environ: dict[str, str] | None = None):
    """Compose the one server-owned service used by ``cua serve``.

    This import path is intentionally lazy: all other CLI commands remain an
    HTTP-only client and do not import browser, session, or provider code.
    """
    from cua.application.config import ApplicationConfig
    from cua.application.service import ApplicationService

    config = ApplicationConfig.from_environment(environ=environ)
    return ApplicationService.from_config(config), config


def _configured_or_generated_token(config) -> tuple[str, bool]:
    configured = getattr(config, "operator_token", None)
    if configured is not None:
        value = (
            configured.get_secret_value()
            if hasattr(configured, "get_secret_value")
            else configured
        )
        if isinstance(value, str) and value:
            return value, False
    return secrets.token_urlsafe(32), True


def _shutdown_service(service) -> None:
    async def close() -> None:
        result = service.shutdown()
        if inspect.isawaitable(result):
            await result

    try:
        asyncio.run(close())
    except CLIError:
        raise
    except Exception:
        raise CLIError(503, "SERVICE_SHUTDOWN_INCOMPLETE") from None


def serve() -> int:
    """Start the loopback operator server and cleanly drain it on exit."""
    service, config = build_service_from_environment()
    token, generated = _configured_or_generated_token(config)
    from cua.web import create_app
    import uvicorn

    if generated:
        # This is the only emission of an ephemeral token.  It is never put in
        # a URL, application config file, or persistent browser storage.
        print(f"CUA_OPERATOR_TOKEN={token}", file=sys.stderr, flush=True)
    operator_origin = getattr(
        config,
        "operator_origin",
        f"http://{config.operator_bind}:{config.operator_port}",
    )
    app = create_app(
        service,
        operator_token=token,
        operator_origin=operator_origin,
    )
    try:
        uvicorn.run(app, host=config.operator_bind, port=config.operator_port)
    finally:
        _shutdown_service(service)
    return 0


def _normalise_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise CLIError(400, "INPUT_INVALID")
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CLIError(400, "INPUT_INVALID")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise CLIError(400, "INPUT_INVALID")
    if parsed.path not in {"", "/"}:
        raise CLIError(400, "INPUT_INVALID")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise CLIError(403, "ORIGIN_NOT_ALLOWED")
    try:
        parsed.port
    except ValueError:
        raise CLIError(400, "INPUT_INVALID") from None
    return value.rstrip("/")


def _resolve_token(client: OperatorClient) -> str:
    if client._token_value is None:
        client._token_value = os.environ.get("CUA_OPERATOR_TOKEN") or getpass.getpass(
            "Operator token: "
        )
    if not isinstance(client._token_value, str) or not client._token_value:
        raise CLIError(401, "AUTH_REQUIRED")
    return client._token_value


def _decode_response(raw: bytes) -> object:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise CLIError(502, "RESPONSE_TOO_LARGE")
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CLIError(502, "INVALID_RESPONSE") from None


def _safe_http_error(error: HTTPError) -> CLIError:
    try:
        raw = error.read(_MAX_RESPONSE_BYTES + 1)
        body = _decode_response(raw)
    except Exception:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if not isinstance(code, str) or not code.isupper() or len(code) > 64:
        code = "REQUEST_REJECTED"
    return CLIError(error.code, code)


def _request_id(value: str | None) -> str:
    if value:
        return value
    return f"cli-{uuid.uuid4().hex}"


def _protected(value: str | None, prompt: str) -> str:
    if value:
        return value
    value = getpass.getpass(prompt)
    if not value:
        raise CLIError(422, "INPUT_INVALID")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cua", description="Client for the local CUA operator service")
    parser.add_argument(
        "--url",
        default=os.environ.get("CUA_OPERATOR_URL", _DEFAULT_URL),
        help="operator URL (default: CUA_OPERATOR_URL or local operator)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="start the loopback operator service")

    sessions = commands.add_parser("sessions", help="manage operator sessions")
    sessions_commands = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_commands.add_parser("list", help="list prepared sessions")
    prepare = sessions_commands.add_parser("prepare", help="prepare a browser session")
    prepare.add_argument("--principal", required=True, dest="principal_ref")
    prepare.add_argument("--target-id", default="parabank-local")

    discover = commands.add_parser("discover", help="start a discovery run")
    discover.add_argument("--session-id", required=True)
    discover.add_argument("--goal")
    discover.add_argument("--account-id")
    discover.add_argument("--request-id")
    discover.add_argument("--capability-version", default="1.0.0")

    capabilities = commands.add_parser("capabilities", help="inspect and review capabilities")
    capability_commands = capabilities.add_subparsers(dest="capability_command", required=True)
    capability_commands.add_parser("list", help="list capability revisions")
    for action in ("inspect", "validate"):
        command = capability_commands.add_parser(action, help=f"{action} a capability revision")
        command.add_argument("--name", required=True)
        command.add_argument("--version", required=True)
        command.add_argument("--digest", required=True)
        if action == "validate":
            command.add_argument("--request-id")
    approve = capability_commands.add_parser("approve", help="approve a validated revision")
    approve.add_argument("--name", required=True)
    approve.add_argument("--version", required=True)
    approve.add_argument("--digest", required=True)
    approve.add_argument("--reviewer-ref", default="local_operator")
    approve.add_argument("--reviewer-type", default="operator", choices=("operator", "independent_reviewer"))

    replay = commands.add_parser("replay", help="run an approved capability")
    replay.add_argument("--session-id", required=True)
    replay.add_argument("--name", required=True)
    replay.add_argument("--version", required=True)
    replay.add_argument("--digest", required=True)
    replay.add_argument("--account-id")
    replay.add_argument("--request-id")

    run = commands.add_parser("run", help="inspect a run")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    status = run_commands.add_parser("status", help="read safe run status")
    status.add_argument("run_id")
    result = run_commands.add_parser("result", help="explicitly read a completed result")
    result.add_argument("run_id")
    result.add_argument("--session-id", required=True)
    return parser


def _dispatch(client: OperatorClient, args: argparse.Namespace) -> object:
    if args.command == "sessions":
        if args.sessions_command == "list":
            return client.list_sessions()
        return client.prepare_session(args.principal_ref, target_id=args.target_id)
    if args.command == "discover":
        return client.discover(
            session_id=args.session_id,
            goal=_protected(args.goal, "Goal: "),
            account_id=_protected(args.account_id, "Account ID: "),
            request_id=_request_id(args.request_id),
            capability_version=args.capability_version,
        )
    if args.command == "capabilities":
        if args.capability_command == "list":
            return client.list_capabilities()
        if args.capability_command == "inspect":
            return client.inspect_capability(
                name=args.name, version=args.version, digest=args.digest
            )
        if args.capability_command == "validate":
            return client.validate_capability(
                name=args.name,
                version=args.version,
                digest=args.digest,
                request_id=_request_id(args.request_id),
            )
        return client.approve_capability(
            name=args.name,
            version=args.version,
            digest=args.digest,
            reviewer_ref=args.reviewer_ref,
            reviewer_type=args.reviewer_type,
        )
    if args.command == "replay":
        return client.replay(
            session_id=args.session_id,
            name=args.name,
            version=args.version,
            digest=args.digest,
            account_id=_protected(args.account_id, "Account ID: "),
            request_id=_request_id(args.request_id),
        )
    if args.run_command == "status":
        return client.run_status(args.run_id)
    return client.result(args.run_id, session_id=args.session_id)


def _print_json(value: object, *, stream) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    except (TypeError, ValueError):
        raise CLIError(502, "INVALID_RESPONSE") from None
    print(encoded, file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            return serve()
        value = _dispatch(OperatorClient(args.url), args)
        _print_json(value, stream=sys.stdout)
    except CLIError as error:
        _print_json({"code": error.code, "status": error.status}, stream=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        _print_json({"code": "INPUT_CANCELLED", "status": 499}, stream=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
