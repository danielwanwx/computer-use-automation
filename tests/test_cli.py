import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import pytest

from cua.cli import main


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls: list[tuple[str, str, dict[str, object] | None, dict[str, str]]] = []

    def _write(self, status: int, body: object) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _record(self, payload: dict[str, object] | None = None) -> None:
        self.calls.append(
            (
                self.command,
                self.path,
                payload,
                {key.lower(): value for key, value in self.headers.items()},
            )
        )

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        path = urlsplit(self.path).path
        if path == "/api/sessions":
            self._write(200, [{"session_id": "s_0123456789abcdef"}])
        elif path == "/api/capabilities":
            self._write(200, [])
        elif path.startswith("/api/capabilities/"):
            self._write(200, {"reference": {"name": "get_savings_balance", "version": "1.0.0", "digest": "a" * 64}})
        elif path.endswith("/result"):
            self._write(200, {"run_id": "run_0123456789abcdef", "status": "SUCCESS"})
        elif path.startswith("/api/runs/"):
            self._write(200, {"run_id": "run_0123456789abcdef", "state": "SUCCESS"})
        else:
            self._write(404, {"code": "NOT_FOUND"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw) if raw else None
        self._record(payload)
        path = urlsplit(self.path).path
        if path == "/api/sessions":
            self._write(201, {"session_id": "s_0123456789abcdef"})
        elif path == "/api/discovery":
            self._write(202, {"run_id": "run_0123456789abcdef", "mode": "DISCOVERY"})
        elif path == "/api/invocations":
            self._write(202, {"run_id": "run_0123456789abcdef", "mode": "REPLAY"})
        elif path.endswith("/validate"):
            self._write(202, {"run_id": "run_0123456789abcdef", "mode": "VALIDATION"})
        elif path.endswith("/approve"):
            self._write(200, {"status": "APPROVED"})
        else:
            self._write(404, {"code": "NOT_FOUND"})

    def log_message(self, *_args) -> None:
        return None


@pytest.fixture
def mock_server():
    _Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _Handler.calls
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _env(monkeypatch, base_url: str) -> None:
    monkeypatch.setenv("CUA_OPERATOR_URL", base_url)
    monkeypatch.setenv("CUA_OPERATOR_TOKEN", "test-token")


def test_cli_commands_use_http_server_and_emit_safe_json(mock_server, monkeypatch, capsys):
    base_url, calls = mock_server
    _env(monkeypatch, base_url)

    assert main(["sessions", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["session_id"] == "s_0123456789abcdef"

    assert main(["sessions", "prepare", "--principal", "synthetic_alpha"]) == 0
    assert main(
        [
            "discover",
            "--session-id",
            "s_0123456789abcdef",
            "--goal",
            "Get savings balance",
            "--account-id",
            "7048162359",
        ]
    ) == 0
    assert main(
        [
            "capabilities",
            "inspect",
            "--name",
            "get_savings_balance",
            "--version",
            "1.0.0",
            "--digest",
            "a" * 64,
        ]
    ) == 0
    assert main(
        [
            "capabilities",
            "validate",
            "--name",
            "get_savings_balance",
            "--version",
            "1.0.0",
            "--digest",
            "a" * 64,
        ]
    ) == 0
    assert main(
        [
            "capabilities",
            "approve",
            "--name",
            "get_savings_balance",
            "--version",
            "1.0.0",
            "--digest",
            "a" * 64,
        ]
    ) == 0
    assert main(
        [
            "replay",
            "--session-id",
            "s_0123456789abcdef",
            "--name",
            "get_savings_balance",
            "--version",
            "1.0.0",
            "--digest",
            "a" * 64,
            "--account-id",
            "7048162359",
        ]
    ) == 0
    assert main(["run", "status", "run_0123456789abcdef"]) == 0
    assert main(
        ["run", "result", "run_0123456789abcdef", "--session-id", "s_0123456789abcdef"]
    ) == 0

    assert len(calls) == 9
    assert all(call[3]["authorization"] == "Bearer test-token" for call in calls)
    assert calls[2][2]["inputs"] == {"account_id": "7048162359"}
    assert calls[6][2]["reference"]["digest"] == "a" * 64
    assert calls[8][3]["x-cua-session-id"] == "s_0123456789abcdef"


def test_cli_reads_token_from_secure_prompt_when_env_missing(mock_server, monkeypatch, capsys):
    base_url, _ = mock_server
    monkeypatch.setenv("CUA_OPERATOR_URL", base_url)
    monkeypatch.delenv("CUA_OPERATOR_TOKEN", raising=False)
    monkeypatch.setattr("cua.cli.getpass.getpass", lambda _prompt: "prompt-token")

    assert main(["sessions", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["session_id"] == "s_0123456789abcdef"


def test_cli_reports_only_safe_server_error(mock_server, monkeypatch, capsys):
    base_url, _ = mock_server
    _env(monkeypatch, base_url)
    # The mock's missing route returns only a stable server code.  The CLI must
    # preserve that code and avoid echoing request values or exception details.
    from cua.cli import OperatorClient, CLIError

    client = OperatorClient(base_url, token="test-token")
    with pytest.raises(CLIError) as error:
        client.request("GET", "/api/private?account_id=7048162359")
    assert error.value.code == "NOT_FOUND"
    assert error.value.status == 404


def test_cli_module_does_not_import_or_construct_application_service():
    project = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import cua.cli; print('cua.application.service' in sys.modules)",
        ],
        cwd=project,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "False"


def test_serve_composes_one_service_binds_loopback_and_shuts_down(monkeypatch, capsys):
    import cua.cli as cli

    class _Config:
        operator_bind = "127.0.0.1"
        operator_port = 8765
        operator_token = None

    class _Service:
        def __init__(self):
            self.shutdown_calls = 0

        async def shutdown(self):
            self.shutdown_calls += 1

    service = _Service()
    composed = []
    uvicorn_calls = []
    monkeypatch.setattr(cli, "build_service_from_environment", lambda: (composed.append(service) or (service, _Config())))
    monkeypatch.setattr("cua.web.create_app", lambda current, **kwargs: (current, kwargs))
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, **kwargs: uvicorn_calls.append((app, kwargs)),
    )

    assert main(["serve"]) == 0
    assert composed == [service]
    assert service.shutdown_calls == 1
    assert uvicorn_calls[0][1] == {"host": "127.0.0.1", "port": 8765}
    printed = capsys.readouterr().err
    assert printed.startswith("CUA_OPERATOR_TOKEN=")
    assert printed.count("CUA_OPERATOR_TOKEN=") == 1
