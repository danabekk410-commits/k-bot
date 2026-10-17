#!/usr/bin/env python3
"""Напоминалки K-Bot, крон каждые 5 минут.

- разовые из data/reminders.json (kb.py remind add) — когда подошло время;
- в 17:00 Алматы — вечерний чек кофейни каждый день: проверить остатки,
  кому пора заказывать (поле «Следующий заказ»), у кого дата не задана.
"""
import fcntl
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime

BIN = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN)
sys.path.insert(0, BIN)
import kb  # noqa: E402

CHAT_ID = kb.env("KBOT_CHAT_ID")  # Telegram chat_id владельца
# Токен бота: из .env репозитория или из .env Telegram-плагина Claude Code.
PLUGIN_ENV = os.path.expanduser("~/.claude/channels/telegram/.env")


def token():
    tok = kb.env("TELEGRAM_BOT_TOKEN", required=False)
    if tok:
        return tok
    try:
        for line in open(PLUGIN_ENV, encoding="utf-8"):
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                if tok:
                    return tok
    except OSError:
        pass
    sys.exit("нет TELEGRAM_BOT_TOKEN")


def send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % token(), data=data, timeout=30)


def one_off(now):
    with kb.reminders_lock():
        _one_off(now)


def _one_off(now):
    items = kb.load_reminders()
    changed = False
    for x in items:
        if not x["sent"] and datetime.fromisoformat(x["at"]) <= now:
            send("Напоминаю: " + x["text"])
            x["sent"] = True
            changed = True
            kb.save_reminders(items)  # сразу, чтобы сбой на следующей не повторил эту
    if changed:
        # отправленные старше недели не храним
        keep = [x for x in items if not x["sent"] or (now - datetime.fromisoformat(x["at"])).days < 7]
        kb.save_reminders(keep)


def evening_check():
    lines = ["Вечерний чек кофейни: глянь молоко, кофе, сиропы, выпечку, стаканы и химию."]
    r = subprocess.run([sys.executable, os.path.join(BIN, "kb.py"), "due"],
                       capture_output=True, text=True, timeout=60)
    out = r.stdout.strip()
    if r.returncode != 0:
        lines.append("Не смог проверить заказы — " + (r.stderr.strip() or out)[:300])
    elif out and not out.startswith("Сегодня заказывать никого"):
        lines.append("Пора заказать:\n" + "\n".join("- " + l for l in out.splitlines()))
    else:
        lines.append("По графику сегодня заказывать никого.")
    try:
        nodate = [s["name"] for s in map(kb.supplier_row, kb.query(kb.SUPPLIERS_DB))
                  if s["active"] and not s["next"]]
    except (Exception, SystemExit):  # kb.call() выходит через sys.exit; чек важнее списка без дат
        nodate = []
    if nodate:
        lines.append("Без даты следующего заказа: " + ", ".join(nodate) + ".")
    lines.append("Заказал — напиши «заказал <кто>», сдвину дату.")
    send("\n\n".join(lines))


def main():
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    lock = open(os.path.join(ROOT, "logs", "remind.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    now = datetime.now(kb.ALMATY)
    one_off(now)
    if now.hour == 17 and now.minute < 5:
        evening_check()


if __name__ == "__main__":
    main()
