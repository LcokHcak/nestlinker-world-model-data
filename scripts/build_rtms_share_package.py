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


def ensure_export(storage: Path, run_id: str, export: Path, search_root: Path) -> tuple[dict, Path]:
    if (export / "manifest.json").is_file():
        return verify_export(export, run_id), export
    for candidate in search_root.glob("*/manifest.json"):
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
    if package.exists():
        raise FileExistsError(f"package output already exists: {package}")
    package.mkdir(parents=True)
    inventory = []

    scopes = []
    for year in range(2011, 2025):
        storage = history / "by-year" / str(year)
        scopes.append((str(year), f"{year}-01", f"{year}-12", storage))
    scopes.append(("2025-01_to_2025-09", "2025-01", "2025-09", history))
    scopes.append(("2025-10_to_2026-09", "2025-10", "2026-09", recent))

    for label, start, end, storage in scopes:
        run_id, run = complete_run(storage)
        export_dir = export_root / label
        manifest, actual_export = ensure_export(storage, run_id, export_dir, export_root.parent)
        records_source = actual_export / "records.jsonl"
        records_target = package / "data" / label / "records.jsonl"
        link_or_copy(records_source, records_target)
        shutil.copy2(actual_export / "manifest.json", package / "data" / label / "manifest.json")
        item = {
            "label": label, "contract_month_start": start, "contract_month_end": end,
            "run_id": run_id, "observed_at": run["observed_at"],
            "record_count": manifest["record_count"],
            "counts_by_source": manifest["counts_by_source"],
            "sha256": manifest["files"][0]["sha256"], "bytes": manifest["files"][0]["bytes"],
            "relative_path": f"data/{label}/records.jsonl",
        }
        inventory.append(item)
        print(label, item["record_count"], flush=True)

    total = sum(item["record_count"] for item in inventory)
    package_manifest = {
        "schema_version": 1, "package_id": args.package_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "classification": "controlled_normalized_transaction_records",
        "geography": "Seoul, 25 districts", "record_count": total,
        "sources": [{"property_type": name, "official_url": url,
                     "portal_license": "no_restriction_as_checked_2026-09-28"} for name, url in SOURCES],
        "datasets": inventory,
        "excluded": ["API credentials", "raw XML responses", "request URLs", "failed/incomplete runs",
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
Coverage: contract months 2011-01 through 2026-09, Seoul 25 districts  
Sources: apartment, officetel, single/multi-family rental APIs from Korea MOLIT via DATA.GO.KR

## Start here

`inventory.csv` lists every file, observation time, row count, byte size and SHA-256. Data files are UTF-8 JSON Lines: one JSON object per line. Verify the archive checksum and each data-file checksum before use.

## Classification and sharing

This is a **controlled normalized-detail package**, not the repository's public aggregate snapshot. It excludes API keys, raw XML, request URLs, incomplete runs and detailed content-fingerprint audits. Share only with an intended recipient through an access-controlled transfer channel. Do not commit this package to GitHub or attach it to public CI.

The official portal described the three API datasets as free and with no restriction on permitted-use scope when checked on 2026-09-28. The recipient must still preserve source attribution, re-check current portal terms, and comply with applicable privacy and data-protection obligations.

## Semantics

Rows are source occurrences, not confirmed unique contracts. `id` and `content_fingerprint` depend on content and occurrence number; neither is an official stable transaction ID. `contract_date` is the contract date. `observed_at` is when this collector saw the version, not official publication time. `source_reported_at` is null where the source omitted it. `first_seen_at` is intentionally null in these baseline exports and must not be inferred from contract date.

Amounts are in 10,000 KRW (`*_manwon`). Explicit zero is valid; missing values were not converted to zero. `exclusive_area_sqm` and `reported_area_sqm` must be interpreted with `reported_area_basis`. For single/multi-family rows, `source_totalFloorAr` is not an apartment-style exclusive area. Missing optional fields remain null.

Historical years are first observed baselines collected on 2026-09-14. They do not reconstruct historical as-of availability or reporting delay. The recent scope is the complete version observed on 2026-09-27 UTC (2026-09-28 Asia/Seoul). Do not add other observation versions to these files as extra transactions.

## Data fields

- `source`, `lawd_code`, `legal_dong`, `building_name`
- `lease_type`, `contract_date`, `deal_date`
- `deposit_manwon`, `monthly_rent_manwon`
- `exclusive_area_sqm`, `reported_area_sqm`, `reported_area_basis`
- `floor`, `build_year`, `contract_term`
- `observed_at`, `source_reported_at`, `first_seen_at`, `first_seen_status`
- `run_id`, `id`, `content_fingerprint`

No deposit-return, move-out, dispute, tenant, safety-score or loss-outcome events are included. This package alone cannot train or validate RDRC.
"""
    write_text_lf(package / "README.md", readme)
    readme_zh = f"""# NestLinker 首尔 RTMS 受控数据交付包

交付包：`{args.package_id}`  
记录数：**{total:,}** 条来源记录出现次数  
范围：首尔 25 个行政区，合同月份 2011-01 至 2026-09  
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
- 历史数据是在 2026-09-14 首次观察到的历史基线，不能还原过去任意日期的 as-of 状态或真实迟报时间。
- 不包含退租、押金返还、纠纷、租客身份、损失结果或安全评分，不能单独训练或验证 RDRC。

官方来源链接见 `inventory.json`。英文说明见 `README.md`。
"""
    write_text_lf(package / "README.zh-CN.md", readme_zh)
    checksums = [f'{item["sha256"]}  {item["relative_path"]}' for item in inventory]
    for name in ("README.md", "README.zh-CN.md", "inventory.json", "inventory.csv"):
        checksums.append(f"{sha256(package / name)}  {name}")
    write_text_lf(package / "SHA256SUMS.txt", "\n".join(checksums) + "\n")

    archive = package.with_suffix(".zip")
    if archive.exists():
        raise FileExistsError(f"archive already exists: {archive}")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as bundle:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(package.name) / path.relative_to(package))
    archive_hash = sha256(archive)
    write_text_lf(archive.with_suffix(".zip.sha256"), f"{archive_hash}  {archive.name}\n")
    return package, archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-storage", default="data/raw/rtms-history-observations")
    parser.add_argument("--recent-storage", default="data/raw/rtms-observations")
    parser.add_argument("--export-root", default="data/work/rtms-normalized/share-package")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--package-id", default="nestlinker-seoul-rtms-2011-2026-20260928")
    args = parser.parse_args()
    package, archive = build(args)
    print(package, flush=True)
    print(archive, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
