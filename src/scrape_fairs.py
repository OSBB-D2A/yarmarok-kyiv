from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_URL = (
    "https://kyivcity.gov.ua/news/"
    "tsogo_tizhnya_u_stolitsi_proydut_sezonni_yarmarki_perelik_lokatsiy_1053155/"
)
SOURCE_URL = os.getenv("FAIRS_URL", DEFAULT_URL)
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))

MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s+("
    + "|".join(MONTHS.keys())
    + r")(?:\s+(\d{4})\s*року?)?"
    r"(?:\s*\(([^)]{3,20})\))?",
    re.IGNORECASE,
)
DISTRICT_RE = re.compile(
    r"^(?:у|в)?\s*"
    r"(.+?районі?)\s*[–—-]\s*(.+)$",
    re.IGNORECASE,
)
PUBLISHED_RE = re.compile(
    r"(\d{1,2})\s+(" + "|".join(MONTHS.keys()) + r")\s+(\d{4})\s+року",
    re.IGNORECASE,
)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().replace("\xa0", " ")


def extract_date(text: str, default_year: int | None) -> tuple[str | None, str] | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    day = int(match.group(1))
    month_name = match.group(2).lower()
    year = int(match.group(3)) if match.group(3) else default_year
    weekday = clean_text(match.group(4) or "")
    date_text = f"{day} {month_name}"
    if weekday:
        date_text += f" ({weekday})"
    iso_date = None
    if year and month_name in MONTHS:
        try:
            iso_date = date(year, MONTHS[month_name], day).isoformat()
        except ValueError:
            iso_date = None
    return iso_date, date_text


def published_year(text: str) -> int | None:
    match = PUBLISHED_RE.search(text)
    if match:
        return int(match.group(3))
    return None


def parse_fair_line(text: str) -> tuple[str, str] | None:
    text = clean_text(text).lstrip("•*-–— ")
    match = DISTRICT_RE.match(text)
    if not match:
        return None
    district = clean_text(match.group(1))
    address = clean_text(match.group(2)).rstrip(";.")
    if "ринок" in district.lower() or not address:
        return None
    if len(address) > 350 or len(district) > 100:
        return None
    return district, address


def fetch_page(url: str) -> dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if response is not None and response.status >= 400:
                raise RuntimeError(f"Сайт повернув HTTP {response.status}: {url}")
            try:
                page.locator("body").wait_for(state="visible", timeout=15000)
                page.wait_for_timeout(1200)
            except PlaywrightTimeoutError:
                raise RuntimeError("Сторінка не стала доступною для читання.")

            title = clean_text(page.title())
            page_text = clean_text(page.locator("body").inner_text(timeout=15000))
            lowered = page_text.lower()
            challenge_markers = (
                "checking your browser", "verify you are human",
                "перевірка безпеки", "captcha", "access denied",
            )
            if any(marker in lowered for marker in challenge_markers) and "районі" not in lowered:
                raise RuntimeError(
                    "Замість новини сайт показав сторінку перевірки доступу."
                )

            candidates = page.locator(
                "article, main, [itemprop='articleBody'], "
                ".news-detail, .news-content, .article-content, .content"
            )
            containers: list[tuple[int, str]] = []
            for i in range(min(candidates.count(), 30)):
                try:
                    text = clean_text(candidates.nth(i).inner_text(timeout=2000))
                    if text and ("ярмар" in text.lower()) and ("район" in text.lower()):
                        containers.append((len(text), text))
                except Exception:
                    continue
            article_text = max(containers, key=lambda x: x[0])[1] if containers else page_text

            root_selector = (
                "article, [itemprop='articleBody'], .news-detail, "
                ".news-content, .article-content, main"
            )
            root = page.locator(root_selector).filter(
                has_text=re.compile("ярмар", re.IGNORECASE)
            ).first
            blocks: list[str] = []
            try:
                if root.count():
                    blocks = root.locator("h1,h2,h3,h4,p,li").all_inner_texts()
            except Exception:
                blocks = []

            if not blocks:
                blocks = [line.strip() for line in article_text.splitlines() if line.strip()]

            pub_year = published_year(page_text)
            current_date_iso: str | None = None
            current_date_text = ""
            fairs: list[dict[str, Any]] = []
            seen: set[tuple[str | None, str, str]] = set()

            for raw in blocks:
                line = clean_text(raw)
                if not line:
                    continue

                found_date = extract_date(line, pub_year)
                if found_date and (
                    "ярмарк" in line.lower()
                    or re.search(r"\(\s*(понеділок|вівторок|серед|четвер|п['’]ятниц|субот|неділ)", line, re.I)
                ):
                    current_date_iso, current_date_text = found_date

                parsed = parse_fair_line(line)
                if parsed:
                    district, address = parsed
                    if any(x in line.lower() for x in ("кп «", "комунальн")):
                        continue
                    key = (current_date_iso, district, address)
                    if key in seen:
                        continue
                    seen.add(key)
                    fairs.append({
                        "date": current_date_iso,
                        "date_text": current_date_text or None,
                        "district": district,
                        "address": address,
                    })

            if not fairs:
                raise RuntimeError(
                    "Не вдалося знайти жодного запису ярмарку. "
                    "Можливо, змінилася структура сторінки або сайт повернув іншу відповідь."
                )

            scraped_at = datetime.now().astimezone().isoformat(timespec="seconds")
            return {
                "source_url": url,
                "title": title,
                "published_at": None,
                "scraped_at": scraped_at,
                "fairs": fairs,
            }
        finally:
            context.close()
            browser.close()


def write_outputs(data: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "fairs.json"
    txt_path = OUTPUT_DIR / "fairs.txt"

    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        data.get("title") or "Сезонні ярмарки Києва",
        f"Джерело: {data['source_url']}",
        f"Час збору: {data['scraped_at']}",
        "",
    ]
    current_date = object()
    for item in data["fairs"]:
        date_label = item.get("date_text") or item.get("date") or "Дата не визначена"
        if date_label != current_date:
            lines.append(date_label)
            current_date = date_label
        lines.append(f"  • {item['district']} — {item['address']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Збережено {len(data['fairs'])} записів: {json_path}, {txt_path}")


def main() -> int:
    try:
        data = fetch_page(SOURCE_URL)
        write_outputs(data)
        print(f"Успішно: {len(data['fairs'])} локацій ярмарків.")
        return 0
    except Exception as exc:
        print(f"ПОМИЛКА ЗБОРУ: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
