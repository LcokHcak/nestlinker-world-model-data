"""Build a controlled, recipient-ready RTMS data package without raw XML or secrets."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worldmodel_data.observation_export import export_observation
from scripts.inventory_worldmodel_data import inventory as worldmodel_inventory


SOURCES = [
    ("apartment", "https://www.data.go.kr/data/15126474/openapi.do"),
    ("officetel", "https://www.data.go.kr/data/15126475/openapi.do"),
    ("single_multi", "https://www.data.go.kr/data/15126472/openapi.do"),
]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text_lf(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def complete_run(storage: Path) -> tuple[str, dict]:
    matches = []
    for path in (storage / "runs").glob("*/run.json"):
        state = read_json(path)
        if state.get("status") == "finalized" and state.get("version_complete") and state.get("audit_passed"):
            matches.append((state["run_id"], state))
    if not matches:
        raise ValueError(f"no complete audited run under {storage}")
    return sorted(matches)[-1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def verify_export(export: Path, run_id: str) -> dict:
    manifest = export / "manifest.json"
    value = read_json(manifest)
    if value.get("run_id") != run_id:
        raise ValueError(f"existing export has wrong run_id: {export}")
    records = export / "records.jsonl"
    if records.stat().st_size != value["files"][0]["bytes"] or sha256(records) != value["files"][0]["sha256"]:
        raise ValueError(f"existing export failed hash verification: {export}")
    return value


def scope_from_run(run: dict) -> tuple[str, str, str]:
    months = sorted(set(run["config"]["contract_months"]))
    if not months:
        raise ValueError("run has no selected contract months")
    indices = [int(month[:4]) * 12 + int(month[4:]) - 1 for month in months]
    if indices != list(range(indices[0], indices[-1] + 1)):
        raise ValueError("package scope must be contiguous; cannot claim missing months")
    start, end = months[0][:4] + "-" + months[0][4:], months[-1][:4] + "-" + months[-1][4:]
    return start + "_to_" + end, start, end


def select_scopes(history: Path, recent: Path) -> list:
    scopes = []
    seen = set()
    for storage in [history / "by-year" / str(y) for y in range(2011, 2025)] + [history, recent]:
        run_id, run = complete_run(storage)
        label, start, end = scope_from_run(run)
        months = set(run["config"]["contract_months"])
        if seen & months:
            raise ValueError("selected runs overlap in contract months; do not add observation versions")
        seen.update(months)
        if start[5:] == "01" and end == start[:4] + "-12":
            label = start[:4]
        scopes.append((label, start, end, storage, run_id, run))
    return scopes


def ensure_export(storage: Path, run_id: str, export: Path, search_root: Path) -> tuple[dict, Path]:
    if (export / "manifest.json").is_file():
        return verify_export(export, run_id), export
    for candidate in search_root.rglob("manifest.json"):
        value = read_json(candidate)
        if value.get("run_id") == run_id:
            return verify_export(candidate.parent, run_id), candidate.parent
    return export_observation(storage, run_id, export), export


def build(args: argparse.Namespace) -> tuple[Path, Path]:
    repo = Path(__file__).resolve().parents[1]
    history = (repo / args.history_storage).resolve()
    recent = (repo / args.recent_storage).resolve()
    export_root = (repo / args.export_root).resolve()
    package = (repo / args.output_dir).resolve()
    if not any(package.parts[i:i + 2] == ("data", "work") for i in range(len(package.parts) - 1)):
        raise ValueError("controlled packages must be under data/work")
    if package.exists():
        raise FileExistsError(f"package output already exists: {package}")
    package.mkdir(parents=True)
    inventory = []

    scopes = select_scopes(history, recent)

    for label, start, end, storage, run_id, run in scopes:
        export_dir = export_root / (label + "_" + run_id)
        manifest, actual_export = ensure_export(storage, run_id, export_dir, export_root.parent)
        if manifest["input_manifest_sha256"] != sha256(storage / "runs" / run_id / "run.json"):
            raise ValueError("export is not bound to selected source manifest")
        if manifest["record_count"] != run["normalized_record_count"]:
            raise ValueError("export count differs from source run")
        records_source = actual_export / "records.jsonl"
        records_target = package / "data" / label / "records.jsonl"
        link_or_copy(records_source, records_target)
        shutil.copy2(actual_export / "manifest.json", package / "data" / label / "manifest.json")
        item = {
            "label": label, "contract_month_start": start, "contract_month_end": end,
            "run_id": run_id, "observed_at": run["observed_at"],
            "record_count": manifest["record_count"],
            "counts_by_source": manifest["counts_by_source"],
            "counts_by_contract_month": manifest["counts_by_contract_month"],
            "missing_field_counts": manifest["missing_field_counts"],
            "complete_partition_count": run["complete_partition_count"],
            "input_manifest_sha256": manifest["input_manifest_sha256"],
            "sha256": manifest["files"][0]["sha256"], "bytes": manifest["files"][0]["bytes"],
            "relative_path": f"data/{label}/records.jsonl",
        }
        inventory.append(item)
        print(label, item["record_count"], flush=True)

    total = sum(item["record_count"] for item in inventory)
    coverage_start = min(item["contract_month_start"] for item in inventory)
    coverage_end = max(item["contract_month_end"] for item in inventory)
    recent_observed_at = inventory[-1]["observed_at"]
    context = []
    if args.include_worldmodel:
        context_inventory = worldmodel_inventory(repo)
        write_text_lf(package / "worldmodel-inventory.json", json.dumps(context_inventory, ensure_ascii=False, indent=2) + "\n")
        for snap in context_inventory["snapshots"]:
            manifest_path = repo / snap["manifest_path"]
            relative_dir = Path("context") / snap["snapshot_id"]
            dest = package / relative_dir
            dest.mkdir(parents=True)
            shutil.copy2(manifest_path, dest / "manifest.json")
            for item in snap["files"]:
                shutil.copy2(manifest_path.parent / item["path"], dest / item["path"])
            context.append({"snapshot_id": snap["snapshot_id"], "relative_dir": relative_dir.as_posix(),
                            "files": snap["files"], "manifest_sha256": snap["manifest_sha256"]})
    package_manifest = {
        "schema_version": 1, "package_id": args.package_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "classification": "controlled_normalized_transaction_records",
        "geography": "Seoul, 25 districts", "record_count": total,
        "contract_month_coverage": {"start": coverage_start, "end": coverage_end},
        "sources": [{"property_type": name, "official_url": url,
                     "portal_license": "no_restriction_as_checked_2026-09-28"} for name, url in SOURCES],
        "datasets": inventory,
        "context_snapshots": context,
        "excluded": ["API credentials", "raw XML responses", "credential-bearing request URLs", "failed/incomplete run records",
                     "annual ZIP source files", "content fingerprint audit details"],
        "public_release_eligible": False,
    }
    write_text_lf(package / "inventory.json", json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n")
    with (package / "inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["label", "contract_month_start", "contract_month_end", "run_id", "observed_at", "record_count", "bytes", "sha256", "relative_path"])
        writer.writeheader()
        for item in inventory:
            writer.writerow({key: item[key] for key in writer.fieldnames})

    readme = f"""# NestLinker Seoul RTMS controlled data package

Package: `{args.package_id}`  
Records: **{total:,}** source occurrences  
Coverage: contract months {coverage_start} through {coverage_end}, Seoul 25 districts
Sources: apartment, officetel, single/multi-family rental APIs from Korea MOLIT via DATA.GO.KR

## Start here

`inventory.csv` lists every file, observation time, row count, byte size and SHA-256. Data files are UTF-8 JSON Lines: one JSON object per line. Verify the archive checksum and each data-file checksum before use.

## Classification and sharing

This is a **controlled normalized-detail package**, not the repository's public aggregate snapshot. It excludes API keys, raw XML, request URLs, incomplete runs and detailed content-fingerprint audits. Share only with an intended recipient through an access-controlled transfer channel. Do not commit this package to GitHub or attach it to public CI.

The official portal described the three API datasets as free and with no restriction on permitted-use scope when checked on 2026-09-28. The recipient must still preserve source attribution, re-check current portal terms, and comply with applicable privacy and data-protection obligations.

## Semantics

Rows are source occurrences, not confirmed unique contracts. `id` and `content_fingerprint` depend on content and occurrence number; neither is an official stable transaction ID. `contract_date` is the contract date. `observed_at` is when this collector saw the version, not official publication time. `source_reported_at` is null where the source omitted it. `first_seen_at` is intentionally null in these baseline exports and must not be inferred from contract date.

Amounts are in 10,000 KRW (`*_manwon`). Explicit zero is valid; missing values were not converted to zero. `exclusive_area_sqm` and `reported_area_sqm` must be interpreted with `reported_area_basis`. For single/multi-family rows, `source_totalFloorAr` is not an apartment-style exclusive area. Missing optional fields remain null.

Observation timestamps are listed for every selected run in inventory.csv. Historical contract years do not reconstruct historical as-of availability or reporting delay. The recent scope is the complete version observed at {recent_observed_at}. Do not add other observation versions to these files as extra transactions.

## Data fields

- `source`, `lawd_code`, `legal_dong`, `building_name`
- `lease_type`, `contract_date`, `deal_date`
- `deposit_manwon`, `monthly_rent_manwon`
- `exclusive_area_sqm`, `reported_area_sqm`, `reported_area_basis`
- `floor`, `build_year`, `contract_term`
- `observed_at`, `source_reported_at`, `first_seen_at`, `first_seen_status`
- `run_id`, `id`, `content_fingerprint`

No deposit-return, move-out, dispute, tenant, safety-score or loss-outcome events are included. This package alone cannot train or validate RDRC.

When present, `context/` contains separately admitted public snapshots (population, regional keys, gosiwon registry, subway anchors, rental examples and annual rental aggregates). `worldmodel-inventory.json` distinguishes registered-only sources from stored evidence. Context snapshots overlap the RTMS scopes and each other; their counts are not added to the package's normalized-record total.
"""
    write_text_lf(package / "README.md", readme)
    readme_zh = f"""# NestLinker 首尔 RTMS 受控数据交付包

交付包：`{args.package_id}`  
记录数：**{total:,}** 条来源记录出现次数  
范围：首尔 25 个行政区，合同月份 {coverage_start} 至 {coverage_end}
来源：韩国国土交通部公寓、办公住宅、独栋／多户住宅租赁实价 API

## 文件怎么用

先看 `inventory.csv`：里面列出每个数据段的月份范围、采集批次、观察时间、记录数、字节数和 SHA-256。`data/<范围>/records.jsonl` 是 UTF-8 JSON Lines，每一行一个 JSON 对象。`manifest.json` 是该数据段的独立校验清单，`SHA256SUMS.txt` 是整包文件清单。

PowerShell 校验压缩包：

```powershell
Get-FileHash -Algorithm SHA256 .\\{args.package_id}.zip
Get-Content .\\{args.package_id}.zip.sha256
```

两者哈希应完全一致。解压后可用 Python 逐行读取，避免一次把几 GB 数据装入内存：

```python
import json
from pathlib import Path

path = Path("data/2024/records.jsonl")
with path.open(encoding="utf-8") as rows:
    for line in rows:
        record = json.loads(line)
        print(record["contract_date"], record["deposit_manwon"])
        break
```

## 分享边界

这是**受控标准化明细包**，不是公开聚合快照。包内不含 API 密钥、原始 XML、带密钥 URL、失败批次和详细指纹审计。应通过有访问控制的网盘、对象存储或加密硬盘交给指定接收人，不要上传公开 GitHub、公开 CI 附件或群聊。

2026-09-28 核对三个官方门户页面时，数据免费且利用许可范围标为不受限制；接收方仍应保留来源署名、重新核对最新条款，并遵守适用的隐私和数据保护要求。

## 口径限制

- 每行是来源中的一次记录出现，不是确认去重后的唯一合同。
- `id` 和 `content_fingerprint` 不是官方稳定交易 ID。
- `contract_date` 是合同日期；`observed_at` 是本系统看到该版本的时间，不是官方发布日期。
- `first_seen_at` 在首次基线中保持 null，不能用合同日期或导出日期代填。
- 押金和月租单位为万韩元；显式 0 有效，缺失值没有转成 0。
- 独栋／多户的 `source_totalFloorAr` 不等同于公寓专有面积，应结合 `reported_area_basis` 使用。
- 各批次真实观察时间见 inventory.csv；近期版本观察时间为 {recent_observed_at}。历史合同年份不能还原过去任意日期的 as-of 状态或真实迟报时间。
- 不包含退租、押金返还、纠纷、租客身份、损失结果或安全评分，不能单独训练或验证 RDRC。

官方来源链接见 `inventory.json`。英文说明见 `README.md`。

若包内存在 `context/`，其中是另外分类的公开快照（人口、地区连接、考试院登记、地铁锚点、租赁示例及年度聚合）。`worldmodel-inventory.json` 区分“已登记”与“有实际文件证据”。这些快照与 API 及彼此可能重叠，其数量未累加到明细总数。隔离的 2025 年度 ZIP 只有诊断清单，不含原始内容。
"""
    write_text_lf(package / "README.zh-CN.md", readme_zh)
    checksums = [f"{sha256(path)}  {path.relative_to(package).as_posix()}"
                 for path in sorted(package.rglob("*")) if path.is_file()]
    write_text_lf(package / "SHA256SUMS.txt", "\n".join(checksums) + "\n")

    archive = package.with_suffix(".zip")
    if archive.exists():
        raise FileExistsError(f"archive already exists: {archive}")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as bundle:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(package.name) / path.relative_to(package))
    verify_archive(archive, package.name, inventory, checksums)
    archive_hash = sha256(archive)
    write_text_lf(archive.with_suffix(".zip.sha256"), f"{archive_hash}  {archive.name}\n")
    return package, archive


def verify_archive(archive: Path, root_name: str, inventory: list, checksums: list) -> None:
    """Read all compressed bytes; validate hashes and actual JSONL line counts."""
    expected_counts = {item["relative_path"]: item["record_count"] for item in inventory}
    expected_hashes = dict(line.split("  ", 1)[::-1] for line in checksums)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        expected_names = {root_name + "/" + path for path in expected_hashes} | {root_name + "/SHA256SUMS.txt"}
        if len(names) != len(set(names)) or set(names) != expected_names:
            raise ValueError("archive membership differs from allowlisted package files")
        if bundle.read(root_name + "/SHA256SUMS.txt") != ("\n".join(checksums) + "\n").encode("utf-8"):
            raise ValueError("archive checksum list differs from verified package inventory")
        for path, expected in expected_hashes.items():
            digest, line_count = hashlib.sha256(), 0
            with bundle.open(root_name + "/" + path) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
                    line_count += block.count(b"\n")
            if digest.hexdigest() != expected:
                raise ValueError("archived file hash mismatch: " + path)
            if path in expected_counts and line_count != expected_counts[path]:
                raise ValueError("archived JSONL row count mismatch: " + path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-storage", default="data/raw/rtms-history-observations")
    parser.add_argument("--recent-storage", default="data/raw/rtms-observations")
    parser.add_argument("--export-root", default="data/work/rtms-normalized/share-package")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--package-id", default="nestlinker-seoul-rtms-2011-2026-20260928")
    parser.add_argument("--include-worldmodel", action="store_true", help="include verified public context snapshots and catalog evidence inventory")
    args = parser.parse_args()
    package, archive = build(args)
    print(package, flush=True)
    print(archive, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
