#!/usr/bin/env python3
import socket, subprocess, time, sys, os
from pathlib import Path
from datetime import datetime

SCRIPT       = Path(__file__).parent / "worktime_report.py"
MIN_INTERVAL = 1800
CHECK_EVERY  = 60
TEST_HOST    = ("oauth2.googleapis.com", 443)

last_run = 0
attempt  = 0

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[watcher] {ts} {msg}", flush=True)

def has_network():
    try:
        socket.setdefaulttimeout(5)
        socket.getaddrinfo(*TEST_HOST)
        return True
    except OSError:
        return False

def run_report():
    global last_run, attempt
    attempt += 1
    log(f"запускаю отчёт (попытка #{attempt}, pid={os.getpid()})")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--days", "2", "--include-today"],
            timeout=300
        )
        if result.returncode == 0:
            last_run = time.time()
            attempt  = 0
            log("успешно записано в Google Sheets")
        else:
            log(f"ОШИБКА код={result.returncode}, повтор через {CHECK_EVERY} сек")
    except subprocess.TimeoutExpired:
        log("ТАЙМАУТ — скрипт завис, повтор через CHECK_EVERY сек")
    except Exception as e:
        log(f"ИСКЛЮЧЕНИЕ: {e}, повтор через {CHECK_EVERY} сек")

log(f"старт pid={os.getpid()} python={sys.executable}")

while True:
    try:
        if has_network():
            elapsed = time.time() - last_run
            if elapsed >= MIN_INTERVAL:
                run_report()
    except Exception as e:
        log(f"ОШИБКА В ЦИКЛЕ: {e}")
    time.sleep(CHECK_EVERY)
