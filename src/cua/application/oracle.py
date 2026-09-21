"""Server-configured validation oracle transport.

The runtime sends a protected result and fixture binding to an explicitly
configured executable over stdin.  The executable is outside the application
process and its stdout is reduced to a small, typed :class:`OracleReport`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Sequence

from pydantic import SecretStr

from cua.application.contracts import OracleReport, ValidationFixture
from cua.execution.contracts import InvocationResult


class SubprocessValidationOracle:
    """Invoke a trusted, server-configured oracle without shell or network code."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 5.0,
        max_bytes: int = 65_536,
    ) -> None:
        command = tuple(command)
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("oracle command must be a nonempty argv sequence")
        if not 0.1 <= float(timeout_seconds) <= 30.0:
            raise ValueError("oracle timeout is outside the allowlist")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 256 <= max_bytes <= 1_048_576:
            raise ValueError("oracle output limit is outside the allowlist")
        self._command = command
        self._timeout_seconds = float(timeout_seconds)
        self._max_bytes = max_bytes

    async def check(self, result: InvocationResult, fixture: ValidationFixture) -> OracleReport:
        try:
            payload = _request_payload(result, fixture)
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > self._max_bytes:
                return OracleReport(passed=False, code="ORACLE_UNAVAILABLE")
        except (TypeError, ValueError, AttributeError):
            return OracleReport(passed=False, code="ORACLE_UNAVAILABLE")

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                close_fds=True,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(encoded + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            output, return_code = await asyncio.wait_for(
                _read_bounded_output(process, self._max_bytes),
                timeout=self._timeout_seconds,
            )
            if return_code != 0 or output is None or len(output) > self._max_bytes:
                return OracleReport(passed=False, code="ORACLE_UNAVAILABLE")
            return _parse_report(output)
        except (asyncio.TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return OracleReport(passed=False, code="ORACLE_UNAVAILABLE")
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await process.wait()
                except Exception:
                    pass


async def _read_bounded_output(process, limit: int):
    """Read at most ``limit + 1`` bytes and wait for exit."""
    assert process.stdout is not None
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = await process.stdout.read(min(8192, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        if sum(map(len, chunks)) > limit:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            break
    output = b"".join(chunks)
    return output, await process.wait()


def _request_payload(result: InvocationResult, fixture: ValidationFixture) -> dict[str, object]:
    if not isinstance(result, InvocationResult) or not isinstance(fixture, ValidationFixture):
        raise ValueError("oracle request types are invalid")
    outputs = None
    if result.outputs is not None:
        outputs = {
            key: value.get_secret_value()
            for key, value in sorted(result.outputs.items())
            if isinstance(value, SecretStr)
        }
    failure = None
    if result.failure is not None:
        failure = {
            "reason_code": result.failure.reason_code.value,
            "effect_state": result.failure.effect_state.value
            if result.failure.effect_state is not None
            else None,
        }
    return {
        "result": {
            "status": result.status.value,
            "code": result.code.value if result.code is not None else None,
            "outputs": outputs,
            "failure": failure,
        },
        "fixture": {
            "principal_alias": fixture.principal_alias,
            "account_id": fixture.account_id.get_secret_value(),
        },
    }


def _parse_report(output: bytes) -> OracleReport:
    try:
        value = json.loads(output.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return OracleReport(passed=value.get("passed"), code=value.get("code"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return OracleReport(passed=False, code="ORACLE_UNAVAILABLE")
