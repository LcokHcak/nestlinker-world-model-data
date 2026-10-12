"""Package local descriptive features, candidate context and a labelled mechanism demo."""
import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worldmodel_data.manifest import sha256_file


def build(feature_dir, context_dir, model_run, output_dir):
    repo = Path(__file__).resolve().parents[1]
    output = Path(output_dir).resolve()
    if not any(output.parts[i:i + 2] == ("data", "work") for i in range(len(output.parts) - 1)):
        raise ValueError("candidate bundle must remain under data/work")
    output.mkdir(parents=True, exist_ok=False)
    features, context = Path(feature_dir), Path(context_dir)
    fm = json.loads((features / "manifest.json").read_text(encoding="utf-8"))
    if sha256_file(features / "market-features.json") != fm["files"][0]["sha256"]:
        raise ValueError("feature manifest/data hash mismatch")
    inputs = [(features / "manifest.json", "price-features/manifest.json"),
              (features / "market-features.json", "price-features/market-features.json"),
              (context / "acquisition.json", "official-context/acquisition.json"),
              (Path(model_run), "mechanism-demo/annual-data-synthetic-scenarios.json"),
              (repo / "docs/WORLD_MODEL_DATA_EXPANSION.md", "README.zh-CN.md"),
              (repo / "catalog/datasets.json", "sources/catalog.json")]
    context_counts = {}
    for name in ("bok-base-rate.json", "hug-guarantee-accidents-region.json", "hug-personal-guarantee-balances.json"):
        item = json.loads((context / name).read_text(encoding="utf-8"))
        if not item.get("records"):
            raise ValueError("candidate context records absent")
        inputs.append((context / name, "official-context/" + name))
        context_counts[item["source_id"]] = len(item["records"])
    acquisition = json.loads((context / "acquisition.json").read_text(encoding="utf-8"))
    for item in acquisition["pages"]:
        if item.get("raw_file"):
            path = (context / item["raw_file"]).resolve()
            if context.resolve() not in path.parents or sha256_file(path) != item["raw_sha256"]:
                raise ValueError("raw aggregate CSV differs from acquisition hash")
            inputs.append((path, "official-context/" + item["raw_file"]))
    files = []
    for source, relative in inputs:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files.append({"path": relative, "sha256": sha256_file(target), "bytes": target.stat().st_size})
    manifest = {"schema_version": 1, "classification": "local_candidate_model_inputs",
                "public_release_eligible": False, "files": files,
                "price_feature_group_count": fm["feature_group_count"],
                "input_source_record_count": fm["source_record_count"],
                "context_counts": context_counts,
                "limitations": ["Price features are descriptive, not trained predictions.",
                                "Context observations have not been admitted to public snapshots.",
                                "The mechanism demonstration uses the annual snapshot and synthetic scenarios; API/context inputs are not yet wired into model_v0.",
                                "No deposit-return event labels or safety scoring model are included."]}
    with (output / "bundle-manifest.json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    files.append({"path": "bundle-manifest.json", "sha256": sha256_file(output / "bundle-manifest.json")})
    checksums = "".join(item["sha256"] + "  " + item["path"] + "\n" for item in files)
    with (output / "SHA256SUMS.txt").open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(checksums)
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                bundle.write(path, output.name + "/" + path.relative_to(output).as_posix())
    with zipfile.ZipFile(archive) as bundle:
        for item in files:
            content = bundle.read(output.name + "/" + item["path"])
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError("archived file hash mismatch")
    digest = sha256_file(archive)
    with archive.with_suffix(".zip.sha256").open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(digest + "  " + archive.name + "\n")
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size, "sha256": digest,
                      "price_groups": fm["feature_group_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-dir", required=True)
    parser.add_argument("--context-dir", required=True)
    parser.add_argument("--model-run", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    build(args.feature_dir, args.context_dir, args.model_run, args.output_dir)
