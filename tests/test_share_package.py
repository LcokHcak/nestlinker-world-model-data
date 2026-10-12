import unittest
import hashlib
import tempfile
import zipfile
from pathlib import Path
from scripts.build_rtms_share_package import scope_from_run, verify_archive


class PackageScopeTests(unittest.TestCase):
    def test_fixed_month_outside_rolling_window_preserves_october(self):
        months = ["202510"] + [f"2026{month:02d}" for month in range(1, 11)] + ["202511", "202512"]
        label, start, end = scope_from_run({"config": {"contract_months": months}})
        self.assertEqual((label, start, end), ("2025-10_to_2026-10", "2025-10", "2026-10"))

    def test_missing_month_cannot_be_claimed_as_contiguous_coverage(self):
        with self.assertRaises(ValueError):
            scope_from_run({"config": {"contract_months": ["202501", "202503"]}})

    def test_archive_hash_and_actual_row_counts_gate_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bundle.zip"
            payload = b'{"amount":0}\n'
            checksum = hashlib.sha256(payload).hexdigest()
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("pkg/data/records.jsonl", payload)
                bundle.writestr("pkg/SHA256SUMS.txt", checksum + "  data/records.jsonl\n")
            inventory = [{"relative_path": "data/records.jsonl", "record_count": 1}]
            verify_archive(archive, "pkg", inventory, [checksum + "  data/records.jsonl"])
            inventory[0]["record_count"] = 2
            with self.assertRaises(ValueError):
                verify_archive(archive, "pkg", inventory, [checksum + "  data/records.jsonl"])
            with self.assertRaises(ValueError):
                verify_archive(archive, "pkg", inventory, ["0" * 64 + "  data/records.jsonl"])
