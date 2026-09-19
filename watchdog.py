#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SERVICE = "bourse-bot.service"
PROJECT = Path("/root/bourse-alert")
DB_PATH = PROJECT / "scores_history.db"
CHART_DIR = PROJECT / "charts"
ENV_PATH = Path("/etc/bourse-alert/bourse-alert.env")
STATE_PATH = Path("/var/lib/bourse-alert-watchdog/state.json")
TEHRAN = ZoneInfo("Asia/Tehran")
MARKET_DAYS = {0, 1, 2, 5, 6}  # Saturday through Wednesday


def command(*args):
    return subprocess.run(args, text=True, capture_output=True, timeout=20)


def load_env():
    values = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def send_alert(text):
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID", "-1004419199993")
    proxy = env.get("TELEGRAM_PROXY", "https://tg-proxy.m-taghvaei74.workers.dev").rstrip("/")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    response = requests.post(
        f"{proxy}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected watchdog alert: {payload}")


def market_window(now):
    if now.weekday() not in MARKET_DAYS:
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 + 5 <= minute <= 12 * 60 + 25


def latest_success_age(now):
    since = (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    result = command("journalctl", "-u", SERVICE, "--since", since, "--no-pager", "-o", "cat")
    lines = [line for line in result.stdout.splitlines() if "TSETMC market data fetched" in line]
    if not lines:
        return None
    # journalctl time parsing is deliberately avoided; a matching success in the
    # bounded five-minute query below is the authoritative freshness check.
    fresh_since = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = command("journalctl", "-u", SERVICE, "--since", fresh_since, "--no-pager", "-o", "cat")
    return 0 if "TSETMC market data fetched" in fresh.stdout else 999


def latest_snapshot_age(now):
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect('file:' + str(DB_PATH) + '?mode=ro', uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'v2_slots' in tables:
            row = conn.execute("SELECT MAX(ts) FROM v2_slots").fetchone()
        elif 'market_snapshots' in tables:
            row = conn.execute("SELECT MAX(timestamp) FROM market_snapshots").fetchone()
        else:
            return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    stamp = datetime.fromisoformat(row[0])
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TEHRAN)
    return (now - stamp.astimezone(TEHRAN)).total_seconds() / 60


def chart_age(now):
    files = [CHART_DIR / "market_panels.png", CHART_DIR / "industries.png", CHART_DIR / "allocation.png"]
    existing = [path for path in files if path.exists()]
    if len(existing) != len(files):
        return None
    newest_generation = min(path.stat().st_mtime for path in existing)
    return (now.timestamp() - newest_generation) / 60


def confirmed_closed(now):
    """A recent successful source check may confirm no session today."""
    try:
        health = json.loads((PROJECT / 'runtime_health.json').read_text(encoding='utf-8'))
        checked = datetime.fromisoformat(health['checked_at'])
        if checked.tzinfo is None:
            return False
        age = (now - checked).total_seconds()
        source = datetime.fromisoformat(health['source_asof'])
        if source.tzinfo is None:
            return False
        return (0 <= age <= 300 and health.get('market_date') == str(now.date())
                and source.astimezone(TEHRAN).date() == now.date()
                and -60 <= (now - source).total_seconds() <= 300
                and health.get('status') == 'closed' and health.get('session_open') is False)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def inspect(now):
    problems = []
    active = command("systemctl", "is-active", SERVICE).stdout.strip() == "active"
    if not active:
        problems.append("سرویس ربات متوقف است")
    if active and market_window(now):
        # Polling is independent of ten-minute report/snapshot persistence.
        fetch_age = latest_success_age(now)
        snapshot_age = latest_snapshot_age(now)
        if fetch_age is None or fetch_age > 5:
            problems.append("بیش از ۵ دقیقه دریافت موفق داده ثبت نشده است")
        if confirmed_closed(now):
            return problems
        try:
            health = json.loads((PROJECT / 'runtime_health.json').read_text(encoding='utf-8'))
            if health.get('status') in ('stale', 'closed'):
                problems.append("داده تازه برای وضعیت بازار در دسترس نیست؛ تعطیلی تأیید نشده است")
        except (OSError, ValueError, TypeError):
            pass
        if now.hour * 60 + now.minute >= 9 * 60 + 20 and (snapshot_age is None or snapshot_age > 20):
            problems.append("ذخیره وضعیت بازار بیش از ۲۰ دقیقه متوقف شده است")
        c_age = chart_age(now)
        if now.hour * 60 + now.minute >= 9 * 60 + 35 and (c_age is None or c_age > 25):
            problems.append("نمودارهای تلگرام بیش از ۲۵ دقیقه به‌روز نشده‌اند")
    return problems


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "unknown", "problems": []}


def save_state(status, problems):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"status": status, "problems": problems}, ensure_ascii=False), encoding="utf-8")


def main():
    now = datetime.now(TEHRAN)
    if "--test" in sys.argv:
        send_alert("✅ مانیتور خودکار ربات بورس فعال شد.\nاز این پس خرابی و بازیابی سیستم به‌صورت خودکار در تلگرام اعلام می‌شود.")
        print("test alert sent")
        return

    previous = load_state()
    problems = inspect(now)
    repaired = False
    restarted = False
    active = command('systemctl', 'is-active', SERVICE).stdout.strip() == 'active'
    fetch_age = latest_success_age(now) if active and problems and market_window(now) else None
    # Healthy polling with unavailable upstream data cannot be repaired by restart.
    if problems and market_window(now) and (not active or fetch_age is None or fetch_age > 5):
        restarted = True
        command("systemctl", "restart", SERVICE)
        time.sleep(8)
        remaining = inspect(datetime.now(TEHRAN))
        repaired = not remaining
        problems = remaining

    if problems:
        status = "problem"
        if previous.get("status") != status or previous.get("problems") != problems:
            details = "\n".join(f"• {item}" for item in problems)
            action = "راه‌اندازی مجدد خودکار انجام شد، اما مشکل همچنان باقی است." if restarted else "دریافت داده فعال است؛ داده بازار یا خروجی‌ها نیاز به بررسی دارند. راه‌اندازی مجدد انجام نشد."
            send_alert(f"🚨 هشدار ربات بورس\n{details}\n\n{action}")
    else:
        status = "ok"
        if previous.get("status") == "problem":
            send_alert("✅ مشکل ربات بورس برطرف شد و ارسال گزارش‌ها دوباره در وضعیت عادی است.")
        elif repaired:
            send_alert("✅ اختلال ربات بورس با راه‌اندازی مجدد خودکار برطرف شد.")

    save_state(status, problems)
    print(status, *problems, sep=" | ")


if __name__ == "__main__":
    main()
