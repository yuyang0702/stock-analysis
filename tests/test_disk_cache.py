import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from a_share_strategy import DiskCache


class DiskCacheTest(unittest.TestCase):
    def test_save_retries_when_replace_is_temporarily_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DiskCache(Path(tmpdir) / "scan_cache.json")
            original_replace = Path.replace
            calls = {"count": 0}

            def flaky_replace(self, target):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise PermissionError("temporarily locked")
                return original_replace(self, target)

            with patch.object(Path, "replace", flaky_replace):
                cache.set("signal_anchor:600000:short", {"active": True})

            self.assertGreaterEqual(calls["count"], 2)
            self.assertEqual(cache.get("signal_anchor:600000:short", ttl_sec=60), {"active": True})

    def test_save_does_not_crash_when_cache_file_stays_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DiskCache(Path(tmpdir) / "scan_cache.json")

            with patch.object(Path, "replace", side_effect=PermissionError("locked")):
                cache.set("signal_anchor:600000:short", {"active": True})

            self.assertEqual(cache.db["signal_anchor:600000:short"]["value"], {"active": True})


if __name__ == "__main__":
    unittest.main()
