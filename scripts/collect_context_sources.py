"""Collect official context pages and safely probe an additional RTMS source."""
import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worldmodel_data.observation import _request_url, _redact_secret_text, _source_reported_at
from worldmodel_data.rtms import _request, normalize_item, parse_xml_items


BOK_URL = "https://www.bok.or.kr/portal/singl/baseRate/list.do?dataSeCd=01&menuNo=200643"
PAGES = {
    "bok-base-rate": BOK_URL,
    "hug-guarantee-accidents-region": "https://www.data.go.kr/data/15002597/fileData.do",
    "hug-personal-guarantee-balances": "https://www.data.go.kr/data/15152527/fileData.do",
    "seoul-housing-by-building-year": "https://data.seoul.go.kr/dataList/DT201004O090003/S/2/datasetView.do",
}


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.table, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, text):
        if self.cell is not None:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def parse_bok_rates(text):
    parser = Tables()
    parser.feed(text)
    records = []
    for table in parser.tables:
        if not any("변경일자" in " ".join(row) and "기준금리" in " ".join(row) for row in table):
            continue
        current_year = None
        for row in table:
            if len(row) == 3 and re.fullmatch(r"\d{4}", row[0]):
                current_year = int(row[0])
                parts = row[1:]
            elif len(row) == 2 and current_year:
                parts = row
            else:
                continue
            match = re.fullmatch(r"(\d{1,2})월\s*(\d{1,2})일", parts[0])
            if not match or not re.fullmatch(r"\d+(?:\.\d+)?", parts[1]):
                raise ValueError("unexpected BOK rate table row")
            effective = date(current_year, int(match[1]), int(match[2]))
            rate = Decimal(parts[1])
            if not Decimal("0") <= rate <= Decimal("30"):
                raise ValueError("invalid base rate")
            records.append({"effective_date": effective.isoformat(), "base_rate_pct": float(rate)})
    if not records or len({x["effective_date"] for x in records}) != len(records):
        raise ValueError("BOK table absent or contains duplicate effective dates")
    return sorted(records, key=lambda x: x["effective_date"])


def save_json(path, data):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def download_portal_csv(page_text, output, source, observed):
    links = re.findall(r'"contentUrl"\s*:\s*"([^"]+)"', page_text)
    urls = [html.unescape(url.replace("\\/", "/")) for url in links]
    urls = [url for url in urls if urlsplit(url).scheme == "https" and
            urlsplit(url).hostname == "www.data.go.kr" and urlsplit(url).path == "/cmm/cmm/fileDownload.do"]
    if len(set(urls)) != 1:
        raise ValueError("portal page does not provide one unambiguous file download")
    with urllib.request.urlopen(urls[0], timeout=30) as response:
        raw = response.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024 or raw.lstrip().lower().startswith((b"<html", b"<!doctype")):
        raise ValueError("download did not return a bounded data file")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(encoding, errors="strict")
            break
        except UnicodeDecodeError:
            pass
    else:
        raise ValueError("CSV encoding unsupported")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2 or not rows[0] or len(rows[0]) != len(set(rows[0])) or any(len(row) != len(rows[0]) for row in rows[1:]):
        raise ValueError("CSV headers/row widths invalid")
    if any(any(word in column for word in ("주민등록", "고객명", "연락처", "전화번호")) for column in rows[0]):
        raise ValueError("aggregate context contains unexpected identifiers")
    with (output / (source + ".csv")).open("xb") as handle:
        handle.write(raw)
    save_json(output / (source + ".json"), {
        "source_id": source, "official_url": PAGES[source], "observed_at": observed,
        "data_label": "observed", "header": rows[0], "records": [dict(zip(rows[0], row)) for row in rows[1:]],
        "units": "preserve original column names; do not mix 억원 with 만원",
        "limitations": ["Aggregate guarantee context, not individual lease eligibility or repayment outcomes.",
                         "Raw blank strings preserved; interpretation must follow source-specific definitions.",
                         "No individual safety score or deposit-return probability is inferred."],
    })
    return {"structured_data_obtained": True, "record_count": len(rows) - 1,
            "raw_file": source + ".csv", "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_bytes": len(raw), "columns": rows[0], "encoding": encoding}


def collect(output, probe_month):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    observed = datetime.now(timezone.utc).isoformat()
    summary = {"schema_version": 1, "observed_at": observed, "pages": [], "rowhouse_probe": None,
               "public_release_eligible": False}
    for source, url in PAGES.items():
        item = {"source_id": source, "official_url": url, "observed_at": observed}
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "NestLinkerWorldData/0.1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("context page exceeds limit")
                encoding = response.headers.get_content_charset() or "utf-8"
                text = raw.decode(encoding, errors="strict")
            with (output / (source + ".html")).open("xb") as handle:
                handle.write(raw)
            item.update(status="page_observed", sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
                        file=source + ".html", encoding=encoding,
                        structured_data_obtained=False)
            if source == "bok-base-rate":
                rows = parse_bok_rates(text)
                if any(x["effective_date"] > observed[:10] for x in rows):
                    raise ValueError("source table contains future effective dates")
                save_json(output / "bok-base-rate.json", {"source_id": source, "official_url": url,
                    "observed_at": observed, "source_reported_at": None, "data_label": "derived_observed",
                    "records": rows, "unit": "percent", "usage": "macro_context_only",
                    "limitations": ["Policy base rate is not an individual deposit-loan interest rate.",
                                    "Effective dates do not reconstruct first official publication timestamps.",
                                    "Historical as-of availability has not been reconstructed."]})
                item.update(structured_data_obtained=True, record_count=len(rows),
                            temporal_coverage={"start": rows[0]["effective_date"], "end": rows[-1]["effective_date"]})
            elif source in ("hug-guarantee-accidents-region", "hug-personal-guarantee-balances"):
                item.update(download_portal_csv(text, output, source, observed))
        except Exception as exc:
            item.update(status="failed", error_type=type(exc).__name__)
        summary["pages"].append(item)
        print(source, item["status"], item.get("record_count", "metadata_only"), flush=True)
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    probe = {"source_id": "molit-rtms-rowhouse-rent", "lawd_code": "11110", "contract_month": probe_month,
             "observed_at": datetime.now(timezone.utc).isoformat(), "public_release_eligible": False}
    if key:
        try:
            text = _request(_request_url("rowhouse", "11110", probe_month, 1, key), timeout=30)
            text, _ = _redact_secret_text(text, key)
            with (output / "rowhouse-probe.xml").open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            items, total, _ = parse_xml_items(text)
            probe.update(status="response_received", source_total_count=total, returned_rows=len(items),
                         normalized_rows=sum(normalize_item("rowhouse", x, "11110") is not None for x in items),
                         field_names=sorted(set(k for row in items for k in row)),
                         official_id_present=any(k in ("dealSeq", "rentalSeq") for row in items for k in row))
        except urllib.error.HTTPError as exc:
            raw = exc.read(1024 * 1024).decode("utf-8", errors="replace")
            safe, _ = _redact_secret_text(raw, key)
            with (output / "rowhouse-probe-error.xml").open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(safe)
            probe.update(status="access_failed", http_status=exc.code)
        except Exception as exc:
            probe.update(status="failed", error_type=type(exc).__name__)
    else:
        probe.update(status="credential_missing")
    summary["rowhouse_probe"] = probe
    save_json(output / "acquisition.json", summary)
    print(json.dumps(probe, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--probe-month", default="202609")
    args = parser.parse_args()
    collect(args.output_dir, args.probe_month)
