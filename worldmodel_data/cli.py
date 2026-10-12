from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .manifest import file_entry, sha256_file, validate_snapshot
from .model_v0 import run_scenario_matrix, scenario_specification_sha256
from .observation import (
    audit_rtms_versions,
    collect_rtms_observation,
    load_observation_config,
    load_resume_config,
    observe_annual_archives,
)
from .replay import as_receipt_filter_counterfactual, audit_replay_inputs, run_historical_replay
from .rtms import fetch_rtms, rolling_months, service_key_from_env
from .seoul_rents import build_monthly_aggregates, load_acquisition_ledger, publish_monthly_snapshot

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog" / "datasets.json"

SEED_FILES = {
    "data/public-housing/listings.json": (
        "rtms-sample.json", "molit-rtms-rent", "https://www.data.go.kr/data/15126474/openapi.do",
        ["molit-rtms-officetel-rent", "molit-rtms-single-multi-rent"]
    ),
    "data/public-housing/summaries.json": (
        "rtms-summaries.json", "molit-rtms-rent", "https://www.data.go.kr/data/15126474/openapi.do",
        ["molit-rtms-officetel-rent", "molit-rtms-single-multi-rent"]
    ),
    "data/public-housing/demographics.json": (
        "demographics.json", "moj-foreign-residents", "https://www.immigration.go.kr/bbs/immigration/227/608718/artclView.do", ["mois-resident-population"]
    ),
    "data/public-housing/market-regions.json": (
        "market-regions.json", "mois-legal-dong-codes", "https://www.data.go.kr/data/15077871/openapi.do"
    ),
    "data/gosiwon/listings.json": (
        "seoul-gosiwon-registry.json", "seoul-gosiwon-fire-registry", "https://www.data.go.kr/data/15030030/fileData.do"
    ),
    "data/koreaSubway/catalog.json": (
        "korea-subway-stations.json", "korea-subway-cc0", "https://gist.github.com/nemorize/ac5f39ff62b6bf82dc496d10c69b2b46"
    ),
}

EXPECTED_INPUT_REPOSITORY = "https://github.com/sunruize93-cmyk/nest-linker"
DEMOGRAPHIC_FIELDS = {
    "guName", "lawdCode", "totalResidents", "koreanCount", "foreignCount",
    "maleCount", "femaleCount", "age",
}
GOSIWON_FIELDS = {
    "id", "name", "guName", "address", "roadAddress", "areaSqm", "floors", "reportedAt",
}
KOREAN_PHONE_PATTERN = (
    r"(?<![0-9A-Za-z])(?:01[016789][ -]?\d{3,4}[ -]?\d{4}"
    r"|0(?:2|[3-8]\d)[ -]?\d{3,4}[ -]?\d{4})(?![0-9A-Za-z])"
)
RTMS_SOURCE_BY_PROPERTY = {
    "apartment": "molit-rtms-rent",
    "officetel": "molit-rtms-officetel-rent",
    "single_multi": "molit-rtms-single-multi-rent",
    "rowhouse": "molit-rtms-rowhouse-rent",
}


def _git_value(source_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _validated_snapshot_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("snapshot date must be YYYY-MM-DD")
    return value


def _require_record_fields(records: object, allowed: set[str], label: str) -> list[dict]:
    if not isinstance(records, list) or not records:
        raise ValueError(f"{label}: expected a non-empty record list")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"{label}: record {index} must be an object")
        unexpected = sorted(set(record) - allowed)
        if unexpected:
            raise ValueError(f"{label}: record {index} has unexpected fields: {unexpected}")
    return records


def sanitize_seed_file(path: Path) -> dict | list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if path.name == "demographics.json":
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
            raise ValueError("demographics.json: invalid schema")
        sources = payload.get("sources")
        if not isinstance(sources, list) or not any("immigration.go.kr" in str(item.get("url")) for item in sources if isinstance(item, dict)):
            raise ValueError("demographics.json: official source metadata missing")
        districts = payload.get("districts")
        if not isinstance(districts, list):
            raise ValueError("demographics.json: districts missing")
        for district in districts:
            if not isinstance(district, dict):
                raise ValueError("demographics.json: district must be an object")
            district.pop("nationality", None)
            district.pop("otherCountries", None)
        _require_record_fields(districts, DEMOGRAPHIC_FIELDS, path.name)
        payload["privacyTransform"] = "Removed district-level nationality breakdowns and rare cells."
    elif path.name == "seoul-gosiwon-registry.json":
        if not isinstance(payload, dict) or payload.get("sourcePage") != "https://www.data.go.kr/data/15030030/fileData.do":
            raise ValueError("seoul-gosiwon-registry.json: official source metadata missing")
        records = _require_record_fields(payload.get("listings"), GOSIWON_FIELDS, path.name)
        for record in records:
            for field in ("name", "address", "roadAddress"):
                value = str(record.get(field) or "")
                value = re.sub(
                    rf"\([^()]*{KOREAN_PHONE_PATTERN}[^()]*\)",
                    "[redacted-contact]",
                    value,
                )
                value = re.sub(
                    rf"[가-힣A-Za-z0-9]+[ :/]*{KOREAN_PHONE_PATTERN}",
                    "[redacted-contact]",
                    value,
                )
                record[field] = re.sub(
                    KOREAN_PHONE_PATTERN, "[redacted-phone]", value
                ).strip()
            record["name"] = re.sub(r"\([^()]*대표[^()]*\)", "", record["name"]).strip()
            record["roadAddress"] = re.sub(r"\s*대표자(?:\s*우편)?\s*\([^)]*\).*$", "", record["roadAddress"]).strip()
            if re.search(r"대표자|귀하", f"{record['name']} {record['roadAddress']}"):
                raise ValueError("seoul-gosiwon-registry.json: representative detail survived privacy transform")
        payload["privacyTransform"] = "Removed phone-like values and representative names from free-text fields."
    elif path.name in {"rtms-sample.json", "rtms-summaries.json"}:
        if not isinstance(payload, dict) or payload.get("source") != "molit-rtms":
            raise ValueError(f"{path.name}: RTMS source metadata missing")
    elif path.name == "market-regions.json":
        _require_record_fields(payload, {"lawdCode", "sido", "cityKey", "guName", "zh", "ko", "en", "lat", "lng"}, path.name)
    elif path.name == "korea-subway-stations.json":
        if not isinstance(payload, dict) or "CC0" not in str(payload.get("source")):
            raise ValueError("korea-subway-stations.json: CC0 source metadata missing")
    else:
        raise ValueError(f"unsupported seed file: {path.name}")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def snapshot_file_contract(name: str, payload: dict | list) -> dict:
    common = {"data_label": "derived_observed"}
    if name == "rtms-sample.json":
        rows = payload["listings"]
        dates = sorted(str(row["dealDate"]) for row in rows)
        counts = {
            property_type: sum(1 for row in rows if row.get("propertyType") == property_type)
            for property_type in RTMS_SOURCE_BY_PROPERTY
        }
        composition = [
            {"property_type": property_type, "source_id": RTMS_SOURCE_BY_PROPERTY[property_type], "record_count": count}
            for property_type, count in counts.items() if count
        ]
        return {**common, "geography": "37 selected Korean districts", "temporal_coverage": {"start": dates[0], "end": dates[-1]}, "usage": "not_for_statistical_inference", "selection": "Latest 100 rows retained per district for UI and schema examples; truncated from 102,002 observed contracts.", "source_composition": composition, "limitations": ["Truncated recent sample; never estimate distributions, volume, availability, or future prices from this file."]}
    if name == "rtms-summaries.json":
        rows = payload["districts"]
        starts = sorted(str(row["minDealDate"]) for row in rows if row.get("minDealDate"))
        ends = sorted(str(row["maxDealDate"]) for row in rows if row.get("maxDealDate"))
        counts = {
            property_type: sum(int(row.get("byPropertyType", {}).get(property_type, {}).get("count", 0)) for row in rows)
            for property_type in RTMS_SOURCE_BY_PROPERTY
        }
        composition = [
            {"property_type": property_type, "source_id": RTMS_SOURCE_BY_PROPERTY[property_type], "record_count": count}
            for property_type, count in counts.items() if count
        ]
        return {**common, "geography": "38 selected Korean districts", "temporal_coverage": {"start": starts[0], "end": ends[-1]}, "usage": "aggregate_market_baseline", "based_on_record_count": sum(int(row.get("count", 0)) for row in rows), "source_composition": composition, "limitations": ["Selected districts and short time window; historical contracts are not current inventory."]}
    if name == "demographics.json":
        return {**common, "geography": "93 Korean districts", "temporal_coverage": {"as_of": payload["vintage"]}, "usage": "aggregate_context_only", "limitations": ["Reference populations differ; nationality details were removed and foreign-resident counts must not score neighborhood suitability."]}
    if name == "market-regions.json":
        return {
            **common,
            "data_label": "curated_reference",
            "geography": "93 Korean market-region join keys",
            "temporal_coverage": {"as_of": "source repository commit"},
            "usage": "join_keys_and_display_only_not_for_real_world_inference",
            "field_provenance": [
                {
                    "fields": ["lawdCode", "sido", "guName"],
                    "source_id": "mois-legal-dong-codes",
                    "method": "official legal-dong join keys curated into the input repository",
                },
                {
                    "fields": ["cityKey", "zh", "ko", "en", "lat", "lng"],
                    "source": "versioned manual curation in the manifest input repository/file",
                    "method": "display labels and approximate display centers; not official observations",
                },
            ],
            "limitations": [
                "Translations and coordinates are manually curated display metadata, not official observations.",
                "Display centers are not boundaries, parcel coordinates, routing evidence, or model targets.",
            ],
        }
    if name == "seoul-gosiwon-registry.json":
        return {**common, "geography": "Seoul", "temporal_coverage": {"extracted_at": payload["extractedAt"]}, "usage": "registry_lookup_not_inventory_or_safety_certification", "limitations": ["Registration is not current vacancy, price, habitability, or a safety certification."]}
    if name == "korea-subway-stations.json":
        return {**common, "geography": "major Korean metro systems", "temporal_coverage": {"as_of": "2025-08-12 plus documented manual patches"}, "usage": "prototype_only_requires_official_validation", "limitations": ["Community coordinates and manual patches require operator validation before routing."]}
    raise ValueError(f"missing file contract for {name}")


def load_catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def command_catalog(args: argparse.Namespace) -> int:
    datasets = load_catalog()["datasets"]
    if args.category:
        datasets = [item for item in datasets if item["category"] == args.category]
    for item in datasets:
        print(f"{item['id']}\t{item['category']}\t{item['status']}\t{item['title']}")
    return 0


def command_import(args: argparse.Namespace) -> int:
    source_root = Path(args.source_root).expanduser().resolve()
    snapshot_date = _validated_snapshot_date(args.snapshot_date)
    snapshot_parent = ROOT / "data" / "snapshots" / snapshot_date
    snapshot_dir = snapshot_parent / "initial-public-baseline"
    if snapshot_dir.exists():
        raise SystemExit(f"published snapshot is immutable: {snapshot_dir}")
    input_commit = _git_value(source_root, "rev-parse", "HEAD")
    input_remote = _git_value(source_root, "remote", "get-url", "origin").removesuffix(".git")
    if input_remote != EXPECTED_INPUT_REPOSITORY:
        raise SystemExit(f"unexpected input repository: {input_remote}")
    seed_paths = list(SEED_FILES)
    if _git_value(source_root, "status", "--porcelain", "--", *seed_paths):
        raise SystemExit("input data files differ from the recorded Git commit")
    snapshot_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".initial-public-baseline-", dir=snapshot_parent) as staging:
        staging_dir = Path(staging)
        entries = []
        input_files = []
        for relative, seed in SEED_FILES.items():
            target_name, source_id, source_url, *rest = seed
            secondary_source_ids = rest[0] if rest else None
            source = source_root / relative
            if not source.is_file() or source.is_symlink():
                raise SystemExit(f"missing or unsafe seed file: {source}")
            input_files.append({"path": relative, "sha256": sha256_file(source)})
            target = staging_dir / target_name
            shutil.copy2(source, target)
            payload = sanitize_seed_file(target)
            entry = file_entry(target, staging_dir, source_id, source_url, secondary_source_ids)
            entry.update(snapshot_file_contract(target_name, payload))
            entries.append(entry)
        manifest = {
            "schema_version": 1,
            "snapshot_id": f"initial-public-baseline-{snapshot_date}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data_label": "observed_derived_and_curated",
            "geography": "Korea market regions with a Seoul-first product focus",
            "derivation": "Imported from committed NestLinker public-data artifacts, preserved curated display metadata as non-observational, and passed schema/privacy transforms; no partner listing or user data included.",
            "input_repository": EXPECTED_INPUT_REPOSITORY,
            "input_commit": input_commit,
            "input_files": input_files,
            "transformation": {
                "command": f"python3 -m worldmodel_data import-nestlinker --source-root <repo> --snapshot-date {snapshot_date}",
                "version": "worldmodel-data-v1"
            },
            "limitations": [
                "RTMS rows are historical contracts, not currently available listings.",
                "Demographic statistics have different reference populations and dates.",
                "Gosiwon fire registration is not a safety certification or vacancy signal.",
                "Community subway coordinates require official validation for production routing."
            ],
            "files": entries,
        }
        (staging_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(staging_dir, snapshot_dir)
    print(snapshot_dir)
    return 0


def command_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    catalog = load_catalog()
    datasets = catalog.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        errors.append("catalog/datasets.json: datasets must be non-empty")
        datasets = []
    ids = [item.get("id") for item in datasets if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        errors.append("catalog/datasets.json: duplicate dataset id")
    required = {"id", "category", "title", "provider", "landing_url", "access", "license", "status", "privacy", "quality_notes"}
    for index, item in enumerate(datasets):
        if not isinstance(item, dict):
            errors.append(f"catalog dataset {index}: must be an object")
            continue
        missing = sorted(required - item.keys())
        if missing:
            errors.append(f"catalog dataset {item.get('id', index)}: missing {', '.join(missing)}")
        url = item.get("landing_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            errors.append(f"catalog dataset {item.get('id', index)}: landing_url must use https")
        historical_urls = item.get("historical_landing_urls", [])
        if (
            not isinstance(historical_urls, list)
            or any(not isinstance(value, str) or not value.startswith("https://") for value in historical_urls)
            or len(historical_urls) != len(set(historical_urls))
        ):
            errors.append(
                f"catalog dataset {item.get('id', index)}: historical_landing_urls must be unique https URLs"
            )
    snapshots_root = ROOT / "data" / "snapshots"
    manifests = sorted(snapshots_root.glob("*/*/manifest.json")) if snapshots_root.exists() else []
    if not manifests:
        errors.append("data/snapshots: no snapshot manifests found")
    known = {str(item["id"]): item for item in datasets if isinstance(item, dict) and item.get("id")}
    for manifest in manifests:
        errors.extend(validate_snapshot(manifest.parent, known))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: {len(datasets)} catalog sources, {len(manifests)} snapshot(s)")
    return 0


def command_fetch_rtms(args: argparse.Namespace) -> int:
    key = service_key_from_env()
    if not key:
        raise SystemExit("DATA_GO_KR_SERVICE_KEY is not set")
    if args.seoul_only:
        region_snapshot = _validated_snapshot_date(args.region_snapshot)
        regions = json.loads((ROOT / "data" / "snapshots" / region_snapshot / "initial-public-baseline" / "market-regions.json").read_text(encoding="utf-8"))
        lawd_codes = [str(item["lawdCode"]) for item in regions if item.get("sido") == "서울특별시"]
    else:
        lawd_codes = args.lawd
    if not lawd_codes:
        raise SystemExit("provide --lawd CODE or --seoul-only")
    result = fetch_rtms(
        service_key=key,
        lawd_codes=lawd_codes,
        months=rolling_months(args.months),
        output_dir=ROOT / "data" / "raw" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        delay=args.delay,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_export_rtms_observation(args: argparse.Namespace) -> int:
    from .observation_export import export_observation
    result = export_observation(Path(args.storage_root), args.run_id, Path(args.output_dir))
    print(json.dumps({"run_id": result["run_id"], "record_count": result["record_count"],
                      "public_release_eligible": False}, ensure_ascii=False))
    return 0


def command_compile_price_features(args: argparse.Namespace) -> int:
    from .price_features import compile_price_features
    result = compile_price_features(Path(args.input_package), Path(args.output_dir), args.minimum_count)
    print(json.dumps({key: result[key] for key in (
        "source_record_count", "feature_group_count", "represented_source_rows", "suppressed_source_rows"
    )}))
    return 0


def command_observe_rtms(args: argparse.Namespace) -> int:
    key = service_key_from_env()
    if not key:
        raise SystemExit("DATA_GO_KR_SERVICE_KEY is not set")
    current = datetime.now(timezone.utc)
    storage_root = Path(args.storage_root).expanduser().resolve()
    if args.resume:
        if not args.run_id:
            raise SystemExit("--resume requires --run-id")
        config = load_resume_config(storage_root, args.run_id)
    else:
        if not args.config:
            raise SystemExit("--config is required unless --resume is used")
        config = load_observation_config(
            Path(args.config).expanduser().resolve(),
            include_backfill=args.include_low_frequency,
            today=current.date(),
        )
    commit = _git_value(ROOT, "rev-parse", "HEAD")
    dirty = bool(_git_value(ROOT, "status", "--porcelain"))
    result = collect_rtms_observation(
        config=config,
        storage_root=storage_root,
        service_key=key,
        code_commit=commit,
        code_dirty=dirty,
        observed_at=current,
        run_id=args.run_id,
        resume=args.resume,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["version_complete"] else 2


def command_observe_annual_archives(args: argparse.Namespace) -> int:
    result = observe_annual_archives(
        input_dir=Path(args.input_dir).expanduser().resolve(),
        storage_root=Path(args.storage_root).expanduser().resolve(),
        run_id=args.run_id,
        code_commit=_git_value(ROOT, "rev-parse", "HEAD"),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_audit_rtms_versions(args: argparse.Namespace) -> int:
    report = audit_rtms_versions(
        storage_root=Path(args.storage_root).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        cadence_days=args.cadence_days,
    )
    print(Path(args.output_dir).expanduser().resolve() / "SUMMARY.md")
    return 0 if report["complete_run_count"] else 2


def command_publish_seoul_history(args: argparse.Namespace) -> int:
    snapshot_date = _validated_snapshot_date(args.snapshot_date)
    raw_dir = Path(args.raw_dir).expanduser().resolve()
    archives = {year: raw_dir / f"seoul-rents-{year}.zip" for year in args.years}
    if _git_value(ROOT, "status", "--porcelain"):
        raise SystemExit("refusing to publish from a dirty worktree; commit transformation code first")
    target = ROOT / "data" / "snapshots" / snapshot_date / "seoul-rental-history"
    publish_monthly_snapshot(
        archives,
        target,
        created_at=datetime.now(timezone.utc).isoformat(),
        input_commit=_git_value(ROOT, "rev-parse", "HEAD"),
        acquisitions=load_acquisition_ledger(Path(args.acquisition_ledger).expanduser().resolve()),
        audit_dir=Path(args.audit_dir).expanduser().resolve() if args.audit_dir else None,
    )
    print(target)
    return 0


def command_historical_replay(args: argparse.Namespace) -> int:
    snapshot_dir = Path(args.snapshot_dir).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise SystemExit(f"replay output is immutable: {output}")
    known_sources = {
        item["id"]: item for item in load_catalog()["datasets"]
        if isinstance(item, dict) and item.get("id")
    }
    validation_errors = validate_snapshot(snapshot_dir, known_sources)
    if validation_errors:
        raise SystemExit("snapshot validation failed: " + "; ".join(validation_errors))
    audit = audit_replay_inputs(snapshot_dir)
    if audit["statistical_replay"] != "supported":
        raise SystemExit(f"snapshot cannot support statistical replay: {audit['blockers']}")
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    data_entry = next(
        (entry for entry in manifest["files"] if entry.get("usage") == "historical_time_sliced_distribution"),
        None,
    )
    if data_entry is None:
        raise SystemExit("snapshot has no historical time-sliced distribution")
    data_path = snapshot_dir / data_entry["path"]
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    result = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "inputSnapshot": manifest["snapshot_id"],
        "inputSha256": sha256_file(data_path),
        "inputAudit": audit,
        "runs": [
            run_historical_replay(payload, min_group_count=value)
            for value in args.minimum_counts
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, output)
    print(output)
    return 0


def command_receipt_filter_sensitivity(args: argparse.Namespace) -> int:
    raw_dir = Path(args.raw_dir).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise SystemExit(f"sensitivity output is immutable: {output}")
    if _git_value(ROOT, "status", "--porcelain"):
        raise SystemExit("refusing to publish from a dirty worktree; commit transformation code first")
    archives = {year: raw_dir / f"seoul-rents-{year}.zip" for year in args.years}
    acquisitions = load_acquisition_ledger(Path(args.acquisition_ledger).expanduser().resolve())
    input_sources = []
    for year, archive in sorted(archives.items()):
        acquisition = acquisitions.get(year)
        if acquisition is None:
            raise SystemExit(f"missing acquisition record for {year}")
        digest = sha256_file(archive)
        if acquisition.get("sha256") != digest or acquisition.get("bytes") != archive.stat().st_size:
            raise SystemExit(f"acquisition record does not match archive for {year}")
        input_sources.append({
            "contractYear": year,
            "sha256": digest,
            "bytes": archive.stat().st_size,
            "retrievedAt": acquisition.get("retrieved_at"),
        })
    filtered_payload = build_monthly_aggregates(
        archives, minimum_group_count=10, exclude_receipt_year_mismatch=True
    )
    included_payload = build_monthly_aggregates(
        archives, minimum_group_count=10, exclude_receipt_year_mismatch=False
    )
    filtered = run_historical_replay(filtered_payload, min_group_count=args.minimum_count)
    included = as_receipt_filter_counterfactual(
        run_historical_replay(included_payload, min_group_count=args.minimum_count)
    )
    metrics = ("monthlyLeaseDeposit", "jeonseDeposit", "monthlyRent")
    comparisons = []
    for filtered_fold, included_fold in zip(filtered["folds"], included["folds"]):
        for metric in metrics:
            comparisons.append({
                "trainYear": filtered_fold["trainYear"],
                "testYear": filtered_fold["testYear"],
                "metric": metric,
                "wapeAbsoluteDifference": round(abs(
                    filtered_fold[metric]["weightedAbsolutePercentageError"]
                    - included_fold[metric]["weightedAbsolutePercentageError"]
                ), 4),
                "bandCoverageAbsoluteDifference": round(abs(
                    filtered_fold[metric]["medianBandCoverage"]
                    - included_fold[metric]["medianBandCoverage"]
                ), 4),
            })
    result = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "inputCommit": _git_value(ROOT, "rev-parse", "HEAD"),
        "inputSources": input_sources,
        "minimumGroupCount": args.minimum_count,
        "purpose": "receipt-year mismatch filter sensitivity only; not a safety outcome evaluation",
        "excludedReceiptYearMismatchCount": filtered_payload["excludedReceiptYearMismatch"],
        "excludedReceiptYearMismatchByContractMonth": filtered_payload[
            "excludedReceiptYearMismatchByContractMonth"
        ],
        "filtered": filtered,
        "includedForSensitivityOnly": included,
        "comparisons": comparisons,
        "maximumWapeAbsoluteDifference": max(item["wapeAbsoluteDifference"] for item in comparisons),
        "maximumBandCoverageAbsoluteDifference": max(
            item["bandCoverageAbsoluteDifference"] for item in comparisons
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, output)
    print(output)
    return 0


def command_minimum_world_model(args: argparse.Namespace) -> int:
    snapshot_dir = Path(args.snapshot_dir).expanduser().resolve()
    scenario_file = Path(args.scenario_file).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise SystemExit(f"world-model output is immutable: {output}")
    known_sources = {
        item["id"]: item for item in load_catalog()["datasets"]
        if isinstance(item, dict) and item.get("id")
    }
    errors = validate_snapshot(snapshot_dir, known_sources)
    if errors:
        raise SystemExit("snapshot validation failed: " + "; ".join(errors))
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = next(
        (
            item for item in manifest["files"]
            if item.get("usage") == "historical_time_sliced_distribution"
            and item.get("source_id") == "seoul-rental-price-files"
            and item.get("data_label") == "derived_observed"
        ),
        None,
    )
    if entry is None:
        raise SystemExit("snapshot has no historical time-sliced distribution")
    market_path = snapshot_dir / entry["path"]
    market_payload = json.loads(market_path.read_text(encoding="utf-8"))
    market_payload["provenance"] = {
        "sourceId": entry["source_id"],
        "dataLabel": entry["data_label"],
        "usage": entry["usage"],
    }
    specification = json.loads(scenario_file.read_text(encoding="utf-8"))
    result = run_scenario_matrix(market_payload, specification)
    result.update({
        "inputSnapshot": manifest["snapshot_id"],
        "inputDataSha256": sha256_file(market_path),
        "inputManifestSha256": sha256_file(snapshot_dir / "manifest.json"),
        "inputCommit": manifest["input_commit"],
        "scenarioSpecificationSha256": scenario_specification_sha256(specification),
        "modelCodeSha256": sha256_file(ROOT / "worldmodel_data" / "model_v0.py"),
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, output)
    print(output)
    return 0


def parser() -> argparse.ArgumentParser:
    from .history_audit import command_audit_history
    result = argparse.ArgumentParser(prog="nestlinker-data")
    sub = result.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit-seoul-history", help="audit annual ZIP files without modifying or admitting them")
    audit.add_argument("--raw-dir", required=True)
    audit.add_argument("--output-dir", required=True)
    audit.add_argument("--years", type=int, nargs="+", default=list(range(2011, 2026)))
    audit.add_argument("--duplicate-threshold", type=float, default=0.01)
    audit.add_argument("--acquisition-ledger")
    audit.set_defaults(handler=command_audit_history)
    catalog = sub.add_parser("catalog", help="list registered sources")
    catalog.add_argument("--category")
    catalog.set_defaults(handler=command_catalog)
    importer = sub.add_parser("import-nestlinker", help="import existing provenance-safe public snapshots")
    importer.add_argument("--source-root", required=True)
    importer.add_argument("--snapshot-date", required=True)
    importer.set_defaults(handler=command_import)
    validate = sub.add_parser("validate", help="validate catalog and snapshot hashes")
    validate.set_defaults(handler=command_validate)
    rtms = sub.add_parser("fetch-rtms", help="fetch RTMS into ignored raw storage")
    rtms.add_argument("--months", type=int, default=3)
    rtms.add_argument("--lawd", action="append", default=[])
    rtms.add_argument("--seoul-only", action="store_true")
    rtms.add_argument("--region-snapshot", default="2026-09-01")
    rtms.add_argument("--delay", type=float, default=0.15)
    rtms.set_defaults(handler=command_fetch_rtms)
    observe = sub.add_parser("observe-rtms", help="collect an immutable RTMS observation version")
    observe.add_argument("--config")
    observe.add_argument("--storage-root", default="data/raw/rtms-observations")
    observe.add_argument("--run-id")
    observe.add_argument("--resume", action="store_true")
    observe.add_argument("--include-low-frequency", action="store_true")
    observe.set_defaults(handler=command_observe_rtms)
    export = sub.add_parser("export-rtms-observation", help="export complete RTMS records locally; never public admission")
    export.add_argument("--storage-root", required=True)
    export.add_argument("--run-id", required=True)
    export.add_argument("--output-dir", required=True)
    export.set_defaults(handler=command_export_rtms_observation)
    features = sub.add_parser("compile-rtms-features", help="compile descriptive API price features from a verified local package")
    features.add_argument("--input-package", required=True)
    features.add_argument("--output-dir", required=True)
    features.add_argument("--minimum-count", type=int, default=10)
    features.set_defaults(handler=command_compile_price_features)
    annual = sub.add_parser("observe-seoul-annual-files", help="record annual ZIP versions separately from RTMS")
    annual.add_argument("--input-dir", required=True)
    annual.add_argument("--storage-root", default="data/raw/rtms-observations")
    annual.add_argument("--run-id", required=True)
    annual.set_defaults(handler=command_observe_annual_archives)
    version_audit = sub.add_parser("audit-rtms-versions", help="compare complete RTMS observation versions")
    version_audit.add_argument("--storage-root", default="data/raw/rtms-observations")
    version_audit.add_argument("--output-dir", required=True)
    version_audit.add_argument("--cadence-days", type=int, default=7)
    version_audit.set_defaults(handler=command_audit_rtms_versions)
    history = sub.add_parser("publish-seoul-history", help="publish privacy-minimal Seoul rental history aggregates")
    history.add_argument("--raw-dir", required=True)
    history.add_argument("--snapshot-date", required=True)
    history.add_argument("--years", type=int, nargs="+", required=True)
    history.add_argument("--acquisition-ledger", required=True)
    history.add_argument("--audit-dir", help="hash-bound annual quality reports; required for pre-2022 backfill")
    history.set_defaults(handler=command_publish_seoul_history)
    replay = sub.add_parser("historical-replay", help="run prior-year aggregate market replay")
    replay.add_argument("--snapshot-dir", required=True)
    replay.add_argument("--output", required=True)
    replay.add_argument("--minimum-counts", type=int, nargs="+", default=[10, 30, 100])
    replay.set_defaults(handler=command_historical_replay)
    sensitivity = sub.add_parser(
        "receipt-filter-sensitivity", help="compare replay with and without receipt-year filtering"
    )
    sensitivity.add_argument("--raw-dir", required=True)
    sensitivity.add_argument("--acquisition-ledger", required=True)
    sensitivity.add_argument("--output", required=True)
    sensitivity.add_argument("--years", type=int, nargs="+", required=True)
    sensitivity.add_argument("--minimum-count", type=int, default=30)
    sensitivity.set_defaults(handler=command_receipt_filter_sensitivity)
    minimum_model = sub.add_parser(
        "minimum-world-model", help="run a deterministic mechanism-only renter decision model"
    )
    minimum_model.add_argument("--snapshot-dir", required=True)
    minimum_model.add_argument("--scenario-file", required=True)
    minimum_model.add_argument("--output", required=True)
    minimum_model.set_defaults(handler=command_minimum_world_model)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.handler(args))
