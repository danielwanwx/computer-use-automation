import fcntl
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from testbed.parabank import TestbedError
from testbed.seed import _exclusive_target_lock


class TargetResetLockTests(TestCase):
    def test_reset_exclusive_lock_refuses_an_active_shared_runtime_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("testbed.seed.CACHE", Path(directory)):
                lock_path = Path(directory) / "target-use.lock"
                descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    with self.assertRaises(TestbedError):
                        with _exclusive_target_lock():
                            self.fail("reset lock unexpectedly coexisted with an active runtime lock")
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)

                with _exclusive_target_lock():
                    pass
