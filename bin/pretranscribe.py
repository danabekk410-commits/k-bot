#!/usr/bin/env python3
"""UserPromptSubmit K-Bot: расшифровывает голосовое ДО начала хода локальной
моделью faster-whisper (без внешних ключей).

Раньше на каждое голосовое уходило два лишних вызова инструментов внутри хода
(download_attachment, затем Bash с transcribe.py), и владелец ждал молча. Тег
<channel> несёт attachment_file_id, поэтому файл можно забрать самим через
Bot API и отдать модели готовый текст вместе с промптом.

Провал здесь не должен ломать ход: при любой ошибке выходим с кодом 0 и без
вывода — модель просто скачает вложение сама, как делала раньше.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
BIN = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BIN, "..", "logs", "transcribe.log")
CACHE_DIR = os.path.join(HOME, ".claude", "channels", "telegram", "inbox")
TRANSCRIBE = os.path.join(BIN, "transcribe_local.py")
PYTHON = sys.executable


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} [pre] {msg}\n")
    except OSError:
        pass


def token():
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        return os.environ["TELEGRAM_BOT_TOKEN"]
    for p in (os.path.join(BIN, "..", ".env"), os.path.join(HOME, ".claude/channels/telegram/.env")):
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("TELEGRAM_BOT_TOKEN="):
                        tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if tok:  # пустое значение = не задано, смотрим следующий файл
                            return tok
        except OSError:
            continue
    return None


def fetch(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def main():
    try:
        prompt = json.load(sys.stdin).get("prompt") or ""
    except Exception:
        return

    if 'attachment_kind="voice"' not in prompt and 'attachment_kind="audio"' not in prompt:
        return
    m = re.search(r'attachment_file_id="([^"]+)"', prompt)
    if not m:
        return
    file_id = m.group(1)

    tok = token()
    if not tok:
        log("токена нет — пропускаю")
        return

    try:
        info = json.loads(fetch(
            f"https://api.telegram.org/bot{tok}/getFile?file_id={file_id}"))
        if not info.get("ok"):
            log(f"getFile отказ: {str(info)[:150]}")
            return
        path = info["result"]["file_path"]
        os.makedirs(CACHE_DIR, exist_ok=True)
        dst = os.path.join(CACHE_DIR, f"pre-{file_id[-16:]}{os.path.splitext(path)[1] or '.oga'}")
        if not os.path.exists(dst):
            with open(dst, "wb") as f:
                f.write(fetch(f"https://api.telegram.org/file/bot{tok}/{path}"))
    except Exception as e:
        log(f"скачивание не вышло: {e!r}")
        return

    try:
        r = subprocess.run([PYTHON, TRANSCRIBE, dst],
                           capture_output=True, text=True, timeout=170)
    except subprocess.TimeoutExpired:
        log("расшифровка не уложилась в 170с")
        return
    text = (r.stdout or "").strip()
    if r.returncode != 0 or not text:
        log(f"расшифровка пустая rc={r.returncode}: {r.stderr.strip()[:200]}")
        return

    log(f"готово, {len(text)} симв., файл {os.path.basename(dst)}")
    # Уходит в контекст хода вместе с промптом.
    print(f"[Голосовое уже расшифровано, скачивать вложение не нужно. "
          f"Файл: {dst}]\nРасшифровка: {text}")


if __name__ == "__main__":
    main()
