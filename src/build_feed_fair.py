#!/usr/bin/env python3
"""Build a Waze CIFS-style feed for fairs using Kyiv's official GIS API dates."""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = (
    "https://gisserver.kyivcity.gov.ua/mayno/rest/services/"
    "KYIV_API/Trade_api/MapServer"
)
PLANNED_LAYER = 1
MAPPING_PATH = Path("data/waze-location-mapping.json")
OUTPUT_PATH = Path("outpoot/feed_fair.json")
KYIV = ZoneInfo("Europe/Kyiv")
FIELDS = "objectid,address,district,organizer,status_text,date_release,address_kd"

PREFIXES = (
    "бульвар", "бульв", "б-р", "проспект", "просп", "пр-т",
    "вулиця", "вул", "провулок", "пров", "провул", "площа", "пл",
    "шосе", "узвіз", "набережна", "наб",
)


def normalize(value: str | None) -> str:
    text = (value or "").casefold().replace("’", "'")
    text = re.sub(r"[^\w\s'-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"[-']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    while words and words[0].rstrip(".") in PREFIXES:
        words.pop(0)
    return " ".join(words)


def get_segments(record: dict) -> list[tuple[str, str]]:
    result = []
    for raw_segment in (record.get("hash") or "").split(";"):
        segment = raw_segment.strip()
        if not segment:
            continue
        parts = segment.split("@")
        if len(parts) < 2:
            print(f"WARNING: malformed geometry segment for {record.get('place')!r}", file=sys.stderr)
            continue
        polyline = parts[0].strip()
        street = parts[1].strip()
        if polyline and street:
            result.append((polyline, street))
        else:
            print(f"WARNING: missing polyline/street for {record.get('place')!r}", file=sys.stderr)
    return result


def fetch_planned_rows() -> list[dict]:
    params = urllib.parse.urlencode({
        "where": "1=1",
        "outFields": FIELDS,
        "returnGeometry": "false",
        "orderByFields": "date_release ASC",
        "resultRecordCount": "10000",
        "f": "json",
    })
    url = f"{BASE_URL}/{PLANNED_LAYER}/query?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "OSBB-D2A-yarmarok-kyiv/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(f"GIS API error: {payload['error']}")

    today = datetime.now(KYIV).date()
    rows = []
    all_dates = []
    features = payload.get("features", [])
    for feature in features:
        attrs = feature.get("attributes", {})
        stamp = attrs.get("date_release")
        if stamp is None:
            continue
        fair_date = datetime.fromtimestamp(stamp / 1000, tz=ZoneInfo("UTC")).astimezone(KYIV).date()
        all_dates.append(fair_date)
        if fair_date < today:
            continue
        rows.append({
            "date": fair_date,
            "objectid": attrs.get("objectid"),
            "address": attrs.get("address") or "",
            "address_kd": attrs.get("address_kd") or "",
            "district": attrs.get("district") or "",
            "status": attrs.get("status_text") or "",
            "organizer": attrs.get("organizer") or "",
        })

    print(f"GIS API planned layer: {len(features)} records returned; {len(rows)} on or after {today.isoformat()}.")
    if all_dates:
        print(
            "GIS API date range: "
            f"{min(all_dates).isoformat()}–{max(all_dates).isoformat()} "
            f"(Kyiv local dates)."
        )
    else:
        print("GIS API returned no records with a date_release value.")
    return rows


def address_match_score(api_row: dict, mapping_record: dict, streets: list[str]) -> int:
    api_values = [normalize(api_row.get("address")), normalize(api_row.get("address_kd"))]
    api_values = [value for value in api_values if value]
    place = normalize(mapping_record.get("place"))
    street_values = [normalize(street) for street in streets]
    street_values = [value for value in street_values if value]

    # Prefer the full mapped location/address, including a house-number range.
    if place and any(place in value or value in place for value in api_values):
        return 100 + len(place)
    # A street-name match is a fallback; callers only use it when unambiguous.
    if street_values and any(street in value or value in street for street in street_values for value in api_values):
        return 10 + max(map(len, street_values))
    return 0


def main() -> int:
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    records = [
        row for row in mapping.get("records", [])
        if row.get("description", "").strip().casefold() == "ярмарок"
    ]
    if not records:
        raise RuntimeError("No records with description='Ярмарок' in mapping file.")

    mapped = []
    for record in records:
        segments = get_segments(record)
        if segments:
            mapped.append({
                "record": record,
                "segments": segments,
                "streets": list(dict.fromkeys(street for _, street in segments)),
            })

    api_rows = fetch_planned_rows()
    if not api_rows:
        raise RuntimeError(
            "The official GIS API has no planned fair dates for today or later. "
            "No new feed was written; the existing feed was left unchanged. "
            "Check whether Kyiv has published the next dates in the API's 'Заплановані' layer."
        )

    # Street fallback is accepted only when it identifies one mapping record.
    street_to_records: dict[str, set[int]] = defaultdict(set)
    for idx, item in enumerate(mapped):
        for street in item["streets"]:
            street_to_records[normalize(street)].add(idx)

    # Process API rows deterministically. If the API contains duplicate records
    # for the same date/location, the lowest objectid is handled first.
    api_rows.sort(key=lambda row: (
        row["date"],
        normalize(row.get("address")),
        normalize(row.get("address_kd")),
        row.get("objectid") if row.get("objectid") is not None else 2**63,
    ))

    incidents = []
    unmatched = []
    seen_location_dates: set[tuple[date, int]] = set()
    for api_row in api_rows:
        scored = []
        for idx, item in enumerate(mapped):
            score = address_match_score(api_row, item["record"], item["streets"])
            if score >= 100:
                scored.append((score, idx, True))
            elif score > 0:
                for street in item["streets"]:
                    norm_street = normalize(street)
                    if norm_street and len(street_to_records[norm_street]) == 1:
                        scored.append((score, idx, False))
                        break

        if not scored:
            unmatched.append(f"{api_row['date'].isoformat()} | {api_row['address'] or api_row['address_kd']}")
            continue

        # Keep all exact matches (a date may cover several mapped segments); if
        # none exists, keep only a unique street fallback to avoid false positives.
        exact = [entry for entry in scored if entry[2]]
        selected = exact if exact else (scored if len({entry[1] for entry in scored}) == 1 else [])
        if not selected:
            unmatched.append(f"{api_row['date'].isoformat()} | ambiguous: {api_row['address'] or api_row['address_kd']}")
            continue

        fair_date = api_row["date"]
        start = datetime.combine(fair_date, time(7, 0), tzinfo=KYIV).isoformat(timespec="seconds")
        end = datetime.combine(fair_date, time(17, 0), tzinfo=KYIV).isoformat(timespec="seconds")
        for _, idx, _ in selected:
            # One mapped geometry per date: the GIS API sometimes returns
            # multiple objectids for the same fair at the same location/date.
            location_date = (fair_date, idx)
            if location_date in seen_location_dates:
                print(
                    f"DEDUPLICATED API row {api_row.get('objectid')} for "
                    f"{fair_date.isoformat()} | {mapped[idx]['record'].get('place')}",
                    file=sys.stderr,
                )
                continue
            seen_location_dates.add(location_date)
            item = mapped[idx]
            for segment_index, (polyline, street) in enumerate(item["segments"], start=1):
                # Stable unique ID per API date, API record, and geometry segment.
                api_id = api_row.get("objectid")
                id_part = str(api_id) if api_id is not None else f"{idx + 1:03d}"
                incident_id = f"UA{fair_date:%Y%m%d}_{id_part}_{segment_index}"
                incidents.append({
                    "polyline": polyline,
                    "direction": item["record"].get("DIRECTIONS") or "BOTH_DIRECTIONS",
                    "starttime": start,
                    "endtime": end,
                    "description": "Ярмарок",
                    "event": "Fair",
                    "id": incident_id,
                    "type": "ROAD_CLOSED",
                    "subtype": "ROAD_CLOSED_EVENT",
                    "street": street,
                })

    # Guard against duplicate IDs if the API includes duplicate records.
    unique = {}
    for incident in incidents:
        unique[incident["id"]] = incident
    incidents = list(unique.values())
    incidents.sort(key=lambda row: (row["starttime"], row["street"], row["id"]))
    if not incidents:
        details = "\n".join(unmatched[:20])
        raise RuntimeError(
            "No API records could be matched to mapped fair locations. "
            "Feed was not written. Unmatched examples:\n" + details
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps({"incidents": incidents}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Generated {OUTPUT_PATH}: {len(incidents)} incidents from {len(api_rows)} upcoming API records.")
    print(f"Included mapping locations: {len({incident['street'] for incident in incidents})} street names.")
    if unmatched:
        print(f"API records not matched to the geometry mapping: {len(unmatched)}")
        for line in unmatched[:20]:
            print(f"  UNMATCHED: {line}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FEED BUILD ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
