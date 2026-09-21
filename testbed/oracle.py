"""Testbed-only stdin oracle for the native ParaBank evaluator.

The application process never imports this module.  It is launched as a
separate executable by the server's configured oracle command and returns only
the small typed report understood by ``SubprocessValidationOracle``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .evaluator import EvaluationError, assert_result_matches_backend


_ACCOUNT = re.compile(r"^[0-9]{1,20}$", re.ASCII)
_ORIGIN = "http://127.0.0.1:8080/parabank"
_MAX_INPUT_BYTES = 65_536


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            return _report("ORACLE_UNAVAILABLE")
        payload = json.loads(raw.decode("utf-8"))
        result = payload["result"]
        fixture = payload["fixture"]
        account_id = fixture["account_id"]
        if not isinstance(result, dict) or not isinstance(fixture, dict):
            raise ValueError
        if not isinstance(account_id, str) or _ACCOUNT.fullmatch(account_id) is None:
            raise ValueError
        backend_account = _read_backend_account(account_id)
        assert_result_matches_backend(
            result,
            requested_account_id=account_id,
            backend_account=backend_account,
        )
    except EvaluationError as error:
        # Detailed evaluator reasons never cross the process boundary. The app
        # consumes one stable comparison outcome code.
        return _report("ORACLE_MISMATCH")
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError, URLError, OSError):
        return _report("ORACLE_UNAVAILABLE")
    return _report("ORACLE_PASS")


def _read_backend_account(account_id: str) -> dict[str, object]:
    origin = os.environ.get("PARABANK_ORIGIN", _ORIGIN).rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError
    request = Request(
        f"{origin}/services/bank/accounts/{account_id}",
        headers={"Accept": "application/json"},
    )
    with urlopen(request, timeout=5.0) as response:
        body = response.read(32_768)
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def _report(code: str) -> int:
    passed = code == "ORACLE_PASS"
    sys.stdout.write(json.dumps({"passed": passed, "code": code}, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
