"""Seed the pinned ParaBank target with four synthetic test customers.

Username/password values are read from the named environment variables only;
they are never written to the generated fixture manifest or printed.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener, urlopen
from http.cookiejar import CookieJar

from testbed.parabank import CACHE, DEFAULT_ORIGIN, TestbedError, UPSTREAM_COMMIT, _origin, check_health


FIXTURE_USERS = (
    {"alias": "alpha", "username_env": "PARABANK_DEMO_ALPHA_USERNAME", "password_env": "PARABANK_DEMO_ALPHA_PASSWORD", "savings": 2},
    {"alias": "beta", "username_env": "PARABANK_DEMO_BETA_USERNAME", "password_env": "PARABANK_DEMO_BETA_PASSWORD", "savings": 1},
    {"alias": "gamma", "username_env": "PARABANK_DEMO_GAMMA_USERNAME", "password_env": "PARABANK_DEMO_GAMMA_PASSWORD", "savings": 1},
    {"alias": "delta", "username_env": "PARABANK_DEMO_DELTA_USERNAME", "password_env": "PARABANK_DEMO_DELTA_PASSWORD", "savings": 1},
)
_CUSTOMER_ID = re.compile(r"services_proxy/bank/customers/(\d+)/accounts")
_ACCOUNT_ID = re.compile(r"^[0-9]{1,20}$", re.ASCII)
_BASELINE_ACCOUNTS = (
    {"account_id": "12345", "customer_id": "12212", "type": "CHECKING", "balance_sign": "negative", "expected_available_balance": "0.00"},
    {"account_id": "12678", "customer_id": "12212", "type": "SAVINGS", "balance_sign": "negative", "expected_available_balance": "0.00"},
    {"account_id": "13344", "customer_id": "12212", "type": "SAVINGS", "balance_sign": "positive"},
)


def _request(opener, url: str, *, method: str = "GET", form: Optional[Mapping[str, str]] = None) -> bytes:
    data = urlencode(form).encode("utf-8") if form is not None else None
    request = Request(url, data=data, method=method, headers={
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        **({"Content-Type": "application/x-www-form-urlencoded"} if form is not None else {}),
    })
    try:
        with opener.open(request, timeout=20) as response:
            if response.status < 200 or response.status >= 300:
                raise TestbedError("local ParaBank fixture request failed")
            return response.read()
    except HTTPError as exc:
        raise TestbedError("local ParaBank fixture request failed (HTTP {})".format(exc.code)) from None
    except (URLError, TimeoutError, OSError):
        raise TestbedError("local ParaBank fixture request failed") from None


def _json_request(opener, url: str, *, method: str = "GET", form: Optional[Mapping[str, str]] = None) -> Any:
    raw = _request(opener, url, method=method, form=form)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TestbedError("local ParaBank returned an invalid fixture response") from None


def _load_credentials() -> List[Dict[str, str]]:
    missing = []
    configured = []
    for spec in FIXTURE_USERS:
        username = os.environ.get(spec["username_env"])
        password = os.environ.get(spec["password_env"])
        if not username or not username.strip():
            missing.append(spec["username_env"])
        if not password or not password.strip():
            missing.append(spec["password_env"])
        configured.append({"username": username or "", "password": password or ""})
    if missing:
        raise TestbedError("set required synthetic credential environment variables: " + ", ".join(missing))
    if any(len(entry["username"]) > 20 or len(entry["password"]) > 20 for entry in configured):
        raise TestbedError("upstream ParaBank limits fixture usernames and passwords to 20 characters")
    names = [entry["username"] for entry in configured]
    if len(set(names)) != len(names):
        raise TestbedError("the synthetic username environment variables must identify distinct principals")
    return configured


def _register_customer(origin: str, user: Mapping[str, str], index: int) -> Tuple[int, Any]:
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    _request(opener, origin + "/register.htm")
    profile = ("Alpha", "Beta", "Gamma", "Delta")[index]
    fields = {
        "customer.firstName": "Synthetic",
        "customer.lastName": profile,
        "customer.address.street": "100 Testbed Lane",
        "customer.address.city": "Test City",
        "customer.address.state": "TS",
        "customer.address.zipCode": "00000",
        "customer.phoneNumber": "555-010{}".format(index + 1),
        "customer.ssn": "900-00-000{}".format(index + 1),
        "customer.username": user["username"],
        "customer.password": user["password"],
        "repeatedPassword": user["password"],
    }
    registration = _request(opener, origin + "/register.htm", method="POST", form=fields)
    confirmation = b"Your account was created successfully. You are now logged in."
    if confirmation not in registration:
        raise TestbedError("synthetic registration was rejected by the upstream fixture form")
    open_account_page = _request(opener, origin + "/openaccount.htm").decode("utf-8", errors="replace")
    match = _CUSTOMER_ID.search(open_account_page)
    if match is None:
        raise TestbedError("synthetic registration did not create an authenticated ParaBank session")
    customer_id = int(match.group(1))
    return customer_id, opener


def _accounts_for(opener, origin: str, customer_id: int) -> List[Mapping[str, Any]]:
    data = _json_request(opener, "{}/services_proxy/bank/customers/{}/accounts".format(origin, customer_id))
    if not isinstance(data, list) or any(not isinstance(account, dict) for account in data):
        raise TestbedError("ParaBank account list did not match the expected shape")
    return data


def _create_savings(opener, origin: str, customer_id: int, checking_id: str) -> Mapping[str, Any]:
    query = urlencode({"customerId": customer_id, "newAccountType": 1, "fromAccountId": checking_id})
    account = _json_request(
        opener,
        "{}/services_proxy/bank/createAccount?{}".format(origin, query),
        method="POST",
    )
    if not isinstance(account, dict) or str(account.get("customerId")) != str(customer_id) or account.get("type") != "SAVINGS":
        raise TestbedError("ParaBank did not create the requested synthetic savings account")
    return account


def _baseline_cases(origin: str) -> List[Dict[str, str]]:
    opener = build_opener()
    accounts = []
    for expected in _BASELINE_ACCOUNTS:
        actual = _json_request(opener, "{}/services/bank/accounts/{}".format(origin, expected["account_id"]))
        if not isinstance(actual, dict):
            raise TestbedError("pinned ParaBank baseline account could not be verified")
        if str(actual.get("id")) != expected["account_id"] or str(actual.get("customerId")) != expected["customer_id"]:
            raise TestbedError("pinned ParaBank baseline account identity did not match the fixture")
        if actual.get("type") != expected["type"]:
            raise TestbedError("pinned ParaBank baseline account type did not match the fixture")
        try:
            balance = Decimal(str(actual.get("balance")))
        except (InvalidOperation, ValueError, TypeError):
            raise TestbedError("pinned ParaBank baseline balance could not be evaluated") from None
        if not balance.is_finite():
            raise TestbedError("pinned ParaBank baseline balance was not finite")
        if expected["balance_sign"] == "negative" and not balance < 0:
            raise TestbedError("pinned ParaBank negative-balance fixture is not present")
        if expected["balance_sign"] == "positive" and not balance > 0:
            raise TestbedError("pinned ParaBank positive-balance fixture is not present")
        accounts.append(dict(expected))
    return accounts


@contextmanager
def _exclusive_target_lock():
    """Exclude active runtime sessions while the fixture database is rebuilt.

    Runtime code must take a shared flock on this same path for each live
    session/run. The OS releases the lock automatically if either process exits.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    lock_path = CACHE / "target-use.lock"
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TestbedError("cannot reset ParaBank while runtime sessions or runs hold the shared target lock") from None
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def seed(*, manifest_path: Optional[Path] = None) -> Path:
    """Reset upstream fixtures and register this testbed's deterministic users."""
    origin = _origin()
    if not check_health():
        raise TestbedError("start ParaBank and wait for local HTTP 200 before seeding")
    credentials = _load_credentials()
    with _exclusive_target_lock():
        return _seed_locked(origin, credentials, manifest_path=manifest_path)


def deposit_savings_for_test(
    account_id: str,
    amount: str,
    *,
    manifest_path: Optional[Path] = None,
) -> None:
    __tracebackhide__ = True
    """Change one seeded synthetic savings balance for test-only freshness checks.

    The helper is intentionally separate from the runtime. It accepts only an
    account listed as SAVINGS in this seed's manifest, uses the pinned local
    origin, and takes the exclusive target lock so it cannot overlap live
    runtime sessions.
    """
    if not isinstance(account_id, str) or _ACCOUNT_ID.fullmatch(account_id) is None:
        raise TestbedError("synthetic savings account reference is invalid")
    if not isinstance(amount, str):
        raise TestbedError("synthetic deposit amount is invalid")
    try:
        parsed_amount = Decimal(amount)
    except (InvalidOperation, ValueError, TypeError):
        raise TestbedError("synthetic deposit amount is invalid") from None
    if (
        not parsed_amount.is_finite()
        or parsed_amount <= 0
        or parsed_amount.quantize(Decimal("0.01")) != parsed_amount
        or parsed_amount > Decimal("1000.00")
    ):
        raise TestbedError("synthetic deposit amount must be positive cents up to 1000.00")

    path = manifest_path or (CACHE / "seed_manifest.json")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise TestbedError("synthetic seed manifest is unavailable") from None
    if manifest.get("upstream_commit") != UPSTREAM_COMMIT:
        raise TestbedError("synthetic seed manifest is not pinned to the required target")
    if manifest.get("origin") != _origin():
        raise TestbedError("synthetic seed manifest origin does not match the local target")
    listed = any(
        isinstance(principal, dict)
        and isinstance(principal.get("accounts"), list)
        and any(
            isinstance(account, dict)
            and account.get("account_id") == account_id
            and account.get("type") == "SAVINGS"
            for account in principal["accounts"]
        )
        for principal in manifest.get("principals", ())
    )
    if not listed:
        raise TestbedError("account is not a synthetic savings fixture")
    origin = _origin()
    if not check_health():
        raise TestbedError("start ParaBank and wait for local HTTP 200 before depositing")

    with _exclusive_target_lock():
        opener = build_opener(ProxyHandler({}))
        backend_before = _json_request(
            opener,
            "{}/services/bank/accounts/{}".format(origin, account_id),
        )
        if (
            not isinstance(backend_before, dict)
            or str(backend_before.get("id")) != account_id
            or backend_before.get("type") != "SAVINGS"
        ):
            raise TestbedError("local backend account is not the seeded synthetic savings fixture")
        before = _decimal_backend_balance(backend_before.get("balance"))
        query = urlencode({"accountId": account_id, "amount": format(parsed_amount, ".2f")})
        _request(opener, "{}/services/bank/deposit?{}".format(origin, query), method="POST")
        backend_after = _json_request(
            opener,
            "{}/services/bank/accounts/{}".format(origin, account_id),
        )
        if (
            not isinstance(backend_after, dict)
            or str(backend_after.get("id")) != account_id
            or backend_after.get("type") != "SAVINGS"
            or _decimal_backend_balance(backend_after.get("balance")) != before + parsed_amount
        ):
            raise TestbedError("local synthetic savings deposit effect was not verified")


def _decimal_backend_balance(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise TestbedError("local backend balance is invalid") from None
    if not parsed.is_finite():
        raise TestbedError("local backend balance is invalid")
    return parsed


def _seed_locked(origin: str, credentials: List[Dict[str, str]], *, manifest_path: Optional[Path] = None) -> Path:
    path = manifest_path or (CACHE / "seed_manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    # initializeDB drops and recreates the pinned demo DB, which makes seed and
    # reset deterministic and restores the upstream negative-balance accounts.
    _request(build_opener(), origin + "/services/bank/initializeDB", method="POST")
    baseline_cases = _baseline_cases(origin)

    principals = []
    for index, (spec, user) in enumerate(zip(FIXTURE_USERS, credentials)):
        customer_id, opener = _register_customer(origin, user, index)
        accounts = _accounts_for(opener, origin, customer_id)
        checking = [account for account in accounts if account.get("type") == "CHECKING"]
        if not checking:
            raise TestbedError("registered synthetic customer does not have the expected checking account")
        checking_id = str(checking[0].get("id"))
        for _ in range(spec["savings"]):
            _create_savings(opener, origin, customer_id, checking_id)
        final_accounts = _accounts_for(opener, origin, customer_id)
        rendered_accounts = []
        for account in final_accounts:
            account_id = account.get("id")
            account_type = account.get("type")
            if account_id is None or account_type not in ("CHECKING", "SAVINGS"):
                raise TestbedError("registered synthetic account did not match the fixture shape")
            rendered_accounts.append({"account_id": str(account_id), "type": account_type})
        if sum(account["type"] == "SAVINGS" for account in rendered_accounts) < spec["savings"]:
            raise TestbedError("synthetic savings fixture count did not match the manifest")
        principals.append({
            "alias": spec["alias"],
            "customer_id": str(customer_id),
            "username_env": spec["username_env"],
            "password_env": spec["password_env"],
            "accounts": rendered_accounts,
        })

    manifest = {
        "schema_version": 1,
        "target": "official ParaBank",
        "upstream_commit": UPSTREAM_COMMIT,
        "origin": origin,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "credentials": "read from named environment variables; values omitted",
        "baseline_principal": {
            "alias": "baseline_negative_checking",
            "customer_id": "12212",
            "username_env": "PARABANK_BASELINE_USERNAME",
            "password_env": "PARABANK_BASELINE_PASSWORD",
        },
        "baseline_cases": baseline_cases,
        "principals": principals,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seed", "reset"))
    args = parser.parse_args(argv)
    try:
        manifest = seed()
    except TestbedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Synthetic ParaBank fixture ready; manifest: {}".format(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
