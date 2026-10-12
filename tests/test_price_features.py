import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from worldmodel_data.price_features import compile_price_features, price_pair_correlation


class FeatureTests(unittest.TestCase):
    def test_deposit_rent_tradeoff_and_constant_rent(self):
        self.assertEqual(price_pair_correlation([100, 200, 300], [90, 60, 30]), -1)
        self.assertIsNone(price_pair_correlation([100, 200, 300], [0, 0, 0]))

    def package(self, root, missing_amount=False):
        root.mkdir()
        rows = [{"deposit_manwon": None if missing_amount else 1000, "monthly_rent_manwon": 0,
                 "contract_date": "2026-10-01", "lawd_code": "11110", "source": "single_multi",
                 "lease_type": "jeonse", "run_id": "r1", "reported_area_sqm": 40,
                 "reported_area_basis": "source_totalFloorAr"} for _ in range(10)]
        rows += [{**rows[0], "lawd_code": "11140"}]
        raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
        (root / "rows.jsonl").write_bytes(raw)
        (root / "inventory.json").write_text(json.dumps({"datasets": [{
            "relative_path": "rows.jsonl", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "record_count": 11, "run_id": "r1", "observed_at": "2026-10-12T00:00:00Z",
            "counts_by_contract_month": {"202610": 11}}]}), encoding="utf-8")

    def test_zero_rent_area_semantics_suppression_and_conservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.package(root / "package")
            result = compile_price_features(root / "package", root / "features")
            self.assertEqual((result["represented_source_rows"], result["suppressed_source_rows"]), (10, 1))
            row = json.loads((root / "features/market-features.json").read_text(encoding="utf-8"))["records"][0]
            self.assertEqual(row["monthly_rent_median_manwon"], 0)
            self.assertIsNone(row["exclusive_area_median_sqm"])
            self.assertFalse(row["contract_month_finished_at_observation"])
            with self.assertRaises(FileExistsError):
                compile_price_features(root / "package", root / "features")

    def test_missing_amount_does_not_become_zero_feature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.package(root / "package", missing_amount=True)
            with self.assertRaises(ValueError):
                compile_price_features(root / "package", root / "features")
