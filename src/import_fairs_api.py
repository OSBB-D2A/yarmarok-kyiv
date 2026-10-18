#!/usr/bin/env python3
"""Diagnostic import of Kyiv fair records from the official GIS API.

Fetches planned, conducted and cancelled fair-zone records for 13–18 October
2026. This is a diagnostic step before replacing the blocked news-page scraper.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = "https://gisserver.kyivcity.gov.ua/mayno/rest/services/KYIV_API/Trade_api/MapServer"
LAYERS = {
    1: "planned",
    4: "conducted",
    7: "cancelled",
}
START = date(2026, 10, 13)
END = date(2026, 10, 18)
KYIV = ZoneInfo("Europe/Kyiv")
FIELDS = "objectid,address,district,organizer,status_text,date_release,address_kd"


def fetch_layer(layer_id: int, label: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "where": "1=1",
        "outFields": FIELDS,
        "returnGeometry": "false",
        "orderByFields": "date_release ASC",
        "resultRecordCount": "10000",
        "f": "json",
    })
    url = f"{BASE_URL}/{layer_id}/query?{params}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "OSBB-D2A-yarmarok-kyiv/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if "error" in payload:
        raise RuntimeError(f"GIS API layer {layer_id} ({label}) error: {payload['error']}")

    rows = []
    for feature in payload.get("features", []):
        attrs = feature.get("attributes", {})
        stamp = attrs.get("date_release")
        if stamp is None:
            continue
        dt = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone(KYIV)
        if START <= dt.date() <= END:
            rows.append({
                "date": dt.date().isoformat(),
                "date_time_kyiv": dt.isoformat(),
                "source_layer": label,
                "objectid": attrs.get("objectid"),
                "district": attrs.get("district"),
                "address": attrs.get("address"),
                "address_kd": attrs.get("address_kd"),
                "status": attrs.get("status_text"),
                "organizer": attrs.get("organizer"),
            })
    return rows


def main() -> int:
    out = Path("output")
    out.mkdir(parents=True, exist_ok=True)
    all_rows = []
    for layer_id, label in LAYERS.items():
        rows = fetch_layer(layer_id, label)
        print(f"Layer {layer_id} ({label}): {len(rows)} records for {START}–{END}")
        all_rows.extend(rows)

    all_rows.sort(key=lambda r: (r["date"], r["district"] or "", r["address"] or ""))
    if not all_rows:
        raise RuntimeError(
            f"GIS API returned no fair-zone records for {START}–{END}. "
            "Check API layer IDs, date field, and timezone before drawing conclusions."
        )

    (out / "fairs-api.json").write_text(
        json.dumps({
            "source": BASE_URL,
            "period": {"from": START.isoformat(), "to": END.isoformat()},
            "record_count": len(all_rows),
            "records": all_rows,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        f"Kyiv fairs from official GIS API: {START}–{END}",
        f"Total records: {len(all_rows)}",
        "",
    ]
    for row in all_rows:
        lines.append(
            f'{row["date"]} | {row["district"] or "Район не вказано"} | '
            f'{row["address"] or row["address_kd"] or "Адреса не вказана"} | '
            f'{row["status"] or row["source_layer"]}'
        )
    (out / "fairs-api.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved {len(all_rows)} records to output/fairs-api.json and output/fairs-api.txt")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"API IMPORT ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
