# Київські ярмарки — GIS API та Waze feed

Репозиторій щогодини отримує дані **лише з офіційного GIS API Києва**, формує Waze feed і створює читабельний HTML-звіт. Сайт Порталу Києва не використовується.

## Єдиний workflow

У **GitHub Actions** залишено один workflow:

**Ярмарки Києва — API, Waze та сповіщення**

Він запускається щогодини та після змін коду.

Що робить запуск:
1. Отримує записи шару «Заплановані» офіційного GIS API.
2. Зберігає отримані дані в `outpoot/fairs-api.json`.
3. Зіставляє майбутні записи API з геометрією у `data/waze-location-mapping.json`.
4. Формує `outpoot/feed_fair.json` лише з відповідних записів API, для яких знайдено геометрію. Час інцидентів — 07:00–17:00 за `Europe/Kyiv`.
5. Якщо API тимчасово не містить майбутніх дат або побудова feed не вдалася, залишає останній успішний feed без змін.
6. Формує HTML-звіт із записами API, статусом потрапляння у feed та посиланнями Waze.
7. Надсилає повідомлення в Telegram лише тоді, коли змінилися дані GIS API.

## Сторінка звіту

**[Відкрити читабельну сторінку звіту](https://raw.githack.com/OSBB-D2A/yarmarok-kyiv/main/outpoot/fairs-report.html)**

Це посилання відкриває HTML як вебсторінку, а не як вихідний код GitHub. У звіті є посилання для відкриття відповідних локацій у Waze.

## Файли результату

- `outpoot/feed_fair.json` — стрічка інцидентів для Waze.
- `outpoot/fairs-report.html` — читабельний HTML-звіт.
- `outpoot/fairs-api.json` — отримані записи GIS API та час перевірки.
- `outpoot/fairs-monitor-state.json` — контрольний відбиток останніх перевірених даних, щоб не надсилати однакове повідомлення щогодини.

## Налаштування Telegram

Токен бота **не можна записувати у файли репозиторію**. Додайте його як GitHub Actions secrets:

1. Відкрийте **Settings → Secrets and variables → Actions**.
2. Натисніть **New repository secret**.
3. Створіть секрет `TELEGRAM_BOT_TOKEN` зі значенням токена від BotFather.
4. Створіть секрет `TELEGRAM_CHAT_ID` зі своїм Telegram chat ID.
5. Збережіть секрети, відкрийте **Actions → Ярмарки Києва — API, Waze та сповіщення → Run workflow**, увімкніть параметр **Надіслати тестове повідомлення в Telegram навіть без змін даних** (`force_telegram`) і запустіть workflow.

Без цих секретів збір і HTML-звіт працюватимуть, але Telegram-повідомлення пропускатимуться. Не публікуйте токен у чатах, коді, комітах або логах.

## Джерела

- [GIS API — шар «Заплановані»](https://gisserver.kyivcity.gov.ua/mayno/rest/services/KYIV_API/Trade_api/MapServer/1)
- [Геометрія локацій для Waze](data/waze-location-mapping.json)

## Локальний запуск

Потрібен Python 3.11 або новіший.

```bash
python src/monitor_fairs.py
```

Для Telegram локально задайте `TELEGRAM_BOT_TOKEN` і `TELEGRAM_CHAT_ID` як змінні середовища.
