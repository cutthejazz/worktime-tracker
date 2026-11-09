#!/usr/bin/env python3
"""
Отчёт по рабочему времени на основе локальных данных ActivityWatch.

Использование:
    python3 worktime_report.py                 # за вчера
    python3 worktime_report.py --date 2026-08-18
    python3 worktime_report.py --days 7        # последние 7 дней (без сегодня)
    python3 worktime_report.py --days 2 --include-today   # сегодня и вчера
    python3 worktime_report.py --dry-run       # только в консоль, без Google Sheets

Требования:
    pip3 install requests pyyaml gspread google-auth-oauthlib
    ActivityWatch должен быть запущен (localhost:5600)
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

import requests
import yaml

AW_HOST = "http://localhost:5600"
SCRIPT_DIR = Path(__file__).resolve().parent
RULES_PATH = SCRIPT_DIR / "rules.yaml"
OAUTH_CLIENT_PATH = SCRIPT_DIR / "oauth_creds.json"
TOKEN_PATH = SCRIPT_DIR / "oauth_token.json"
SHEET_NAME = "cut the jazz_ Inweb_operation manager"


# ---------------------------------------------------------------- ActivityWatch

def aw_get(path, **params):
    r = requests.get(f"{AW_HOST}/api/0{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def find_buckets():
    """Находит нужные бакеты: окна, AFK, браузер."""
    try:
        buckets = aw_get("/buckets/")
    except requests.exceptions.ConnectionError:
        sys.exit("ActivityWatch не отвечает на localhost:5600. Запущено ли приложение?")

    found = {"window": None, "afk": None, "web": []}
    for bid, meta in buckets.items():
        t = meta.get("type", "")
        if t == "currentwindow":
            found["window"] = bid
        elif t == "afkstatus":
            found["afk"] = bid
        elif t == "web.tab.current":
            found["web"].append(bid)

    if not found["window"]:
        sys.exit("Не найден бакет окон. Проверь разрешения Accessibility для ActivityWatch.")
    if not found["afk"]:
        sys.exit("Не найден AFK-бакет — без него нельзя отличить работу от простоя.")
    if not found["web"]:
        print("! Расширение для браузера не найдено — вкладки Chrome не будут детализированы.",
              file=sys.stderr)
    return found


def _local_tz():
    """Читаем смещение таймзоны из вывода системной команды date.
    Это работает даже когда Python запущен через launchd без переменной TZ,
    потому что macOS date всегда возвращает правильное местное время."""
    import subprocess, re
    try:
        out = subprocess.check_output(["date", "+%z"], text=True).strip()
        # формат: +0300 или -0500
        m = re.match(r"([+-])(\d{2})(\d{2})", out)
        if m:
            sign = 1 if m.group(1) == "+" else -1
            hours, mins = int(m.group(2)), int(m.group(3))
            offset = sign * (hours * 3600 + mins * 60)
            return timezone(timedelta(seconds=offset))
    except Exception:
        pass
    # fallback: UTC
    return timezone.utc


def fetch_events(bucket_id, day):
    # Используем локальное время для границ дня чтобы работа после полуночи
    # по местному времени попадала в правильный день.
    tz = _local_tz()
    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    return aw_get(f"/buckets/{bucket_id}/events",
                  start=start.isoformat(), end=end.isoformat(), limit=-1)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def intervals_from(events, predicate=None):
    """Превращает события в список (start, end)."""
    out = []
    for e in events:
        if predicate and not predicate(e):
            continue
        st = parse_ts(e["timestamp"])
        out.append((st, st + timedelta(seconds=e["duration"])))
    return out


def overlap(a, b):
    """Пересечение двух списков интервалов."""
    res, i, j = [], 0, 0
    a = sorted(a)
    b = sorted(b)
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            res.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return res


def union(intervals):
    """Схлопывает пересекающиеся интервалы в непересекающиеся."""
    if not intervals:
        return []
    out = []
    for st, en in sorted(intervals):
        if out and st <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], en))
        else:
            out.append((st, en))
    return out


def merge_with_gap(intervals, gap):
    """Как union, но склеивает и разрывы короче gap."""
    if not intervals:
        return []
    out = []
    for st, en in sorted(intervals):
        if out and st - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], en))
        else:
            out.append((st, en))
    return out


def total_seconds(intervals):
    return sum((e - s).total_seconds() for s, e in intervals)


# ---------------------------------------------------------------- Категоризация

class Classifier:
    def __init__(self, rules_path):
        cfg = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        self.rules = cfg["rules"]
        self.default = cfg.get("default_category", "uncategorized")
        self.browsers = [b.lower() for b in cfg["settings"]["browsers"]]
        self.min_sec = cfg["settings"].get("min_event_seconds", 5)
        mt = cfg.get("meetings", {}) or {}
        self.meet_urls = mt.get("url_patterns", [])
        self.meet_apps = [a.lower() for a in mt.get("apps", [])]
        self.meet_gap = timedelta(minutes=mt.get("gap_minutes", 15))
        self.meet_label = mt.get("label", "Созвоны")

    def is_meeting_url(self, url):
        return any(re.search(p, url or "", re.I) for p in self.meet_urls)

    def is_meeting_app(self, app):
        return any(a in (app or "").lower() for a in self.meet_apps)

    def is_browser(self, app):
        return any(b in (app or "").lower() for b in self.browsers)

    def classify(self, app, title, url):
        for rule in self.rules:
            field = {"app": app, "title": title, "url": url}.get(rule["match"], "")
            if not field:
                continue
            if rule["match"] == "app":
                hit = rule["pattern"].lower() in field.lower()
            else:
                hit = re.search(rule["pattern"], field, re.I) is not None
            if not hit:
                continue

            task = None
            tft = rule.get("task_from_title")
            if tft and title:
                m = re.search(tft, title, re.I)
                if m:
                    task = m.group("task").strip()
            if task is None and title:
                task = title.strip()[:120]
            return rule["category"], rule["label"], task

        label = app or "Неизвестно"
        return self.default, label, (title or "").strip()[:120]


# ---------------------------------------------------------------- Сбор дня

def build_day(day, buckets, clf):
    win = fetch_events(buckets["window"], day)
    afk = fetch_events(buckets["afk"], day)
    active = intervals_from(afk, lambda e: e["data"].get("status") == "not-afk")

    web_events = []
    for wb in buckets["web"]:
        web_events += fetch_events(wb, day)
    # Расширение часто пишет события нулевой длительности (heartbeat-точки),
    # поэтому ищем не "какая вкладка активна в этот миг", а "какая была
    # заявлена последней к этому моменту" — точка действует до следующей.
    # ВАЖНО: сортируем строго по времени. Без key= Python при совпадении
    # времени и длительности дойдёт до сравнения словарей и упадёт.
    web_pts = sorted(((parse_ts(e["timestamp"]),
                       max(e["duration"], 0.0),
                       e["data"]) for e in web_events),
                     key=lambda x: (x[0], x[1]))

    # Насколько долго после последней отметки ещё считаем вкладку актуальной.
    STALE_AFTER = timedelta(minutes=5)

    def url_at(t):
        best = None
        for ts, dur, d in web_pts:
            if ts > t + timedelta(seconds=2):
                break
            best = (ts, dur, d)
        if not best:
            return "", ""
        ts, dur, d = best
        end = ts + timedelta(seconds=dur)
        if t > max(end, ts + STALE_AFTER):
            return "", ""
        return d.get("url", ""), d.get("title", "")

    # --- Встречи считаются целиком, даже если окно неактивно и мышь не трогается.
    # Событие вкладки Meet — точка heartbeat; склеиваем их в сессию, допуская
    # разрывы до meet_gap (переключение на другую задачу во время звонка).
    meet_raw = []
    for e in web_events:
        if clf.is_meeting_url(e["data"].get("url", "")):
            st = parse_ts(e["timestamp"])
            meet_raw.append((st, st + timedelta(seconds=max(e["duration"], 0))))
    for e in win:
        if clf.is_meeting_app(e["data"].get("app", "")):
            st = parse_ts(e["timestamp"])
            meet_raw.append((st, st + timedelta(seconds=max(e["duration"], 0))))

    meet_iv = merge_with_gap(meet_raw, clf.meet_gap)
    day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=_local_tz())
    day_end = day_start + timedelta(days=1)
    meet_iv = [(max(s0, day_start), min(e0, day_end)) for s0, e0 in meet_iv]
    meet_iv = [(s0, e0) for s0, e0 in meet_iv if e0 > s0]

    by_label = defaultdict(float)
    by_cat = defaultdict(float)
    tasks = defaultdict(float)  # label|task -> secs
    task_urls = {}              # label|task -> canonical url
    work_iv = []

    for ev in win:
        if ev["duration"] < clf.min_sec:
            continue
        st = parse_ts(ev["timestamp"])
        iv = [(st, st + timedelta(seconds=ev["duration"]))]
        secs = total_seconds(overlap(iv, active))
        if secs < clf.min_sec:
            continue

        app = ev["data"].get("app", "")
        title = ev["data"].get("title", "")
        url = ""
        if clf.is_browser(app):
            url, wtitle = url_at(st)
            if wtitle:
                title = wtitle

        cat, label, task = clf.classify(app, title, url)

        # время внутри звонка учитывается отдельным интервалом ниже
        if clf.is_meeting_url(url) or clf.is_meeting_app(app):
            continue

        by_label[(cat, label)] += secs
        by_cat[cat] += secs
        if cat == "work":
            work_iv += overlap(iv, active)
        if cat == "work" and task:
            # для Планфикса переопределяем название и берём канонический URL
            if "planfix" in (url or "").lower():
                pf_name = _pf_task_name(title, url)
                pf_url = _pf_task_url(url)
                if pf_name:
                    key = (label, pf_name)
                    tasks[key] += secs
                    if pf_url and key not in task_urls:
                        task_urls[key] = pf_url
                # не добавляем если имя не распознано (хроника, фильтры)
            else:
                key = (label, task)
                tasks[key] += secs
                if url and key not in task_urls:
                    task_urls[key] = url

    meet_secs = total_seconds(meet_iv)
    if meet_secs >= clf.min_sec:
        by_label[("work", clf.meet_label)] = meet_secs

    # объединение убирает двойной счёт: работа во время звонка не удваивается
    by_cat["work"] = total_seconds(union(work_iv + meet_iv))

    return {
        "date": day.isoformat(),
        "meeting_seconds": meet_secs,
        "task_urls": {f"{l}|{t}": u for (l, t), u in task_urls.items()},
        "by_category": dict(by_cat),
        "by_label": {f"{c}|{l}": v for (c, l), v in by_label.items()},
        "tasks": {f"{l}|{t}": v for (l, t), v in tasks.items()},
    }


def hhmm(seconds):
    m = int(round(seconds / 60))
    return f"{m // 60}:{m % 60:02d}"


def print_day(d):
    print(f"\n=== {d['date']} ===")
    for cat in ("work", "neutral", "personal", "uncategorized"):
        if d["by_category"].get(cat):
            print(f"  {cat:14} {hhmm(d['by_category'][cat])}")
    print("\n  По программам и сервисам:")
    rows = sorted(d["by_label"].items(), key=lambda kv: -kv[1])
    for key, secs in rows:
        cat, label = key.split("|", 1)
        if secs < 60:
            continue
        print(f"    [{cat[:4]}] {label:32} {hhmm(secs)}")
    if d["tasks"]:
        print("\n  Задачи:")
        for key, secs in sorted(d["tasks"].items(), key=lambda kv: -kv[1])[:25]:
            label, task = key.split("|", 1)
            if secs < 120:
                continue
            print(f"    {label:16} {task[:60]:62} {hhmm(secs)}")


# ---------------------------------------------------------------- Google Sheets

def _wait_for_network(timeout=60, interval=5):
    """Ждём, пока DNS не заработает — актуально при запуске через launchd
    сразу после пробуждения мака, когда сеть ещё не поднялась."""
    import socket, time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.getaddrinfo("oauth2.googleapis.com", 443)
            return True
        except socket.gaierror:
            time.sleep(interval)
    return False


def open_sheet():
    import gspread

    if not OAUTH_CLIENT_PATH.exists():
        sys.exit(
            f"Нет файла {OAUTH_CLIENT_PATH.name}.\n"
            "Скачай OAuth client ID (тип Desktop app) из Google Cloud Console,\n"
            f"переименуй в {OAUTH_CLIENT_PATH.name} и положи рядом со скриптом."
        )

    # Первый запуск откроет браузер для подтверждения доступа.
    # Дальше используется сохранённый токен из oauth_token.json.
    gc = gspread.oauth(
        credentials_filename=str(OAUTH_CLIENT_PATH),
        authorized_user_filename=str(TOKEN_PATH),
    )

    if not _wait_for_network():
        sys.exit("Нет сети — не удалось подключиться к Google за 60 секунд.")

    try:
        return gc.open(SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sys.exit(
            f"Таблица «{SHEET_NAME}» не найдена в твоём Google Drive.\n"
            "Проверь название посимвольно — оно должно совпадать точно."
        )


def ensure_ws(sh, title, header, rows, cols, banner=""):
    """Лист по образцу фирменного шаблона: строка 1 — титульная плашка,
    строка 2 — шапка колонок, данные с третьей строки."""
    import gspread
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title, rows=rows, cols=cols)

    if ws.row_values(1) != [banner]:
        ws.update(values=[[banner]], range_name="A1")
    if ws.row_values(2) != header:
        end = chr(64 + len(header))
        ws.update(values=[header], range_name=f"A2:{end}2")
    return ws


def push_to_sheets(days, _attempt=1):
    try:
        sh = open_sheet()
    except Exception as e:
        if _attempt < 3 and any(x in str(e).lower() for x in
                                ("transporterror","resolve","connectionerror","httpsconnection")):
            import time
            print(f"Сетевая ошибка, повтор через 15 сек (попытка {_attempt}/3)…",
                  file=sys.stderr)
            time.sleep(15)
            return push_to_sheets(days, _attempt + 1)
        raise

    # --- Лист «Сводка»: одна строка на день, перезаписывается при повторе
    summary_header = ["Date", "Work", "Background", "Personal", "Uncategorized"]
    ws = ensure_ws(sh, "Сводка", summary_header, 400, 6,
                   banner="Облік робочого часу — Зведення")

    # Читаем ВСЕ существующие строки с датами — из любого места таблицы.
    # kept + new → история не теряется никогда.
    update_dates = {d["date"] for d in days}
    # Читаємо всі рядки таблиці (не тільки від рядка 3, а повністю)
    all_existing = ws.get_all_values()
    # Збираємо ВСІ рядки з датами у форматі YYYY-MM-DD з будь-якого місця
    import re as _re
    existing_date_rows = {}
    for r in all_existing:
        if r and r[0] and _re.match(r'\d{4}-\d{2}-\d{2}', str(r[0]).strip()):
            existing_date_rows[r[0].strip()] = r[:5]

    new_rows = []
    for d in days:
        new_rows.append([d["date"],
                         hhmm(d["by_category"].get("work", 0)),
                         hhmm(d["by_category"].get("neutral", 0)),
                         hhmm(d["by_category"].get("personal", 0)),
                         hhmm(d["by_category"].get("uncategorized", 0))])

    # Об'єднуємо: всі існуючі дати + нові (нові перекривають старі для тих самих дат)
    merged = dict(existing_date_rows)
    for r in new_rows:
        merged[r[0]] = r
    ordered = sorted(merged.values(), key=lambda r: r[0])

    existing_summary_count = len(existing_date_rows)
    summary_safe = (existing_summary_count == 0) or (len(ordered) >= existing_summary_count)
    if not summary_safe:
        print(f"! Сводка: було {existing_summary_count} рядків, стало {len(ordered)}, "
              f"пропускаємо запис щоб не затерти історію", file=sys.stderr)
    else:
        ws.batch_clear(["A3:E400"])
        if ordered:
            ws.update(values=ordered, range_name=f"A3:E{len(ordered) + 2}")

    # --- Лист «Детализация»: строки за перезаписываемые дни удаляются целиком
    detail_header = ["Date", "Category", "App / Service", "Task", "Time", "Link"]
    ws2 = ensure_ws(sh, "Детализация", detail_header, 5000, 6,
                    banner="Облік робочого часу — Деталізація")

    dates = {d["date"] for d in days}
    all_rows = ws2.get_all_values()
    kept = [r for r in all_rows[2:] if r and r[0] not in dates]

    batch = []
    for d in days:
        # Сначала определяем для каких сервисов есть распознанные задачи
        tasks_by_label = set()
        task_rows = []
        for key, secs in sorted(d["tasks"].items(), key=lambda kv: -kv[1]):
            if secs < 120:
                continue
            label, task = key.split("|", 1)
            tasks_by_label.add(label)
            task_url = d.get("task_urls", {}).get(f"{label}|{task}", "")
            task_rows.append([d["date"], "work", label, task, hhmm(secs), task_url])

        # Заглушки только для сервисов без задач
        for key, secs in sorted(d["by_label"].items(), key=lambda kv: -kv[1]):
            if secs < 60:
                continue
            cat, label = key.split("|", 1)
            if label in tasks_by_label:
                continue
            batch.append([d["date"], cat, label, "", hhmm(secs), ""])

        # Задачи идут после заглушек (будут выше после сортировки по дате desc)
        batch.extend(task_rows)

    # стабильная сортировка: дни по убыванию, порядок внутри дня сохраняется
    existing_detail_count = len([r for r in all_rows[2:] if r and r[0]])
    final = sorted(kept + batch, key=lambda r: r[0], reverse=True)
    detail_safe = (existing_detail_count == 0) or (len(final) >= existing_detail_count)
    if not detail_safe:
        print(f"! Детализация: було {existing_detail_count} строк, стало {len(final)}, "
              f"пропускаем запись чтобы не затереть историю", file=sys.stderr)
    else:
        ws2.batch_clear(["A3:F5000"])
        if final:
            ws2.update(values=final, range_name=f"A3:F{len(final) + 2}", value_input_option="USER_ENTERED")

    apply_brand(sh, ws, ws2)

    # Баланс — передай carry_hours если есть перенос с прошлого периода
    # Например: push_balance(sh, ws, days, carry_hours=-3.5)
    push_balance(sh, ws, days, carry_hours=0.0)

    # Группировка строк: всё до текущего финансового периода — свёрнуто
    _update_row_groups(sh, ws)

    print(f"\nЗаписано в Google Sheets: {sh.url}")



# ---------------------------------------------------------------- Праздники

# Официальные праздники Украины по ст.73 КЗпП.
# В условиях военного положения (закон №2136-IX) переносы отменены —
# если праздник попал на выходной, дополнительного выходного нет.
# Список обновляется вручную при изменении законодательства.
def _build_holidays():
    """Праздники по КЗпП + корпоративные переносы Inweb:
    праздник в субботу → пятница выходная,
    праздник в воскресенье → понедельник выходной."""
    from datetime import date, timedelta
    base = {
        # 2026
        "2026-01-01", "2026-03-08", "2026-04-12", "2026-05-01",
        "2026-05-08", "2026-05-31", "2026-06-28", "2026-07-15",
        "2026-08-24", "2026-10-01", "2026-12-25",
        # 2027 (Великдень 2 травня, Трійця 20 червня)
        "2027-01-01", "2027-03-08", "2027-05-02", "2027-05-01",
        "2027-05-08", "2027-06-20", "2027-06-28", "2027-07-15",
        "2027-08-24", "2027-10-01", "2027-12-25",
    }
    extra = set()
    for ds in base:
        d = date.fromisoformat(ds)
        if d.weekday() == 5:    # суббота → пятница
            extra.add((d - timedelta(days=1)).isoformat())
        elif d.weekday() == 6:  # воскресенье → понедельник
            extra.add((d + timedelta(days=1)).isoformat())
    return base | extra


UA_HOLIDAYS = _build_holidays()

# ---------------------------------------------------------------- Планфикс

_PF_SKIP = {"inweb","внутрішні задачі","операційна","топ-менеджмент",
            "непрочитані","хроніка","задачі за останні","перевірка чатів",
            "ризик від","ризик в","завершення проєкту"}


def _pf_task_url(url):
    """Канонический URL задачи Планфикса (без comment и прочих параметров)."""
    m = re.search(r'/task/(\d+)', url or "")
    if m: return f"https://inweb.planfix.com/task/{m.group(1)}"
    m = re.search(r'[?&]task=(\d+)', url or "")
    if m: return f"https://inweb.planfix.com/task/{m.group(1)}"
    return None


def _pf_task_name(title, url):
    """Название задачи из заголовка вкладки Планфикса."""
    if not _pf_task_url(url):
        return None
    parts = [p.strip() for p in re.split(r'\s*/\s*', title or "")]
    good = []
    for p in parts:
        pl = p.lower()
        if any(w in pl for w in _PF_SKIP): continue
        if len(p) < 3: continue
        if re.match(r'^\d{4}-\d{2}-\d{2}', p): continue
        good.append(p)
    if not good:
        return (parts[0] if parts else title or "")[:120]
    if re.match(r'^[\w-]+\.(ua|com|net|org|io|eu|biz)$', good[0], re.I) and len(good) > 1:
        return f"{good[1]} ({good[0]})"[:120]
    return good[0][:120]



# ---------------------------------------------------------------- Баланс

DAILY_NORM_H = 4.0   # норма часов в рабочий день


def _safe_date(s):
    try:
        return date.fromisoformat(s)
    except Exception:
        return date(2000, 1, 1)


def period_bounds(today):
    """Зарплатный период: с 5-го по 4-е следующего месяца."""
    if today.day >= 5:
        start = date(today.year, today.month, 5)
    else:
        m = today.month - 1 or 12
        y = today.year if today.month > 1 else today.year - 1
        start = date(y, m, 5)
    m2 = start.month % 12 + 1
    y2 = start.year + (1 if start.month == 12 else 0)
    end = date(y2, m2, 4)
    return start, end


def workdays_in(start, end_inclusive):
    """Количество рабочих дней в диапазоне (пн-пт, не праздник)."""
    count = 0
    d = start
    while d <= end_inclusive:
        if d.weekday() < 5 and d.isoformat() not in UA_HOLIDAYS:
            count += 1
        d += timedelta(days=1)
    return count


def push_balance(sh, ws_summary, days_data, carry_hours=0.0):
    """Добавляет/обновляет строку баланса в листе Сводка.

    carry_hours — перенесённая недоработка с прошлого периода (отрицательное число).
    Переработка обнуляется, недоработка переносится.
    """
    today = date.today()
    p_start, p_end = period_bounds(today)

    # Норма: рабочие дни от начала периода до сегодня
    norm_days = workdays_in(p_start, min(today, p_end))
    norm_secs = norm_days * DAILY_NORM_H * 3600

    # Отработано: читаем из листа Сводка (там все дни периода),
    # добавляем данные текущего запуска (они могут быть свежее таблицы)
    worked_secs = 0.0

    # Сначала берём из таблицы
    def parse_hhmm(s):
        """Преобразует строку H:MM в секунды."""
        try:
            parts = s.strip().split(":")
            return int(parts[0]) * 3600 + int(parts[1]) * 60
        except Exception:
            return 0

    all_rows = ws_summary.get_all_values()
    for row in all_rows[2:]:  # пропускаем плашку и шапку
        if not row or not row[0] or row[0].startswith("Баланс"):
            continue
        try:
            r_date = date.fromisoformat(row[0])
        except Exception:
            continue
        if p_start <= r_date <= min(today, p_end):
            worked_secs += parse_hhmm(row[1]) if len(row) > 1 else 0

    # Обновляем данными текущего запуска (перезаписываем если дата совпадает)
    days_in_table = {date.fromisoformat(row[0]) for row in all_rows[2:]
                     if row and row[0] and not row[0].startswith("Баланс")
                     and p_start <= _safe_date(row[0]) <= min(today, p_end)}
    for d in days_data:
        try:
            d_date = date.fromisoformat(d["date"])
        except Exception:
            continue
        if p_start <= d_date <= min(today, p_end):
            # вычитаем старое значение из таблицы если оно было
            for row in all_rows[2:]:
                if row and row[0] == d["date"] and not row[0].startswith("Баланс"):
                    worked_secs -= parse_hhmm(row[1]) if len(row) > 1 else 0
                    break
            worked_secs += d["by_category"].get("work", 0)

    carry_secs = carry_hours * 3600
    balance_secs = worked_secs - norm_secs + carry_secs

    def fmt(secs):
        total_min = int(abs(secs) / 60)
        sign = "+" if secs >= 0 else "-"
        return f"{sign}{total_min // 60}:{total_min % 60:02d}"

    banner = f"Баланс за {p_start.strftime('%d.%m')}–{p_end.strftime('%d.%m.%Y')}"
    worked_str = hhmm(worked_secs)
    norm_str = hhmm(norm_secs)
    balance_str = fmt(balance_secs)

    # Ищем строку баланса (начинается с "Баланс") в листе
    all_rows = ws_summary.get_all_values()
    balance_row_idx = None
    for i, row in enumerate(all_rows):
        if row and str(row[0]).startswith("Баланс"):
            balance_row_idx = i + 1  # 1-based
            break

    # Цвет: зелёный если норма выполнена, красный если нет
    if balance_secs >= 0:
        bg = {"red": 0.800, "green": 0.878, "blue": 0.831}  # лёгкий зелёный
    else:
        bg = {"red": 1.0, "green": 0.878, "blue": 0.878}    # лёгкий красный

    carry_str = f"перенос {carry_hours:+.1f}ч" if carry_hours else ""
    # Колонки: A=период, B=отработано, C=норма, D=баланс, E=перенос
    # Добавляем метки прямо в значения чтобы было читаемо
    row_data = [
        banner,
        f"отроб: {worked_str}",
        f"норма: {norm_str}",
        f"баланс: {balance_str}",
        carry_str,
    ]

    if balance_row_idx:
        ws_summary.update(
            values=[row_data],
            range_name=f"A{balance_row_idx}:E{balance_row_idx}"
        )
        row_n = balance_row_idx
    else:
        ws_summary.append_row(row_data)
        row_n = len(ws_summary.get_all_values())

    # Подсветка строки баланса
    try:
        sh.batch_update({"requests": [{"repeatCell": {
            "range": {"sheetId": ws_summary.id,
                      "startRowIndex": row_n - 1, "endRowIndex": row_n,
                      "startColumnIndex": 0, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg,
                "textFormat": {"bold": True, "fontSize": 10}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}}]})
    except Exception:
        pass

    print(f"Баланс: норма {norm_str} | отработано {worked_str} | итог {balance_str}")

# ---------------------------------------------------------------- Оформление

# Палитра Inweb 2026 (02_BRAND_TOKENS.json) + оттенки разлиновки, снятые
# из фирменного шаблона 50_SPREADSHEETS/Шаблон таблиць Inweb _ 2026.xlsx
BRAND = {
    "green":    {"red": 0.000, "green": 0.231, "blue": 0.161},  # #003B29
    "orange":   {"red": 0.937, "green": 0.345, "blue": 0.239},  # #EF583D
    "lemon":    {"red": 0.941, "green": 0.988, "blue": 0.494},  # #F0FC7E
    "ink":      {"red": 0.110, "green": 0.110, "blue": 0.110},  # #1C1C1C
    "offWhite": {"red": 0.969, "green": 0.969, "blue": 0.969},  # #F7F7F7
    "white":    {"red": 1.0, "green": 1.0, "blue": 1.0},
    "gridSoft": {"red": 0.953, "green": 0.953, "blue": 0.953},  # #F3F3F3
    "gridRow":  {"red": 0.718, "green": 0.718, "blue": 0.718},  # #B7B7B7
    "black":    {"red": 0.0, "green": 0.0, "blue": 0.0},
}
BRAND_FONT = "Geist"   # если шрифта нет в аккаунте, Sheets подставит свой


def _txt(color, bold=False, size=10):
    return {"foregroundColor": color, "bold": bold,
            "fontFamily": BRAND_FONT, "fontSize": size}


def _border(color, style="SOLID"):
    return {"style": style, "color": color}


def _update_row_groups(sh, ws_summary):
    """Групує рядки до поточного фінансового місяця і згортає їх.
    Якщо таких рядків немає — видаляє всі існуючі групи (щоб не залишались зайві)."""
    from datetime import date
    import re as _re
    today = date.today()
    p_start, _ = period_bounds(today)
    all_rows = ws_summary.get_all_values()
    data_rows = all_rows[2:]
    last_old_idx = -1
    for i, row in enumerate(data_rows):
        if not row or not row[0]:
            continue
        if not _re.match(r'\d{4}-\d{2}-\d{2}', row[0]):
            continue
        try:
            row_date = date.fromisoformat(row[0])
        except ValueError:
            continue
        if row_date < p_start:
            last_old_idx = i

    sheet_id = ws_summary.id
    requests = []

    # Завжди видаляємо всі існуючі групи на цьому аркуші
    try:
        meta = sh.fetch_sheet_metadata()
        for s in meta.get("sheets", []):
            if s["properties"]["sheetId"] == sheet_id:
                for grp in s.get("rowGroups", []):
                    r = grp["range"]
                    requests.append({"deleteDimensionGroup": {
                        "range": {"sheetId": sheet_id, "dimension": "ROWS",
                                  "startIndex": r["startIndex"], "endIndex": r["endIndex"]}
                    }})
    except Exception:
        pass

    # Додаємо нову групу тільки якщо є рядки до поточного місяця
    if last_old_idx >= 0:
        group_start = 2
        group_end = group_start + last_old_idx + 1
        requests.append({"addDimensionGroup": {"range": {
            "sheetId": sheet_id, "dimension": "ROWS",
            "startIndex": group_start, "endIndex": group_end,
        }}})
        requests.append({"updateDimensionGroup": {
            "dimensionGroup": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": group_start, "endIndex": group_end},
                "depth": 1, "collapsed": True,
            },
            "fields": "collapsed",
        }})

    if requests:
        sh.batch_update({"requests": requests})


def apply_brand(sh, ws_summary, ws_detail):
    """Оформление по фирменному шаблону Inweb 2026.

    Строка 1 — зелёная плашка с названием, строка 2 — шапка без заливки
    с чёрной линией снизу, данные с третьей строки на белом фоне
    с тонкой разлиновкой. Применяется при каждом запуске.
    """
    reqs = []

    for ws, cols in ((ws_summary, 5), (ws_detail, 6)):
        sid = ws.id

        # строка 1 — титульная плашка #003B29, объединена по ширине таблицы
        reqs.append({"mergeCells": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": cols},
            "mergeType": "MERGE_ALL"}})
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": cols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": BRAND["green"],
                "textFormat": _txt(BRAND["white"], bold=True, size=14),
                "verticalAlignment": "MIDDLE",
                "padding": {"left": 10, "right": 10}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,"
                      "verticalAlignment,padding)"}})

        # строка 2 — шапка: без заливки, Geist 12 bold, чёрная линия снизу
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2,
                      "startColumnIndex": 0, "endColumnIndex": cols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": BRAND["white"],
                "textFormat": _txt(BRAND["ink"], bold=True, size=12),
                "verticalAlignment": "MIDDLE",
                "padding": {"left": 10, "right": 10}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,"
                      "verticalAlignment,padding)"}})
        reqs.append({"updateBorders": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2,
                      "startColumnIndex": 0, "endColumnIndex": cols},
            "bottom": _border(BRAND["black"]),
            "innerVertical": _border(BRAND["gridSoft"])}})

        # данные — Geist 10 на белом
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 2,
                      "startColumnIndex": 0, "endColumnIndex": cols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": BRAND["white"],
                "textFormat": _txt(BRAND["ink"], size=10),
                "verticalAlignment": "MIDDLE",
                "padding": {"left": 10, "right": 10}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,"
                      "verticalAlignment,padding)"}})

        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 2},
                           "tabColor": BRAND["green"]},
            "fields": "gridProperties.frozenRowCount,tabColor"}})

        # высоты строк как в шаблоне
        for idx, h in ((0, 40), (1, 39)):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": idx, "endIndex": idx + 1},
                "properties": {"pixelSize": h}, "fields": "pixelSize"}})
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 2,
                      "endIndex": 500},
            "properties": {"pixelSize": 30}, "fields": "pixelSize"}})

    # разлиновка данных: вертикали #F3F3F3, горизонтали #B7B7B7
    for ws, cols, last in ((ws_summary, 5, 400), (ws_detail, 6, 5000)):
        reqs.append({"updateBorders": {
            "range": {"sheetId": ws.id, "startRowIndex": 2, "endRowIndex": last,
                      "startColumnIndex": 0, "endColumnIndex": cols},
            "innerVertical": _border(BRAND["gridSoft"]),
            "innerHorizontal": _border(BRAND["gridRow"])}})

    # ширина колонок
    for sid, widths in ((ws_summary.id, [120, 110, 120, 110, 130]),
                        (ws_detail.id, [110, 110, 190, 300, 70, 260])):
        for i, wpx in enumerate(widths):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": wpx}, "fields": "pixelSize"}})

    # один акцент на категорию — не декор, а быстрая читаемость
    for value, bg in (("work", BRAND["lemon"]), ("personal", BRAND["orange"])):
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [{"sheetId": ws_detail.id, "startRowIndex": 2,
                        "startColumnIndex": 1, "endColumnIndex": 2}],
            "booleanRule": {
                "condition": {"type": "TEXT_EQ",
                              "values": [{"userEnteredValue": value}]},
                # условное форматирование принимает только bold/italic/
                # strikethrough и цвета — шрифт и размер сюда передавать нельзя
                "format": {"backgroundColor": bg,
                           "textFormat": {"foregroundColor": BRAND["ink"]}}}},
            "index": 0}})

    # выходные дни в Сводке — лёгкий серый фон
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [{"sheetId": ws_summary.id, "startRowIndex": 2,
                    "startColumnIndex": 0, "endColumnIndex": 5}],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue":
                              '=AND(LEN($A3)=10,OR(WEEKDAY($A3,2)=6,WEEKDAY($A3,2)=7))'}]},
            "format": {"backgroundColor": {"red":0.925,"green":0.925,"blue":0.925},
                       "textFormat": {"foregroundColor": {"red":0.6,"green":0.6,"blue":0.6}}}}},
        "index": 0}})

    # строка баланса в Сводке — зелёный/красный фон + жирный, через условное форматирование
    # строка баланса начинается с "Баланс"
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [{"sheetId": ws_summary.id, "startRowIndex": 2,
                    "startColumnIndex": 0, "endColumnIndex": 5}],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue": '=LEFT($A3,6)="Баланс"'}]},
            "format": {"backgroundColor": BRAND["green"],
                       "textFormat": {"foregroundColor": BRAND["white"], "bold": True}}}},
        "index": 0}})

    # прошлые правила и зебру убираем, иначе они копятся при каждом запуске
    meta = sh.fetch_sheet_metadata()
    cleanup = []
    for sheet in meta.get("sheets", []):
        sid = sheet["properties"]["sheetId"]
        if sid not in (ws_summary.id, ws_detail.id):
            continue
        for b in sheet.get("bandedRanges", []):
            cleanup.append({"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}})
        for i in range(len(sheet.get("conditionalFormats", [])) - 1, -1, -1):
            cleanup.append({"deleteConditionalFormatRule":
                            {"sheetId": sid, "index": i}})

    # Батч атомарен: одна упавшая правка отменяет все остальные. Поэтому
    # объединение ячеек (падает, если плашка уже объединена) шлём отдельно
    # и его ошибку игнорируем, а на основной батч ругаемся вслух.
    merges = [r for r in reqs if "mergeCells" in r]
    rest = [r for r in reqs if "mergeCells" not in r]

    for m in merges:
        try:
            sh.batch_update({"requests": [m]})
        except Exception:
            pass

    try:
        sh.batch_update({"requests": cleanup + rest})
    except Exception as e:
        print(f"! оформление применить не удалось: {e}", file=sys.stderr)


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="конкретный день, YYYY-MM-DD")
    p.add_argument("--days", type=int, default=1, help="сколько последних дней")
    p.add_argument("--include-today", action="store_true",
                   help="включить текущий день (для промежуточных отчётов)")
    p.add_argument("--dry-run", action="store_true", help="без выгрузки в Google Sheets")
    p.add_argument("--json", help="дополнительно сохранить сырые данные в файл")
    args = p.parse_args()

    clf = Classifier(RULES_PATH)
    buckets = find_buckets()

    if args.date:
        targets = [date.fromisoformat(args.date)]
    else:
        today = date.today()
        if args.include_today:
            # сегодня и (days-1) предыдущих дней
            targets = [today - timedelta(days=i) for i in range(args.days - 1, -1, -1)]
        else:
            targets = [today - timedelta(days=i) for i in range(args.days, 0, -1)]

    days = [build_day(d, buckets, clf) for d in targets]
    # дни без единой записи в таблицу не пишем (иначе появляются пустые строки)
    days = [d for d in days if sum(d["by_category"].values()) > 0]
    if not days:
        print("За выбранный период данных нет.")
        return
    for d in days:
        print_day(d)

    if args.json:
        Path(args.json).write_text(json.dumps(days, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    if not args.dry_run:
        push_to_sheets(days)


if __name__ == "__main__":
    main()
