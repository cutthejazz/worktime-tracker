# Worktime Tracker — автоматичний облік робочого часу

Система автоматично фіксує активне робоче час на macOS, категоризує його та записує в Google Sheets — без жодної ручної дії.

**Розроблено:** Tetyana ([@cutthejazz](https://github.com/cutthejazz)) разом з Claude (Anthropic)  
**Використовується в:** Inweb, з жовтня 2026

---

## Як це виглядає

Таблиця Google Sheets з двома аркушами:

**Сводка** — зведення по днях:
| Date | Work | Background | Personal | Uncategorized |
|------|------|-----------|----------|--------------|
| 2026-10-31 | 6:12 | 0:45 | 0:10 | 0:20 |

Останній рядок — баланс за фінансовий період (відпрацьовано / норма / різниця).

**Детализация** — детальний лог: кожна програма і сайт з часом і посиланням на задачу.

---

## Як це працює

```
ActivityWatch (локально на Mac)
    ↓  збирає активні вікна та URL
worktime_report.py
    ↓  категоризує за rules.yaml
    ↓  рахує баланс відносно норми (4 год/день, без свят)
Google Sheets
    ↓  Сводка + Детализация оновлюються автоматично
network_watcher.py
    ↓  запускає звіт кожні 30 хвилин коли є мережа
LaunchAgent (macOS)
    ↓  тримає watcher живим у фоні
```

Зустрічі в Google Meet / Zoom рахуються повністю (навіть якщо вікно згорнуте). Перерви до 30 хвилин всередині дзвінка склеюються. Двойного рахунку немає.

Категорія `uncategorized` — це сигнал: якщо там з'являється багато часу, значить з'явився новий інструмент який ще не внесений в правила.

---

## Встановлення

### 1. ActivityWatch

Завантажити з [activitywatch.net](https://activitywatch.net) та встановити.  
Також встановити розширення **ActivityWatch Web Watcher** для Chrome — без нього URL вкладок не визначаються.

ActivityWatch запускається автоматично і зберігає дані локально. Нікуди не відправляє.

### 2. Python залежності

```bash
pip3 install requests pyyaml gspread google-auth-oauthlib
```

### 3. Google Sheets OAuth

1. Відкрити [Google Cloud Console](https://console.cloud.google.com)
2. Створити проєкт → увімкнути Google Sheets API та Google Drive API
3. Створити OAuth 2.0 credentials (тип: Desktop app)
4. Завантажити JSON → зберегти як `~/worktime/oauth_creds.json`
5. При першому запуску скрипт відкриє браузер для авторизації

### 4. Налаштувати таблицю

1. Створити нову Google Sheets таблицю
2. Відкрити `worktime_report.py`, знайти рядок `SHEET_NAME` і вказати назву своєї таблиці

### 5. Скопіювати файли

```bash
mkdir -p ~/worktime
cp worktime_report.py network_watcher.py rules.yaml ~/worktime/
```

### 6. Перевірити що все працює

```bash
cd ~/worktime
python3 worktime_report.py --days 1 --include-today --dry-run
```

Якщо виводить статистику в термінал — все підключено правильно.

### 7. Записати в таблицю

```bash
python3 worktime_report.py --days 1 --include-today
```

### 8. Автозапуск (опційно)

Щоб скрипт запускався автоматично кожні 30 хвилин:

```bash
# Відредагувати plist — замінити YOUR_USERNAME на своє ім'я
sed -i '' 's|YOUR_USERNAME|'"$(whoami)"'|g' net.worktime.watcher.plist

# Скопіювати в LaunchAgents
cp net.worktime.watcher.plist ~/Library/LaunchAgents/

# Запустити
launchctl load ~/Library/LaunchAgents/net.worktime.watcher.plist
```

---

## Команди

```bash
# Перевірити за сьогодні (без запису в таблицю)
python3 worktime_report.py --days 1 --include-today --dry-run

# Записати останні 7 днів
python3 worktime_report.py --days 7

# Конкретна дата
python3 worktime_report.py --date 2026-10-15

# Логи автозапуску
tail -f ~/Library/Logs/ActivityWatch/worktime-watcher.log
```

---

## Налаштування категорій (rules.yaml)

Правила перевіряються зверху вниз, спрацьовує перше збіжіння.

```yaml
# Додати новий робочий сайт:
- match: url
  pattern: 'example\.com'
  category: work
  label: 'Назва сервісу'

# Додати нову програму:
- match: app
  pattern: 'Назва програми'
  category: work
  label: 'Мітка у звіті'
```

Категорії: `work` / `neutral` (фон) / `personal` / `uncategorized`

---

## Обмеження

- Тільки macOS
- ActivityWatch повинен бути запущений — час без нього не фіксується
- Якщо вікно браузера згорнуте під час дзвінка — може не зафіксуватись (AFK-таймаут 180 сек)
- Нові сервіси потрібно додавати в `rules.yaml` вручну
- OAuth потрібно налаштувати для кожного нового користувача окремо

---

## Структура проєкту

```
worktime/
├── worktime_report.py          # Основний скрипт
├── network_watcher.py          # Демон автозапуску
├── rules.yaml                  # Правила категоризації
├── net.worktime.watcher.plist  # LaunchAgent для watcher
├── net.worktime.report.plist   # LaunchAgent для звітів за розкладом
├── oauth_creds.json            # ⚠️ не комітити — Google OAuth ключ
└── oauth_token.json            # ⚠️ не комітити — токен авторизації
```
