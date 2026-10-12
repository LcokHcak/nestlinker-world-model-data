"""Inventory stored evidence and unmet collection scopes without exposing records."""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worldmodel_data.manifest import sha256_file


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_runs(storage):
    result = []
    for manifest in sorted(storage.glob("runs/*/run.json")):
        run = load(manifest)
        months = run.get("config", {}).get("contract_months", [])
        result.append({key: run.get(key) for key in (
            "run_id", "status", "observed_at", "finished_at", "version_complete", "audit_passed",
            "planned_partition_count", "complete_partition_count", "raw_record_count",
        )})
        result[-1].update(contract_months=sorted(months), manifest_sha256=sha256_file(manifest),
                          manifest_path=manifest.as_posix())
    return result


def inventory(repo):
    catalog = load(repo / "catalog/datasets.json")
    snapshots, evidence_sources = [], set()
    for manifest_path in sorted((repo / "data/snapshots").glob("*/*/manifest.json")):
        manifest = load(manifest_path)
        files = []
        for entry in manifest["files"]:
            path = manifest_path.parent / entry["path"]
            if manifest_path.parent not in path.resolve().parents:
                raise ValueError("snapshot path escapes manifest directory")
            actual_hash = sha256_file(path)
            if actual_hash != entry["sha256"] or path.stat().st_size != entry["bytes"]:
                raise ValueError("snapshot bytes/hash mismatch: " + str(path))
            source_ids = [entry["source_id"]] + entry.get("secondary_source_ids", [])
            evidence_sources.update(source_ids)
            files.append({key: entry.get(key) for key in (
                "path", "source_id", "secondary_source_ids", "record_count", "bytes", "sha256",
                "temporal_coverage", "based_on_record_count", "limitations",
            )})
        snapshots.append({"snapshot_id": manifest["snapshot_id"],
                          "manifest_path": manifest_path.relative_to(repo).as_posix(),
                          "manifest_sha256": sha256_file(manifest_path), "files": files})
    recent = summarize_runs(repo / "data/raw/rtms-observations")
    history = summarize_runs(repo / "data/raw/rtms-history-observations")
    for year in range(2011, 2025):
        history.extend(summarize_runs(repo / "data/raw/rtms-history-observations/by-year" / str(year)))
    dataset_entries = [{"id": item["id"], "title": item["title"], "category": item["category"],
                        "catalog_status": item["status"], "access": item["access"],
                        "snapshot_file_evidence": item["id"] in evidence_sources,
                        "official_url": item["landing_url"]} for item in catalog["datasets"]]
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "catalog_source_count": len(dataset_entries),
            "catalog_status_counts": dict(Counter(x["catalog_status"] for x in dataset_entries)),
            "snapshot_count": len(snapshots), "snapshot_evidenced_source_count": len(evidence_sources),
            "registered_sources": dataset_entries, "snapshots": snapshots,
            "recent_runs": recent, "historical_api_runs": history,
            "annual_zip_ledger": load(repo / "data/acquisitions/2026-09-05/seoul-rental-files.json"),
            "quarantined_annual_2025": load(repo / "data/quarantine/2026-09-03/seoul-rents-2025.audit.json"),
            "limitations": ["Catalog registration alone is not evidence of collection or admission.",
                            "Snapshot record counts have different units and overlapping scopes; do not sum as contracts.",
                            "Historical contract months are not historical observation timestamps.",
                            "Unfinished run manifests alone do not establish live processes.",
                            "No deposit-return or dispute events have been collected in these APIs."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError("inventory output is immutable; select a new filename")
    result = inventory(Path(args.repo).resolve())
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"sources": result["catalog_source_count"], "snapshots": result["snapshot_count"],
                      "snapshot_evidenced_sources": result["snapshot_evidenced_source_count"],
                      "recent_runs": len(result["recent_runs"]), "historical_runs": len(result["historical_api_runs"])}))


if __name__ == "__main__":
    main()
