"""Enrich docs/sector_dict.json with stock names from Tencent API.

One-time migration. Reads existing code-only dict, queries Tencent for
each code, writes back with [{code, name}, ...] format.

Re-run safe: detects already-enriched entries.
"""
import json
import sys
import time
from pathlib import Path

import requests


SECTOR_DICT = Path(__file__).parent.parent / "docs" / "sector_dict.json"


def _collect_codes(data):
    out = []
    for category, sectors in data.items():
        if category.startswith("_"):
            continue
        if not isinstance(sectors, dict):
            continue
        for sector, stocks in sectors.items():
            if isinstance(stocks, list):
                for entry in stocks:
                    if isinstance(entry, str):
                        out.append(entry)
                    elif isinstance(entry, dict):
                        code = entry.get("code")
                        if code:
                            out.append(code)
    return out


def _lookup_names(codes, batch_size=80):
    name_map = {}
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        url = "http://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            resp = requests.get(url, timeout=10)
            resp.encoding = "gbk"
            for entry in resp.text.split(";"):
                entry = entry.strip()
                if "=" not in entry:
                    continue
                k, v = entry.split("=", 1)
                code = k.strip().replace("v_", "")
                fields = v.strip().strip('"').split("~")
                if len(fields) > 1 and fields[1]:
                    name_map[code] = fields[1]
        except Exception as e:
            print(f"  batch {i} failed: {e}")
        time.sleep(0.1)
    return name_map


def _enrich(data, name_map):
    enriched_count = 0
    for category, sectors in data.items():
        if category.startswith("_") or not isinstance(sectors, dict):
            continue
        for sector, stocks in sectors.items():
            if not isinstance(stocks, list):
                continue
            new_stocks = []
            for entry in stocks:
                if isinstance(entry, str):
                    code = entry
                    name = name_map.get(code, "")
                    new_stocks.append({"code": code, "name": name})
                    if name:
                        enriched_count += 1
                elif isinstance(entry, dict) and "code" in entry:
                    new_stocks.append(entry)
            sectors[sector] = new_stocks
    return enriched_count


def main():
    if not SECTOR_DICT.exists():
        print(f"sector_dict not found at {SECTOR_DICT}")
        sys.exit(1)

    data = json.loads(SECTOR_DICT.read_text(encoding="utf-8"))
    codes = list(dict.fromkeys(_collect_codes(data)))
    print(f"Found {len(codes)} unique stock codes in sector_dict.json")

    print(f"Querying Tencent for names...")
    name_map = _lookup_names(codes)
    print(f"Got {len(name_map)}/{len(codes)} names")

    enriched = _enrich(data, name_map)
    print(f"Enriched {enriched} entries with names")

    SECTOR_DICT.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {SECTOR_DICT}")

    unresolved = [c for c in codes if c not in name_map]
    if unresolved:
        print(f"\nUnresolved codes ({len(unresolved)}):")
        for c in unresolved:
            print(f"  {c}")


if __name__ == "__main__":
    main()