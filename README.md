# Київські ярмарки — моніторинг, порівняння та Waze feed

Репозиторій автоматично перевіряє офіційний портал Києва та GIS API, порівнює локації, оновлює Waze feed і створює HTML-звіт.

## Єдиний workflow

У **GitHub Actions** залишено один workflow:

**Ярмарки Києва — API, Waze та сповіщення**

Він запускається щогодини та після змін коду. Окремо запускати збір сайту, тест API або побудову mapping-файлу не потрібно.

Що робить запуск:
1. Зчитує список ярмарків з офіційної новини Порталу Києва.
2. Отримує записи шару «Заплановані» офіційного GIS API.
3. Порівнює записи сайту з API та геометрією у \`data/waze-location-mapping.json\`.
4. Формує \`outpoot/feed_fair.json\) лише з відповідних записів API, для яких знайдено геометрію. Час інцидентів — 07:00–17:00 за \`Europe/Kyiv\`.
5. Якщо API тимчасово не містить майбутніх дат або побудова feed не вдалася, залишає останній успішний feed без змін.
6. Формує HTML-звіт із записами сайту, записами API, статусом потрапляння у feed та посиланнями Waze для локацій, які потрапили у feed.
7. Надсилає повідомлення в Telegram лише тоді, коли змінився знімок даних сайту або API.

## Файли результату

- \`outpoot/feed_fair.json\` — стрічка інцидентів для Waze.
- \`outpoot/fairs-report.html\` — читабельний звіт: сайт → API → Waze feed.
- \`outpoot/fairs-website.json\` — структурований список із новини Києва.
- \`outpoot/fairs-api.json\` — отримані записи GIS API та час перевірки.
- \`outpoot/fairs-monitor-state.json\` — контрольний відбиток останніх перевірених даних, щоб не надсилати однакове повідомлення щогодини.

Звіт зберігається в репозиторії: [відкрити fairs-report.html](outpoot/fairs-report.html). Щоб відкрити його як повноцінну вебсторінку, завантажте файл і відкрийте браузером; GitHub може показувати HTML-файл як вихідний код, а не як сайт.

## Налаштування Telegram

Токен бота **не можна записувати у файли репозиторію**. Додайте його як GitHub Actions secrets:

1. Відкрийте **Settings → Secrets and variables → Actions**.
2. Натисніть **New repository secret**.
3. Створіть секрет \`TELEGRAM_BOT_TOKEN\` зі значенням токена від BotFather.
4. Створіть секрет \`TELEGRAM_CHAT_ID\` зі своїм Telegram chat ID.
5. Збережіть секрети та запустіть workflow вручну один раз для перевірки.

Без цих секретів збір і HTML-звіт працюватимуть, але Telegram-повідомлення пропускатимуться. Не публікуйте токен у чатах, коді, комітах або логах.

## Джерела

- [Новина Порталу Києва](https://kyivcity.gov.ua/news/tsogo_tizhnya_u_stolitsi_proydut_sezonni_yarmarki_perelik_lokatsiy_1053155/)
- [GIS API — шар «Заплановані»](https://gisserver.kyivcity.gov.ua/mayno/rest/services/KYIV_API/Trade_api/MapServer/1)
- [Геометрія локацій для Waze](data/waze-location-mapping.json)

## Локальний запуск

Потрібен Python 3.11 або новіший.

\`\`\`bash
python -m venv .venv
# Windows:
.venv\\Scripts\\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
python src/monitor_fairs.py
\`\`\`

Змінну \`FAIRS_URL\` можна встановити, щоб використати іншу офіційну сторінку новини. Для Telegram локально задайте \`TELEGRAM_BOT_TOKEN\` і \`TELEGRAM_CHAT_ID\` як змінні середовища.
