#!/usr/bin/env python3
"""Hourly monitor for Kyiv seasonal fairs: official website, GIS API, Waze feed and Telegram."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scrape_fairs import DEFAULT_URL, fetch_page
from build_feed_fair import BASE_URL, FIELDS, KYIV, MAPPING_PATH, OUTPUT_PATH, get_segments, normalize

API_LAYER = 1
REPORT_PATH = Path("outpoot/fairs-report.html")
STATE_PATH = Path("outpoot/fairs-monitor-state.json")
SITE_JSON_PATH = Path("outpoot/fairs-website.json")
API_JSON_PATH = Path("outpoot/fairs-api.json")
WEEKDAYS = ("понеділок", "вівторок", "середа", "четвер", "п’ятниця", "субота", "неділя")


def fetch_api_rows() -> list[dict]:
    params = urllib.parse.urlencode({
        "where": "1=1",
        "outFields": FIELDS,
        "returnGeometry": "false",
        "orderByFields": "date_release ASC",
        "resultRecordCount": "10000",
        "f": "json",
    })
    url = f"{BASE_URL}/{API_LAYER}/query?{params}"
    request = urllib.request.Request(url, headers={
        "User-Agent": "OSBB-D2A-yarmarok-kyiv/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=40) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(f"GIS API error: {payload['error']}")
    rows = []
    for feature in payload.get("features", []):
        a = feature.get("attributes", {})
        stamp = a.get("date_release")
        if stamp is None:
            continue
        dt = datetime.fromtimestamp(stamp / 1000, tz=ZoneInfo("UTC")).astimezone(KYIV)
        rows.append({
            "date": dt.date().isoformat(),
            "objectid": a.get("objectid"),
            "address": a.get("address") or "",
            "address_kd": a.get("address_kd") or "",
            "district": a.get("district") or "",
            "status": a.get("status_text") or "",
            "organizer": a.get("organizer") or "",
        })
    return rows


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def day_label(iso_date: str | None) -> str:
    if not iso_date:
        return "Дату не визначено"
    try:
        d = datetime.fromisoformat(iso_date).date()
        return f"{d:%d.%m.%Y} ({WEEKDAYS[d.weekday()]})"
    except ValueError:
        return iso_date


def api_match(site_item: dict, api_rows: list[dict]) -> list[dict]:
    address = normalize(site_item.get("address"))
    site_date = site_item.get("date")
    matches = []
    for row in api_rows:
        if site_date and row["date"] != site_date:
            continue
        values = [normalize(row.get("address")), normalize(row.get("address_kd"))]
        values = [v for v in values if v]
        if address and any(address in v or v in address for v in values):
            matches.append(row)
            continue
        # If the article gives a street plus house range, compare the street part too.
        site_street = re.split(r",\s*\d| \(", address, maxsplit=1)[0].strip()
        if len(site_street) >= 8 and any(site_street in v or v in site_street for v in values):
            matches.append(row)
    return matches


_SEGMENT_CACHE: dict[str, list[tuple[str, str]]] = {}

def mapping_feed_status(site_item: dict, mapping: dict, incidents: list[dict]) -> tuple[bool, str | None]:
    site_date = site_item.get("date")
    address = normalize(site_item.get("address"))
    candidates = []
    for record in mapping.get("records", []):
        if (record.get("description") or "").strip().casefold() != "ярмарок":
            continue
        cache_key = str(record.get("place") or "") + "|" + str(record.get("hash") or "")
        if cache_key not in _SEGMENT_CACHE:
            _SEGMENT_CACHE[cache_key] = get_segments(record)
        segments = _SEGMENT_CACHE[cache_key]
        place = normalize(record.get("place"))
        streets = list(dict.fromkeys(street for _, street in segments))
        norm_streets = [normalize(street) for street in streets]
        place_match = bool(place and (place in address or address in place))
        street_match = any(s and (s in address or address in s) for s in norm_streets)
        if place_match or street_match:
            candidates.append((record, segments, streets))
    for record, segments, streets in candidates:
        for incident in incidents:
            if site_date and incident.get("starttime", "")[:10] != site_date:
                continue
            if normalize(incident.get("street")) in {normalize(s) for s in streets}:
                first = next((p for p in segments if p[0]), None)
                if first:
                    coord = first[0].split()
                    if len(coord) >= 2:
                        lat, lon = coord[0], coord[1]
                        return True, f"https://waze.com/ul?ll={lat}%2C{lon}&navigate=yes"
    return False, None


def render_report(site_data: dict, api_rows: list[dict], future_api: list[dict], mapping: dict,
                  incidents: list[dict], api_error: str | None, site_error: str | None) -> str:
    now = datetime.now(KYIV).isoformat(timespec="seconds")
    site_items = site_data.get("fairs", []) if site_data else []
    site_items = sorted(site_items, key=lambda x: (x.get("date") or "9999", x.get("district") or "", x.get("address") or ""))
    future_api = sorted(future_api, key=lambda x: (x["date"], x.get("district") or "", x.get("address") or ""))
    incident_dates = sorted({i.get("starttime", "")[:10] for i in incidents})
    rows_html = []
    for item in site_items:
        matches = api_match(item, api_rows)
        sent, waze_url = mapping_feed_status(item, mapping, incidents)
        if matches:
            api_status = f'<span class="yes">Є збіг в API ({len(matches)})</span>'
        else:
            api_status = '<span class="no">Не знайдено в API для цієї дати</span>'
        feed_status = (
            f'<span class="yes">Є у feed · <a href="{html.escape(waze_url, quote=True)}" target="_blank" rel="noopener">відкрити локацію у Waze</a></span>'
            if sent and waze_url else '<span class="no">Не потрапила у feed</span>'
        )
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(day_label(item.get('date')))}</td>"
            f"<td>{html.escape(item.get('district') or '—')}</td>"
            f"<td>{html.escape(item.get('address') or '—')}</td>"
            f"<td>{api_status}</td><td>{feed_status}</td></tr>"
        )

    api_html = []
    today = datetime.now(KYIV).date().isoformat()
    for row in future_api:
        api_html.append(
            "<tr>"
            f"<td>{html.escape(day_label(row['date']))}</td>"
            f"<td>{html.escape(row.get('district') or '—')}</td>"
            f"<td>{html.escape(row.get('address') or row.get('address_kd') or '—')}</td>"
            f"<td>{html.escape(row.get('status') or '—')}</td>"
            f"<td>{html.escape(str(row.get('objectid') or '—'))}</td></tr>"
        )
    api_section = "".join(api_html) or '<tr><td colspan="5" class="no">API не повернув записів на сьогодні або майбутні дати.</td></tr>'
    site_section = "".join(rows_html) or '<tr><td colspan="5" class="no">Не вдалося отримати список із сайту.</td></tr>'
    api_error_html = f'<p class="error">Помилка API: {html.escape(api_error)}</p>' if api_error else ""
    site_error_html = f'<p class="error">Помилка сайту: {html.escape(site_error)}</p>' if site_error else ""
    status = "Є майбутні записи в API" if future_api else "API не має майбутніх дат"
    return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Моніторинг сезонних ярмарків Києва</title>
<style>
:root{{color-scheme:light dark;--line:#8885;--muted:#777;--yes:#16803c;--no:#b42318}}
body{{font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1500px;margin:0 auto;padding:24px}}
h1{{line-height:1.2}} .meta{{color:var(--muted)}} .status{{padding:12px 16px;border:1px solid var(--line);border-radius:10px;margin:16px 0}}
table{{width:100%;border-collapse:collapse;margin:12px 0 28px}}th,td{{padding:9px 10px;border:1px solid var(--line);text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:Canvas}}tr:nth-child(even){{background:color-mix(in srgb,CanvasText 4%,Canvas)}}
.yes{{color:var(--yes);font-weight:650}}.no,.error{{color:var(--no);font-weight:650}}a{{overflow-wrap:anywhere}}.scroll{{overflow-x:auto}}small{{color:var(--muted)}}
</style></head><body>
<h1>Моніторинг сезонних ярмарків Києва</h1>
<p class="meta">Оновлено: {html.escape(now)} · Часовий пояс Europe/Kyiv</p>
<div class="status"><strong>Стан:</strong> {html.escape(status)}<br>
Записи сайту: {len(site_items)} · Записи API на сьогодні/майбутнє: {len(future_api)} · Інциденти у Waze feed: {len(incidents)} · Дати у feed: {html.escape(", ".join(incident_dates) or "немає")}</div>
{site_error_html}{api_error_html}
<h2>Порівняння: офіційний сайт → API → Waze feed</h2>
<p>Сайт — перелік із новини Порталу Києва. API — записи шару «Заплановані» GIS. У feed додаються лише ті записи API, які зіставлено з геометрією в mapping-файлі. Посилання Waze відкриває координати відповідної геометрії.</p>
<div class="scroll"><table><thead><tr><th>Дата</th><th>Район</th><th>Локація із сайту</th><th>Збіг в API</th><th>У feed / Waze</th></tr></thead><tbody>{site_section}</tbody></table></div>
<h2>Записи API на сьогодні та майбутні дати</h2>
<div class="scroll"><table><thead><tr><th>Дата</th><th>Район</th><th>Адреса</th><th>Статус API</th><th>Object ID</th></tr></thead><tbody>{api_section}</tbody></table></div>
<h2>Файли та джерела</h2><ul>
<li><a href="{html.escape(DEFAULT_URL, quote=True)}" target="_blank" rel="noopener">Офіційна новина Порталу Києва</a></li>
<li><a href="https://gisserver.kyivcity.gov.ua/mayno/rest/services/KYIV_API/Trade_api/MapServer/1" target="_blank" rel="noopener">GIS API — шар «Заплановані»</a></li>
<li><a href="https://github.com/OSBB-D2A/yarmarok-kyiv/blob/main/outpoot/feed_fair.json">Поточний Waze feed JSON</a></li>
</ul>
</body></html>
"""


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram notification skipped: configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID repository secrets.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned an unsuccessful response: {result}")


def main() -> int:
    now = datetime.now(KYIV)
    site_data = None
    site_error = None
    api_rows: list[dict] = []
    api_error = None
    try:
        site_data = fetch_page(os.getenv("FAIRS_URL", DEFAULT_URL))
        save_json(SITE_JSON_PATH, site_data)
    except Exception as exc:
        site_error = str(exc)
        print(f"WARNING: website collection failed: {site_error}", file=sys.stderr)
        # Keep the last known website snapshot so the report remains useful when
        # the portal temporarily returns HTTP 403 or is unavailable.
        try:
            site_data = json.loads(SITE_JSON_PATH.read_text(encoding="utf-8"))
            print(f"Using saved website snapshot ({len(site_data.get('fairs', []))} locations).")
        except (FileNotFoundError, json.JSONDecodeError):
            site_data = None
    try:
        api_rows = fetch_api_rows()
        save_json(API_JSON_PATH, {
            "source": f"{BASE_URL}/{API_LAYER}",
            "checked_at": now.isoformat(timespec="seconds"),
            "record_count": len(api_rows),
            "records": api_rows,
        })
    except Exception as exc:
        api_error = str(exc)
        print(f"WARNING: GIS API collection failed: {api_error}", file=sys.stderr)

    today = now.date().isoformat()
    future_api = [row for row in api_rows if row["date"] >= today]
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    # Do not replace the last known-good feed with an empty feed when API is stale.
    if future_api:
        result = subprocess.run([sys.executable, "src/build_feed_fair.py"], text=True, capture_output=True)
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode:
            print("WARNING: feed generation failed; retaining the last known-good feed.", file=sys.stderr)
    else:
        print(f"GIS API has no records dated {today} or later; retaining existing feed.")

    incidents = []
    try:
        incidents = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("incidents", [])
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        render_report(site_data or {}, api_rows, future_api, mapping, incidents, api_error, site_error),
        encoding="utf-8",
    )

    # Notify only when the fetched source data changes, not on every hourly run.
    snapshot = {
        "site_fairs": (site_data or {}).get("fairs", []),
        "api_rows": api_rows,
        "site_error": site_error,
        "api_error": api_error,
    }
    digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    previous = {}
    try:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if previous.get("digest") != digest:
        save_json(STATE_PATH, {"digest": digest, "checked_at": now.isoformat(timespec="seconds")})
        if not os.getenv("SKIP_TELEGRAM"):
            site_count = len((site_data or {}).get("fairs", []))
            msg = (
                "🔔 <b>Оновлено дані про ярмарки Києва</b>\n"
                f"Сайт: {site_count} локацій; GIS API: {len(future_api)} записів на сьогодні/майбутнє.\n"
                f"У Waze feed зараз {len(incidents)} інцидентів.\n"
                "Звіт: https://github.com/OSBB-D2A/yarmarok-kyiv/blob/main/outpoot/fairs-report.html"
            )
            try:
                send_telegram(msg)
            except Exception as exc:
                # A notification failure must fail visibly, but the report and data are preserved.
                print(f"TELEGRAM ERROR: {exc}", file=sys.stderr)
                raise
    else:
        print("No source data changes; Telegram notification not sent.")
    print(f"Report saved: {REPORT_PATH}; website locations: {len((site_data or {}).get('fairs', []))}; future API rows: {len(future_api)}; feed incidents: {len(incidents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
