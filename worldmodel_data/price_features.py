"""Descriptive API price features, kept separate from annual-price model inputs."""
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .manifest import sha256_file
from .observation import _write_new_json


def quantile(values, fraction):
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    left, right = math.floor(index), math.ceil(index)
    return round(ordered[left] + (ordered[right] - ordered[left]) * (index - left), 4)


def finite_nonnegative(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def price_pair_correlation(deposits, rents):
    mean_deposit, mean_rent = sum(deposits) / len(deposits), sum(rents) / len(rents)
    left, right = [x - mean_deposit for x in deposits], [x - mean_rent for x in rents]
    denominator = math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))
    if denominator == 0:
        return None
    return round(max(-1, min(1, sum(x * y for x, y in zip(left, right)) / denominator)), 6)


def compile_price_features(package_dir, output_dir, minimum_count=10):
    package, output = Path(package_dir).resolve(), Path(output_dir).resolve()
    if isinstance(minimum_count, bool) or not isinstance(minimum_count, int) or minimum_count < 10:
        raise ValueError("minimum count must be at least 10")
    if output.exists():
        raise FileExistsError("feature output is immutable")
    inventory = json.loads((package / "inventory.json").read_text(encoding="utf-8"))
    grouped, inputs, seen_scopes = defaultdict(list), [], set()
    source_count = 0
    for dataset in inventory["datasets"]:
        path = (package / dataset["relative_path"]).resolve()
        if package not in path.parents or sha256_file(path) != dataset["sha256"] or path.stat().st_size != dataset["bytes"]:
            raise ValueError("input path/bytes/hash mismatch")
        scope = dataset.get("counts_by_contract_month")
        if not scope:
            raise ValueError("dataset lacks actual month coverage")
        if seen_scopes & set(scope):
            raise ValueError("overlapping observation versions are not extra price records")
        seen_scopes.update(scope)
        rows = 0
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                deposit, rent = row["deposit_manwon"], row["monthly_rent_manwon"]
                if not finite_nonnegative(deposit) or not finite_nonnegative(rent):
                    raise ValueError("invalid amounts cannot become zero price features")
                month = row["contract_date"][:7]
                if month.replace("-", "") not in scope or row["run_id"] != dataset["run_id"]:
                    raise ValueError("row does not belong to selected observed scope")
                lease = "monthly" if rent > 0 else "jeonse"
                if row["lease_type"] != lease:
                    raise ValueError("lease type inconsistent with explicit rent")
                key = (month, row["lawd_code"], row["source"], lease, dataset["run_id"], dataset["observed_at"])
                grouped[key].append((deposit, rent, row.get("exclusive_area_sqm"), row.get("floor"), row.get("build_year")))
                rows += 1
        if rows != dataset["record_count"]:
            raise ValueError("dataset row count mismatch")
        source_count += rows
        inputs.append({"path": dataset["relative_path"], "sha256": dataset["sha256"], "rows": rows,
                       "run_id": dataset["run_id"], "observed_at": dataset["observed_at"]})
    features, suppressed_rows, suppressed_groups = [], 0, 0
    for key, rows in sorted(grouped.items()):
        if len(rows) < minimum_count:
            suppressed_groups += 1
            suppressed_rows += len(rows)
            continue
        month, lawd, source, lease, run_id, observed = key
        deposits, rents = [row[0] for row in rows], [row[1] for row in rows]
        areas = [row[2] for row in rows if finite_nonnegative(row[2]) and row[2] > 0]
        build_years = [row[4] for row in rows if finite_nonnegative(row[4]) and row[4] > 0]
        item = {"contract_month": month, "lawd_code": lawd, "property_type": source, "lease_type": lease,
                "count": len(rows), "run_id": run_id, "observed_at": observed,
                "data_label": "derived_observed", "first_seen_at": None,
                "contract_month_finished_at_observation": month < observed[:7],
                "exclusive_area_observed_count": len(areas),
                "exclusive_area_median_sqm": quantile(areas, .5) if len(areas) >= minimum_count else None,
                "build_year_observed_count": len(build_years),
                "build_year_median": quantile(build_years, .5) if len(build_years) >= minimum_count else None,
                "deposit_rent_pearson": price_pair_correlation(deposits, rents),
                "floor_missing_count": sum(row[3] is None for row in rows),
                "usage": "descriptive_price_baseline_not_safety_score"}
        for name, values in (("deposit", deposits), ("monthly_rent", rents)):
            for suffix, fraction in (("p25", .25), ("median", .5), ("p75", .75)):
                item[f"{name}_{suffix}_manwon"] = quantile(values, fraction)
        features.append(item)
    if sum(item["count"] for item in features) + suppressed_rows != source_count:
        raise ValueError("feature count conservation failed")
    output.mkdir(parents=True)
    payload = {"schema_version": 1, "feature_version": "api-price-features-v1",
               "data_label": "derived_observed", "records": features}
    _write_new_json(output / "market-features.json", payload)
    summary = {"schema_version": 1, "feature_version": "api-price-features-v1",
               "created_at": datetime.now(timezone.utc).isoformat(),
               "input_inventory_sha256": sha256_file(package / "inventory.json"), "inputs": inputs,
               "source_record_count": source_count, "feature_group_count": len(features),
               "represented_source_rows": sum(row["count"] for row in features),
               "suppressed_groups": suppressed_groups, "suppressed_source_rows": suppressed_rows,
               "minimum_count": minimum_count, "public_release_eligible": False,
               "files": [{"path": "market-features.json", "sha256": sha256_file(output / "market-features.json")}],
               "compiler_sha256": sha256_file(Path(__file__)),
               "limitations": ["Source occurrences are not confirmed unique contracts.",
                               "Price quantiles preserve source multiplicity; no automatic exact-row deduplication.",
                               "Deposit and rent marginals do not recover their joint distribution.",
                               "Pair correlation retains one descriptive association, not a joint model or causal relationship.",
                               "A completed contract month does not imply all late reports have arrived.",
                               "Exclusive area is not substituted with totalFloorAr.",
                               "Historical as-of availability is unknown; observed_at is retained per cohort.",
                               "This is a local derived feature table, not admitted public snapshot or trained prediction.",
                               "model_v0 currently requires annual-price provenance; no source identity is spoofed."]}
    _write_new_json(output / "manifest.json", summary)
    return summary
