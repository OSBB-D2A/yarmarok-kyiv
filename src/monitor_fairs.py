#!/usr/bin/env python3
"""Hourly monitor for Kyiv seasonal fairs using only the official GIS API."""
from __future__ import annotations

import hashlib
import html
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from build_feed_fair import BASE_URL, FIELDS, KYIV, MAPPING_PATH, OUTPUT_PATH, get_segments, normalize

API_LAYER = 1
REPORT_PATH = Path("outpoot/fairs-report.html")
STATE_PATH = Path("outpoot/fairs-monitor-state.json")
API_JSON_PATH = Path("outpoot/fairs-api.json")
WEEKDAYS = ("понеділок", "вівторок", "середа", "четвер", "п’ятниця", "субота", "неділя")
REPORT_URL = "https://raw.githack.com/SapozhnikUA/yarmarok-kyiv/main/outpoot/fairs-report.html"


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
        return f"{d:%d.%m.%Y}<br><small>{WEEKDAYS[d.weekday()]}</small>"
    except ValueError:
        return iso_date


def mapping_feed_status(api_item: dict, mapping: dict, incidents: list[dict]) -> tuple[bool, str | None, str | None]:
    api_date = api_item.get("date")
    address = normalize(api_item.get("address") or api_item.get("address_kd"))
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
            if api_date and incident.get("starttime", "")[:10] != api_date:
                continue
            if normalize(incident.get("street")) in {normalize(s) for s in streets}:
                first = next((p for p in segments if p[0]), None)
                if first:
                    coord = first[0].split()
                    if len(coord) >= 2:
                        lat, lon = coord[0], coord[1]
                        waze_url = f"https://waze.com/ul?ll={lat}%2C{lon}&navigate=yes"
                        return True, waze_url, str(record.get("place") or incident.get("street") or "")
    return False, None, None


_SEGMENT_CACHE: dict[str, list[tuple[str, str]]] = {}


def render_report(api_rows: list[dict], future_api: list[dict], mapping: dict,
                  incidents: list[dict], api_error: str | None) -> str:
    now_dt = datetime.now(KYIV)
    now = now_dt.strftime("%d.%m.%Y о %H:%M")
    cutoff = (now_dt.date() - timedelta(days=90)).isoformat()
    today_iso = now_dt.date().isoformat()
    history_rows = [r for r in api_rows if cutoff <= (r.get("date") or "") < today_iso]
    template_records = [r for r in mapping.get("records", []) if (r.get("description") or "").strip().casefold() == "ярмарок"]
    template_usage = {}
    for record in template_records:
        segments = get_segments(record)
        streets = list(dict.fromkeys(street for _, street in segments if street))
        key = str(record.get("place") or "")
        template_usage[key] = {"place": key, "streets": streets, "count": 0, "latest": ""}
    unmatched_locations = {}
    for row in history_rows:
        addresses = [normalize(row.get("address")), normalize(row.get("address_kd"))]
        addresses = [a for a in addresses if a]
        matched_key = None
        for key, template in template_usage.items():
            place = normalize(template["place"])
            streets = [normalize(street) for street in template["streets"]]
            if any((place and (place in addr or addr in place)) or
                   any(street and (street in addr or addr in street) for street in streets)
                   for addr in addresses):
                matched_key = key
                break
        if matched_key is not None:
            template_usage[matched_key]["count"] += 1
            template_usage[matched_key]["latest"] = max(template_usage[matched_key]["latest"], row.get("date") or "")
        else:
            address = row.get("address") or row.get("address_kd") or "Адресу не вказано"
            norm_address = normalize(address) or address
            entry = unmatched_locations.setdefault(norm_address, {"address": address, "district": row.get("district") or "—", "count": 0, "latest": "", "ids": []})
            entry["count"] += 1
            entry["latest"] = max(entry["latest"], row.get("date") or "")
            if row.get("objectid") is not None and len(entry["ids"]) < 3:
                entry["ids"].append(str(row["objectid"]))
    archive_rows = []
    for item in sorted(template_usage.values(), key=lambda x: (-x["count"], normalize(x["place"]))):
        if item["count"]:
            archive_rows.append(
                f'<tr><td>Наявний шаблон</td><td>{html.escape(item["place"])}</td>'
                f'<td data-sort="{item["count"]}">{item["count"]}</td>'
                f'<td data-sort="{html.escape(item["latest"])}">{html.escape(item["latest"] or "—")}</td></tr>'
            )
    for item in sorted(unmatched_locations.values(), key=lambda x: (-x["count"], normalize(x["address"]))):
        ids = ", ".join(item["ids"]) or "—"
        archive_rows.append(
            f'<tr><td>Не зіставлено — перевірено</td><td>{html.escape(item["address"])}'
            f'<br><small>{html.escape(item["district"])} · Object ID: {html.escape(ids)}</small></td>'
            f'<td data-sort="{item["count"]}">{item["count"]}</td>'
            f'<td data-sort="{html.escape(item["latest"])}">{html.escape(item["latest"] or "—")}</td></tr>'
        )
    archive_section = "".join(archive_rows) or '<tr><td colspan="4">За цей період записів не знайдено.</td></tr>'
    future_api = sorted(future_api, key=lambda x: (x["date"], x.get("district") or "", normalize(x.get("address") or x.get("address_kd")), x.get("objectid") or 0))
    # In the report, combine duplicate API rows for the same date/location and
    # show every source objectid in one comma-separated cell.
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for item in future_api:
        location = normalize(item.get("address") or item.get("address_kd"))
        key = (item.get("date") or "", item.get("district") or "", location)
        grouped.setdefault(key, []).append(item)
    report_rows = []
    for items in grouped.values():
        item = dict(items[0])
        objectids = sorted(
            {str(row["objectid"]) for row in items if row.get("objectid") is not None},
            key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
        )
        item["objectids"] = ", ".join(objectids) or "—"
        statuses = list(dict.fromkeys(row.get("status") for row in items if row.get("status")))
        item["status"] = ", ".join(statuses) or ""
        report_rows.append(item)
    incident_dates = sorted({i.get("starttime", "")[:10] for i in incidents})
    rows_html = []
    mapped_count = 0
    for item in report_rows:
        sent, waze_url, mapped_place = mapping_feed_status(item, mapping, incidents)
        if sent and waze_url:
            mapped_count += 1
            feed_status = (
                f'<span class="yes">Є у feed</span><br>'
                f'<a href="{html.escape(waze_url, quote=True)}" target="_blank" rel="noopener">Відкрити локацію у Waze</a>'
                + (f'<br><small>{html.escape(mapped_place)}</small>' if mapped_place else "")
            )
        else:
            feed_status = '<span class="no">Не потрапив у feed</span>'
        rows_html.append(
            "<tr>"
            f"<td>{day_label(item.get('date'))}</td>"
            f"<td>{html.escape(item.get('district') or '—')}</td>"
            f"<td>{html.escape(item.get('address') or item.get('address_kd') or '—')}</td>"
            f"<td>{html.escape(item.get('status') or '—')}</td>"
            f"<td>{html.escape(item.get('objectids') or str(item.get('objectid') or '—'))}</td>"
            f"<td>{feed_status}</td></tr>"
        )

    api_section = "".join(rows_html) or '<tr><td colspan="6" class="no">GIS API не повернув записів на сьогодні або майбутні дати.</td></tr>'
    api_error_html = f'<p class="error">Помилка GIS API: {html.escape(api_error)}</p>' if api_error else ""
    status = "Помилка GIS API" if api_error else ("Є майбутні записи в API" if future_api else "API не має майбутніх дат")
    return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Моніторинг ярмарків Києва</title>
<style>
:root{{color-scheme:dark;--bg:#080d18;--panel:#111a2b;--panel2:#172338;--line:#263650;--text:#e8eef8;--muted:#93a4bd;--accent:#7c9cff;--accent2:#54d6c7;--yes:#54d6a0;--no:#ff7b87;--shadow:0 18px 50px #0005}}
*{{box-sizing:border-box}}
body{{margin:0;background:radial-gradient(ellipse at 12% 0%,#1a2c4c 0,transparent 38%),var(--bg);color:var(--text);font:15px/1.6 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh}}
main{{max-width:1500px;margin:0 auto;padding:clamp(18px,4vw,48px)}}
header{{padding:clamp(22px,4vw,42px);border:1px solid var(--line);border-radius:24px;background:linear-gradient(125deg,#172944dd,#101827ee);box-shadow:var(--shadow);position:relative;overflow:hidden}}
header:after{{content:"";position:absolute;width:230px;height:230px;right:-75px;top:-100px;border-radius:50%;background:#728eff18;filter:blur(2px)}}
.eyebrow{{color:var(--accent2);font-size:.76rem;letter-spacing:.15em;text-transform:uppercase;font-weight:750}}
h1{{font-size:clamp(1.8rem,4vw,3rem);line-height:1.12;letter-spacing:-.04em;margin:.55rem 0 1rem;max-width:850px}}
h2{{font-size:1.2rem;letter-spacing:-.02em;margin:32px 0 12px}}
p{{color:#c2cde0}}.meta{{font-size:.88rem;color:var(--muted);margin:0}}
.status{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}}
.metric{{padding:18px 20px;border:1px solid var(--line);border-radius:16px;background:linear-gradient(145deg,#17243a,#101827);min-width:0}}
.metric strong{{display:block;color:var(--muted);font-size:.78rem;font-weight:650;margin-bottom:6px}}
.metric span{{font-size:1.5rem;font-weight:750;letter-spacing:-.03em;overflow-wrap:anywhere}}
.panel{{padding:20px 22px;background:var(--panel);border:1px solid var(--line);border-radius:18px;margin:16px 0}}
.error{{color:#ffadb4;background:#351a27;border:1px solid #713140;border-radius:14px;padding:15px 18px;overflow-wrap:anywhere}}
.scroll{{overflow-x:auto;border:1px solid var(--line);border-radius:16px;background:#0d1523;box-shadow:var(--shadow)}}
table{{width:100%;border-collapse:separate;border-spacing:0;min-width:850px}}
th,td{{padding:13px 15px;border-bottom:1px solid #223149;text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:#18253a;color:#aebed6;font-size:.75rem;text-transform:uppercase;letter-spacing:.07em;white-space:nowrap}}
th.sortable{{cursor:pointer;user-select:none}}th.sortable:after{{content:" ↕";opacity:.55}}th.sort-asc:after{{content:" ↑";opacity:1}}th.sort-desc:after{{content:" ↓";opacity:1}}
.rules{{padding-left:22px;color:#c2cde0}}.rules li{{margin:7px 0}}
tbody tr{{transition:background .15s ease}}tbody tr:hover{{background:#192840}}tbody tr:last-child td{{border-bottom:0}}
.yes{{display:inline-block;color:#7be0b4;background:#15392f;padding:3px 9px;border-radius:999px;font-size:.8rem;font-weight:700}}
.no{{display:inline-block;color:#ff9da7;background:#3a202b;padding:3px 9px;border-radius:999px;font-size:.8rem;font-weight:700}}
a{{color:#9bb4ff;text-decoration:none}}a:hover{{color:#d1dcff;text-decoration:underline}}small{{display:inline-block;color:var(--muted);margin-top:5px}}ul{{padding-left:20px;color:var(--muted)}}li{{margin:8px 0}}
footer{{margin-top:28px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:.82rem}}
@media(max-width:640px){{main{{padding:14px}}header{{border-radius:18px}}.panel{{padding:15px}}.metric{{padding:14px}}}}
</style></head><body><main>
<header><div class="eyebrow">Kyiv · GIS data monitor</div>
<h1>Моніторинг ярмарків Києва</h1>
<p class="meta">Оновлено: {html.escape(now)} · Автоматична перевірка щогодини</p></header>
<div class="status">
<div class="metric"><strong>Стан джерела</strong><span>{html.escape(status)}</span></div>
<div class="metric"><strong>Записи GIS API</strong><span>{len(future_api)}</span></div>
<div class="metric"><strong>Унікальні локації / дати</strong><span>{len(report_rows)}</span></div>
<div class="metric"><strong>Інциденти у Waze feed</strong><span>{len(incidents)}</span></div>
<div class="metric"><strong>Зіставлено з геометрією</strong><span>{mapped_count}</span></div>
</div>
{api_error_html}
<section class="panel"><h2>Як формується Waze feed</h2>
<ul class="rules">
<li>Єдине джерело дат та адрес — офіційний GIS API, шар «Заплановані».</li>
<li>До Waze feed потрапляють лише записи, для яких знайдено геометрію в mapping-файлі.</li>
<li>Час інцидентів — 07:00–17:00 за київським часом.</li>
<li>Дублікати API з однаковою датою та адресою згруповано, усі Object ID наведено в одному рядку.</li>
</ul>
<p class="meta">Дати у feed: {html.escape(", ".join(incident_dates) or "немає")}</p></section>
<h2>Заплановані ярмарки</h2>
<div class="scroll"><table class="sortable-table"><thead><tr><th>Дата</th><th>Район</th><th>Адреса з API</th><th>Статус API</th><th>Object ID</th><th>У feed / Waze</th></tr></thead><tbody>{api_section}</tbody></table></div>
<h2>Порівняння з архівом GIS API за останні 90 днів</h2>
<p class="meta">Період: {cutoff} — {now_dt.date().isoformat()}. Адреси, які не зіставилися з наявними шаблонами, перевірені вручну; нові шаблони за результатами перевірки не додавалися.</p>
<div class="scroll"><table class="sortable-table"><thead><tr><th>Тип</th><th>Шаблон / адреса з архіву</th><th>Кількість записів</th><th>Остання дата</th></tr></thead><tbody>{archive_section}</tbody></table></div>
<h2>Джерела та файли</h2><section class="panel"><ul>
<li><a href="https://gisserver.kyivcity.gov.ua/mayno/rest/services/KYIV_API/Trade_api/MapServer/1" target="_blank" rel="noopener">Офіційний GIS API — шар «Заплановані»</a></li>
<li><a href="https://github.com/SapozhnikUA/yarmarok-kyiv/blob/main/outpoot/feed_fair.json" target="_blank" rel="noopener">Поточний Waze feed JSON</a></li>
<li><a href="https://github.com/SapozhnikUA/yarmarok-kyiv/blob/main/outpoot/fairs-api.json" target="_blank" rel="noopener">Знімок даних GIS API</a></li>
<li><a href="https://github.com/SapozhnikUA/yarmarok-kyiv/blob/main/data/waze-location-mapping.json" target="_blank" rel="noopener">Шаблони геометрії Waze</a></li>
</ul></section><footer>SapozhnikUA/yarmarok-kyiv · Автоматичний моніторинг ярмарків Києва</footer>
<script>
document.querySelectorAll("table.sortable-table").forEach(table => {
  table.querySelectorAll("thead th").forEach((th, index) => {
    th.classList.add("sortable"); th.tabIndex = 0; th.setAttribute("role", "button");
    th.setAttribute("aria-label", "Сортувати за стовпцем " + th.textContent.trim());
    const sort = () => {
      const tbody = table.tBodies[0], ascending = !th.classList.contains("sort-asc");
      table.querySelectorAll("thead th").forEach(cell => cell.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(ascending ? "sort-asc" : "sort-desc");
      const rows = Array.from(tbody.rows);
      rows.sort((a, b) => {
        const ac = a.cells[index], bc = b.cells[index];
        const av = (ac.dataset.sort || ac.innerText || "").trim(), bv = (bc.dataset.sort || bc.innerText || "").trim();
        const an = Number(av), bn = Number(bv);
        const numeric = av !== "" && bv !== "" && Number.isFinite(an) && Number.isFinite(bn);
        const cmp = numeric ? an - bn : av.localeCompare(bv, "uk", {numeric:true, sensitivity:"base"});
        return (ascending ? 1 : -1) * cmp;
      });
      rows.forEach(row => tbody.appendChild(row));
    };
    th.addEventListener("click", sort);
    th.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); sort(); } });
  });
});
</script>
</main></body></html>
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
    api_rows: list[dict] = []
    api_error = None
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

    # Canonical ordering prevents notification noise caused only by API row order.
    api_rows.sort(key=lambda row: (
        row.get("date") or "",
        row.get("objectid") if row.get("objectid") is not None else 2**63,
        normalize(row.get("address")),
        normalize(row.get("address_kd")),
    ))
    today = now.date().isoformat()
    future_api = [row for row in api_rows if row["date"] >= today]
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    # Keep the last known-good feed if the API is empty or feed generation fails.
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
    REPORT_PATH.write_text(render_report(api_rows, future_api, mapping, incidents, api_error), encoding="utf-8")

    # Persist outage state only when it changes; this avoids hourly commits.
    # If the GIS API stays unavailable for 14 full days, alert on day 15 and
    # repeat every 7 days until a successful API response clears the outage.
    try:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}
    state = dict(previous)
    changed = False
    digest = hashlib.sha256(json.dumps(
        {"api_rows": api_rows, "api_error": None}, ensure_ascii=False, sort_keys=True
    ).encode()).hexdigest()

    if api_error:
        if not state.get("api_failure_since"):
            state["api_failure_since"] = now.isoformat(timespec="seconds")
            changed = True
        try:
            failure_since = datetime.fromisoformat(state["api_failure_since"])
            if failure_since.tzinfo is None:
                failure_since = failure_since.replace(tzinfo=KYIV)
            outage_days = (now - failure_since).total_seconds() / 86400
        except (ValueError, TypeError):
            failure_since = now
            outage_days = 0
            state["api_failure_since"] = now.isoformat(timespec="seconds")
            changed = True

        last_alert = state.get("last_api_failure_alert_at")
        alert_due = outage_days >= 14
        if last_alert:
            try:
                last_alert_dt = datetime.fromisoformat(last_alert)
                if last_alert_dt.tzinfo is None:
                    last_alert_dt = last_alert_dt.replace(tzinfo=KYIV)
                alert_due = alert_due and (now - last_alert_dt).total_seconds() >= 7 * 86400
            except (ValueError, TypeError):
                alert_due = outage_days >= 14
        if alert_due and not os.getenv("SKIP_TELEGRAM"):
            msg = (
                "🚨 <b>Помилка моніторингу ярмарків Києва</b>\n"
                "Не вдається отримати дані з офіційного GIS API вже "
                f"{int(outage_days)} днів.\n"
                f"<b>Остання помилка:</b> {html.escape(api_error)}\n"
                "Наступне нагадування надійде через 7 днів, якщо доступ не відновиться.\n"
                f'📊 <a href="{REPORT_URL}">Відкрити сторінку звіту</a>'
            )
            try:
                send_telegram(msg)
                state["last_api_failure_alert_at"] = now.isoformat(timespec="seconds")
                changed = True
            except Exception as exc:
                print(f"TELEGRAM ERROR: {exc}", file=sys.stderr)
                raise
        elif alert_due and os.getenv("SKIP_TELEGRAM"):
            print("SKIP_TELEGRAM set; outage alert suppressed during workflow retry.")
        print(f"GIS API unavailable for {int(outage_days)} days; outage alert due={alert_due}.")
    else:
        # Successful response ends the consecutive outage period.
        if state.pop("api_failure_since", None) is not None:
            changed = True
        if state.pop("last_api_failure_alert_at", None) is not None:
            changed = True
        if state.get("digest") != digest:
            state["digest"] = digest
            state["checked_at"] = now.isoformat(timespec="seconds")
            changed = True
            if not os.getenv("SKIP_TELEGRAM"):
                msg = (
                    "🔔 <b>Оновлено дані про ярмарки Києва</b>\n"
                    f"GIS API: {len(future_api)} записів на сьогодні/майбутнє.\n"
                    f"У Waze feed зараз {len(incidents)} інцидентів.\n"
                    f'📊 <a href="{REPORT_URL}">Відкрити сторінку звіту</a>'
                )
                try:
                    send_telegram(msg)
                except Exception as exc:
                    print(f"TELEGRAM ERROR: {exc}", file=sys.stderr)
                    raise
        else:
            print("No GIS API data changes; Telegram notification not sent.")

    if changed:
        save_json(STATE_PATH, state)
    print(f"Report saved: {REPORT_PATH}; future API rows: {len(future_api)}; feed incidents: {len(incidents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
