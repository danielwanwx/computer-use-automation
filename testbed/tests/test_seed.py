import json
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler

from testbed import seed as seed_module
from testbed.parabank import UPSTREAM_COMMIT


class _Response:
    status = 200

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self._body


def test_synthetic_savings_deposit_uses_no_proxy_opener_and_verifies_change(
    tmp_path, monkeypatch
):
    origin = "http://127.0.0.1:8080/parabank"
    account_id = "55555"
    manifest_path = tmp_path / "seed.json"
    manifest_path.write_text(
        json.dumps({
            "upstream_commit": UPSTREAM_COMMIT,
            "origin": origin,
            "principals": [
                {"alias": "gamma", "accounts": [{"account_id": account_id, "type": "SAVINGS"}]}
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(seed_module, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(seed_module, "_origin", lambda: origin)
    monkeypatch.setattr(seed_module, "check_health", lambda: True)

    class _Opener:
        def __init__(self):
            self.account_reads = 0
            self.deposit_calls = 0

        def open(self, request, timeout):
            parsed = urlsplit(request.full_url)
            if parsed.netloc != "127.0.0.1:8080":
                raise AssertionError("test helper attempted a non-loopback request")
            if parsed.path.endswith("/services/bank/deposit"):
                query = parse_qs(parsed.query)
                if (
                    request.get_method() != "POST"
                    or query != {"accountId": [account_id], "amount": ["5.00"]}
                ):
                    raise AssertionError("test helper built an invalid deposit request")
                self.deposit_calls += 1
                return _Response(b"accepted")
            if parsed.path.endswith("/services/bank/accounts/" + account_id):
                balances = ("10.00", "15.00")
                balance = balances[self.account_reads]
                self.account_reads += 1
                return _Response(json.dumps({
                    "id": account_id,
                    "type": "SAVINGS",
                    "balance": balance,
                }).encode("utf-8"))
            raise AssertionError("test helper requested an unexpected local path")

    opener = _Opener()

    def build_local_opener(*handlers):
        if (
            len(handlers) != 1
            or not isinstance(handlers[0], ProxyHandler)
            or handlers[0].proxies
        ):
            raise AssertionError("test helper must disable proxies for loopback traffic")
        return opener

    monkeypatch.setattr(seed_module, "build_opener", build_local_opener)

    seed_module.deposit_savings_for_test(
        account_id,
        "5.00",
        manifest_path=manifest_path,
    )

    assert opener.account_reads == 2
    assert opener.deposit_calls == 1
