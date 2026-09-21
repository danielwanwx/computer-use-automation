import os
from unittest import TestCase
from unittest.mock import patch

from testbed.parabank import TestbedError, _origin


class OriginValidationTests(TestCase):
    def test_accepts_only_the_local_pinned_connector(self):
        for origin in (
            "http://127.0.0.1:8080/parabank",
            "http://localhost:8080/parabank/",
        ):
            with self.subTest(origin=origin), patch.dict(os.environ, {"PARABANK_ORIGIN": origin}):
                self.assertTrue(_origin().startswith("http://"))

    def test_rejects_remote_userinfo_ports_and_path_overrides(self):
        for origin in (
            "http://127.0.0.1:8080@evil.example/parabank",
            "http://user@127.0.0.1:8080/parabank",
            "http://127.0.0.1:8081/parabank",
            "http://127.0.0.1:8080/parabank?next=http://evil.example",
            "http://127.0.0.1:8080/parabank#fragment",
            "http://127.0.0.1:8080/parabank?",
            "http://127.0.0.1:8080/parabank#",
            "http://evil.example:8080/parabank",
        ):
            with self.subTest(origin=origin), patch.dict(os.environ, {"PARABANK_ORIGIN": origin}):
                with self.assertRaises(TestbedError):
                    _origin()
