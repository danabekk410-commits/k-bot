#!/usr/bin/env python3
"""Учёт кофейни в Notion. Все суммы считает этот скрипт, а не модель.

Расходы — таблица «Расходы по дням»: строка = день, столбец = статья, в клетке
сумма за день. Каждая покупка прибавляется к клетке и пишется строкой в скрытый
столбец «Журнал» (операция | статья | сумма | оплата | что) — по нему повтор
с тем же --op не задваивает, а undo вычитает.

  kb.py add "Молоко" 1800 --cat Молоко --op 29301-1 [--qty "2 л"] [--pay Kaspi] [--supplier Magnum] [--date 2026-10-13] [--note ...]
  kb.py undo <операция>           отменить покупку: вычесть из клетки дня (29301-1)
  kb.py list [--days 7]           последние расходы
  kb.py report week|month [--date 2026-10-13]   итоги по статьям и способам оплаты
  kb.py suppliers                 поставщики и даты следующего заказа
  kb.py supplier "Поставщик" [--every 3] [--next 2026-10-15] [--what ...] [--contact ...] [--cat Выпечка]
  kb.py ordered "Поставщик"          заказ сделан: следующий = сегодня + every дней
  kb.py due                       кому пора заказывать (сегодня и просроченные)
  kb.py add "Молоко" --count 36 --price 570 --cat Молоко --op ...   сумму считает скрипт
  kb.py remind add "молоко, чанки" --at "2026-10-14 09:00"   разовая напоминалка (Алматы)
  kb.py remind list | kb.py remind del <n>
"""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

ALMATY = timezone(timedelta(hours=5))
API = "https://api.notion.com/v1"
# Порядок = порядок столбцов в «Расходах по дням».
CATS = ["Обеды", "Выпечка", "Зарплаты", "Кофе", "Молоко", "Сиропы", "Шоколад", "Вода",
        "Magnum", "Химия", "Хозтовары", "Посуда", "Касса", "Прочее"]
PAYS = ["Kaspi", "Наличные", "Перевод"]


ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")


def env(name, required=True):
    """Переменная из окружения или из .env в корне репозитория."""
    val = os.environ.get(name)
    if not val:
        try:
            for line in open(ENV_FILE, encoding="utf-8"):
                if line.startswith(name + "="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    if not val and required:
        sys.exit("ОШИБКА: нет %s (см. .env.example)" % name)
    return val


def load_token():
    return env("KBOT_NOTION_TOKEN")


# ID баз Notion: «Расходы по дням» и «Поставщики» — из .env.
DAYS_DB = env("KBOT_DAYS_DB", required=False)
SUPPLIERS_DB = env("KBOT_SUPPLIERS_DB", required=False)


class Ambiguous(Exception):
    """Запрос ушёл, ответа нет: Notion мог его выполнить."""


def call(method, path, body=None, ambiguous_ok=False):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + load_token(),
                 "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if ambiguous_ok and e.code >= 500:
            raise Ambiguous(str(e.code))
        sys.exit("ОШИБКА Notion %s: %s" % (e.code, e.read().decode()[:300]))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if ambiguous_ok:
            raise Ambiguous(repr(e))
        sys.exit("ОШИБКА сети: %r" % e)


def query(db, flt=None, sorts=None):
    if not db:
        sys.exit("ОШИБКА: не заданы KBOT_DAYS_DB / KBOT_SUPPLIERS_DB (см. .env.example)")
    out, cursor = [], None
    while True:
        body = {"page_size": 100}
        if flt:
            body["filter"] = flt
        if sorts:
            body["sorts"] = sorts
        if cursor:
            body["start_cursor"] = cursor
        d = call("POST", "/databases/%s/query" % db, body)
        out += d["results"]
        if not d.get("has_more"):
            return out
        cursor = d["next_cursor"]


def today():
    return datetime.now(ALMATY).date()


def txt(p):
    t = p.get("title") or p.get("rich_text") or []
    return "".join(x.get("plain_text", "") for x in t)


def pick(value, allowed, what):
    for a in allowed:
        if value.lower() == a.lower():
            return a
    sys.exit("ОШИБКА: %s «%s» нет в списке: %s" % (what, value, ", ".join(allowed)))


def money(n):
    return "{:,.0f}".format(n).replace(",", " ") + " ₸"


def find_supplier(name):
    rows = query(SUPPLIERS_DB)
    hits = [r for r in rows if name.lower() in txt(r["properties"]["Поставщик"]).lower()]
    if len(hits) > 1:
        exact = [r for r in hits if txt(r["properties"]["Поставщик"]).lower() == name.lower()]
        if len(exact) == 1:
            return exact[0]
        sys.exit("ОШИБКА: «%s» подходит под несколько: %s" % (
            name, ", ".join(txt(r["properties"]["Поставщик"]) for r in hits)))
    return hits[0] if hits else None


LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "kb.lock")


@contextlib.contextmanager
def days_lock():
    """add/undo читают клетку и пишут сумму — по одному за раз."""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def clean(s):
    return " ".join(str(s).replace("|", "/").split())


def journal(row):
    """Строки журнала дня: [{op, cat, sum, pay, what}]."""
    out = []
    for line in txt(row["properties"]["Журнал"]).splitlines():
        parts = [x.strip() for x in line.split("|", 4)]
        if len(parts) < 5:
            continue
        try:
            s = float(parts[2].replace(" ", ""))
        except ValueError:
            continue
        out.append({"op": parts[0], "cat": parts[1], "sum": s, "pay": parts[3], "what": parts[4]})
    return out


def num(x):
    """Точная запись суммы для журнала: 1234567 -> "1234567", 1800.5 -> "1800.5"."""
    x = float(x)
    return str(int(x)) if x.is_integer() else repr(x)


def journal_prop(entries):
    text = "\n".join("%s | %s | %s | %s | %s" % (e["op"], e["cat"], num(e["sum"]), e["pay"], e["what"])
                     for e in entries)
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    return {"rich_text": [{"text": {"content": c}} for c in chunks]}


def cell(row, cat):
    return row["properties"][cat]["number"] or 0


def day_title(d):
    return date.fromisoformat(d).strftime("%d.%m.%Y")


def find_day(d):
    rows = query(DAYS_DB, {"property": "Дата", "date": {"equals": d}})
    if len(rows) > 1:
        sys.exit("ОШИБКА: на %s в таблице %d строки. Оставь в Notion одну" % (day_title(d), len(rows)))
    return rows[0] if rows else None


def find_op(op):
    """Строка дня, в журнале которой есть операция op, и сама запись."""
    for row in query(DAYS_DB, {"property": "Журнал", "rich_text": {"contains": op}}):
        for e in journal(row):
            if e["op"] == op:
                return row, e
    return None, None


def confirm(check, what, op):
    """Ответ Notion потерян: повтора не шлём, только перечитываем."""
    for _ in range(5):
        time.sleep(4)
        r = check()
        if r:
            return r
    sys.exit("НЕ ПОДТВЕРЖДЕНО: Notion не ответил (%s). Через минуту повтори ту же команду "
             "с тем же --op %s — если запись есть, она не задвоится" % (what, op))


def get_day(d, op):
    row = find_day(d)
    if row:
        return row
    body = {"parent": {"database_id": DAYS_DB}, "properties": {
        "День": {"title": [{"text": {"content": day_title(d)}}]},
        "Дата": {"date": {"start": d}}}}
    try:
        return call("POST", "/pages", body, ambiguous_ok=True)
    except Ambiguous as e:
        return confirm(lambda: find_day(d), e, op)


def cmd_add(a):
    with days_lock():
        _cmd_add(a)


def _cmd_add(a):
    if a.sum is None:
        if a.count is None or a.price is None:
            sys.exit("ОШИБКА: нужна сумма или --count и --price")
        a.sum = round(a.count * a.price, 2)
        if not a.qty:
            a.qty = "%g × %s" % (a.count, money(a.price))
    elif a.count is not None or a.price is not None:
        sys.exit("ОШИБКА: либо сумма, либо --count/--price, не всё сразу")
    if a.sum <= 0:
        sys.exit("ОШИБКА: сумма должна быть больше нуля")
    cat = pick(a.cat, CATS, "статьи")
    pay = pick(a.pay, PAYS, "способа оплаты")
    d = a.date or today().isoformat()
    what = a.what
    if a.supplier:
        s = find_supplier(a.supplier)
        if not s:
            sys.exit("ОШИБКА: поставщика «%s» нет. Сначала: kb.py supplier \"%s\"" % (a.supplier, a.supplier))
        what += " (%s)" % txt(s["properties"]["Поставщик"])
    what = ", ".join(clean(x) for x in (what, a.qty, a.note) if x)
    row, old = find_op(a.op)
    if old:
        print("УЖЕ ЗАПИСАНО раньше (операция %s): %s — %s, %s" % (
            a.op, old["what"], money(old["sum"]), txt(row["properties"]["День"])))
        return
    # Свежее чтение прямо перед записью: окно, где ручная правка клетки
    # потеряется, — доли секунды (условной записи в Notion нет).
    row = call("GET", "/pages/" + get_day(d, a.op)["id"])
    entry = {"op": clean(a.op), "cat": cat, "sum": a.sum, "pay": pay, "what": what}
    props = {cat: {"number": round(cell(row, cat) + a.sum, 2)},
             "Журнал": journal_prop(journal(row) + [entry])}
    try:
        call("PATCH", "/pages/" + row["id"], {"properties": props}, ambiguous_ok=True)
    except Ambiguous as e:
        confirm(lambda: find_op(a.op)[1], e, a.op)
    print("ЗАПИСАНО: %s — %s, %s, %s (операция %s)" % (what, money(a.sum), cat, day_title(d), a.op))


def cmd_undo(a):
    with days_lock():
        row, e = find_op(a.op)
        if not e:
            sys.exit("ОШИБКА: операции %s нет в журнале. Посмотри kb.py list" % a.op)
        row = call("GET", "/pages/" + row["id"])
        e = next((x for x in journal(row) if x["op"] == a.op), None)
        if not e:
            sys.exit("ОШИБКА: операции %s уже нет в журнале" % a.op)
        left = round(cell(row, e["cat"]) - e["sum"], 2)
        props = {e["cat"]: {"number": left if left > 0 else None},
                 "Журнал": journal_prop([x for x in journal(row) if x["op"] != a.op])}
        call("PATCH", "/pages/" + row["id"], {"properties": props})
    print("УДАЛЕНО: %s — %s, %s" % (e["what"], money(e["sum"]), txt(row["properties"]["День"])))


def days_between(start, end):
    return query(DAYS_DB, {"and": [
        {"property": "Дата", "date": {"on_or_after": start.isoformat()}},
        {"property": "Дата", "date": {"on_or_before": end.isoformat()}}]},
        [{"property": "Дата", "direction": "ascending"}])


def day_total(row):
    return sum(cell(row, c) for c in CATS)


def cmd_list(a):
    end = today()
    rows = days_between(end - timedelta(days=a.days - 1), end)
    if not any(day_total(r) for r in rows):
        print("За %d дн. расходов нет." % a.days)
        return
    for r in rows:
        t = day_total(r)
        if not t:
            continue
        print("%s — %s" % (txt(r["properties"]["День"]), money(t)))
        logged = 0
        for e in journal(r):
            logged += e["sum"]
            print("  %s — %s [%s, %s] операция %s" % (e["what"], money(e["sum"]), e["cat"], e["pay"], e["op"]))
        if round(t - logged, 2):
            print("  вписано в таблицу вручную: %s" % money(t - logged))
    print("ИТОГО: %s" % money(sum(day_total(r) for r in rows)))


def cmd_report(a):
    ref = date.fromisoformat(a.date) if a.date else today()
    if a.period == "week":
        start = ref - timedelta(days=ref.weekday())
        end = start + timedelta(days=6)
    else:
        start = ref.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    rows = days_between(start, end)
    by_cat, by_pay = defaultdict(float), defaultdict(float)
    for r in rows:
        for c in CATS:
            by_cat[c] += cell(r, c)
        for e in journal(r):
            by_pay[e["pay"]] += e["sum"]
    total = sum(by_cat.values())
    print("РАСХОДЫ %s — %s" % (start.strftime("%d.%m"), end.strftime("%d.%m.%Y")))
    if not total:
        print("Записей нет.")
        return
    for c, s in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        if s:
            print("- %s: %s (%d%%)" % (c, money(s), round(100 * s / total)))
    manual = round(total - sum(by_pay.values()), 2)
    if manual:
        by_pay["вписано вручную"] = manual
    print("Оплата: " + ", ".join("%s %s" % (k, money(v)) for k, v in sorted(by_pay.items())))
    print("ИТОГО: %s, дней с расходами %d" % (money(total), sum(1 for r in rows if day_total(r))))


def supplier_row(r):
    p = r["properties"]
    return {
        "name": txt(p["Поставщик"]),
        "what": txt(p["Что берём"]),
        "contact": txt(p["Контакт"]),
        "every": p["Как часто, дней"]["number"],
        "next": (p["Следующий заказ"]["date"] or {}).get("start", "")[:10],
        "active": p["Активен"]["checkbox"],
    }


def cmd_suppliers(a):
    for r in query(SUPPLIERS_DB):
        s = supplier_row(r)
        print("%s%s — %s; каждые %s дн.; следующий: %s; контакт: %s" % (
            s["name"], "" if s["active"] else " (не активен)", s["what"] or "?",
            int(s["every"]) if s["every"] else "?", s["next"] or "не задан", s["contact"] or "—"))


def cmd_supplier(a):
    props = {}
    if a.every is not None:
        if a.every <= 0:
            sys.exit("ОШИБКА: --every больше нуля")
        props["Как часто, дней"] = {"number": a.every}
    if a.next:
        date.fromisoformat(a.next)
        props["Следующий заказ"] = {"date": {"start": a.next}}
    if a.what:
        props["Что берём"] = {"rich_text": [{"text": {"content": a.what}}]}
    if a.contact:
        props["Контакт"] = {"rich_text": [{"text": {"content": a.contact}}]}
    if a.cat:
        props["Статья"] = {"multi_select": [{"name": pick(c.strip(), CATS, "статьи")} for c in a.cat.split(",")]}
    s = find_supplier(a.name)
    if s:
        call("PATCH", "/pages/" + s["id"], {"properties": props})
        print("ОБНОВЛЁН: " + txt(s["properties"]["Поставщик"]))
    else:
        props["Поставщик"] = {"title": [{"text": {"content": a.name}}]}
        props["Активен"] = {"checkbox": True}
        call("POST", "/pages", {"parent": {"database_id": SUPPLIERS_DB}, "properties": props})
        print("ДОБАВЛЕН: " + a.name)


def cmd_ordered(a):
    s = find_supplier(a.name)
    if not s:
        sys.exit("ОШИБКА: поставщика «%s» нет" % a.name)
    row = supplier_row(s)
    if not row["every"]:
        sys.exit("ОШИБКА: у «%s» не задано «Как часто». Спроси, раз в сколько дней заказываем" % row["name"])
    nxt = today() + timedelta(days=int(row["every"]))
    call("PATCH", "/pages/" + s["id"], {"properties": {"Следующий заказ": {"date": {"start": nxt.isoformat()}}}})
    print("ОТМЕЧЕНО: %s, следующий заказ %s" % (row["name"], nxt.strftime("%d.%m")))


def due_list():
    t = today().isoformat()
    out = []
    for r in query(SUPPLIERS_DB):
        s = supplier_row(r)
        if s["active"] and s["next"] and s["next"] <= t:
            out.append(s)
    return out


def cmd_due(a):
    rows = due_list()
    if not rows:
        print("Сегодня заказывать никого не нужно.")
    for s in rows:
        late = (today() - date.fromisoformat(s["next"])).days
        print("%s — %s%s" % (s["name"], s["what"] or "заказ", " (просрочено на %d дн.)" % late if late else ""))


REMINDERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "reminders.json")


@contextlib.contextmanager
def reminders_lock():
    """Один замок на чтение-правку-запись reminders.json для CLI и крона."""
    os.makedirs(os.path.dirname(REMINDERS), exist_ok=True)
    with open(REMINDERS + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def load_reminders():
    try:
        return json.load(open(REMINDERS, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_reminders(items):
    os.makedirs(os.path.dirname(REMINDERS), exist_ok=True)
    tmp = REMINDERS + ".tmp"
    json.dump(items, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, REMINDERS)


def cmd_remind(a):
    with reminders_lock():
        _cmd_remind(a)


def _cmd_remind(a):
    items = load_reminders()
    if a.action == "add":
        if not a.text or not a.at:
            sys.exit("ОШИБКА: нужно: remind add \"текст\" --at \"ГГГГ-ММ-ДД ЧЧ:ММ\"")
        at = datetime.strptime(a.at, "%Y-%m-%d %H:%M").replace(tzinfo=ALMATY)
        if at <= datetime.now(ALMATY):
            sys.exit("ОШИБКА: время уже прошло")
        items.append({"text": a.text, "at": at.isoformat(), "sent": False})
        save_reminders(items)
        print("НАПОМНЮ: %s — %s" % (at.strftime("%d.%m %H:%M"), a.text))
    elif a.action == "list":
        pending = [(i, x) for i, x in enumerate(items, 1) if not x["sent"]]
        if not pending:
            print("Напоминалок нет.")
        for i, x in pending:
            print("%d. %s — %s" % (i, datetime.fromisoformat(x["at"]).strftime("%d.%m %H:%M"), x["text"]))
    else:
        n = int(a.text or 0)
        if not 1 <= n <= len(items) or items[n - 1]["sent"]:
            sys.exit("ОШИБКА: нет такой напоминалки")
        x = items.pop(n - 1)
        save_reminders(items)
        print("УДАЛЕНА: " + x["text"])


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("add"); p.add_argument("what"); p.add_argument("sum", type=float, nargs="?")
    p.add_argument("--count", type=float); p.add_argument("--price", type=float)
    p.add_argument("--cat", required=True); p.add_argument("--op", required=True)
    p.add_argument("--qty"); p.add_argument("--pay", default="Kaspi")
    p.add_argument("--supplier"); p.add_argument("--date"); p.add_argument("--note")
    p = sp.add_parser("undo"); p.add_argument("op")
    p = sp.add_parser("list"); p.add_argument("--days", type=int, default=7)
    p = sp.add_parser("report"); p.add_argument("period", choices=["week", "month"]); p.add_argument("--date")
    sp.add_parser("suppliers")
    p = sp.add_parser("supplier"); p.add_argument("name"); p.add_argument("--every", type=int)
    p.add_argument("--next"); p.add_argument("--what"); p.add_argument("--contact"); p.add_argument("--cat")
    p = sp.add_parser("ordered"); p.add_argument("name")
    sp.add_parser("due")
    p = sp.add_parser("remind"); p.add_argument("action", choices=["add", "list", "del"])
    p.add_argument("text", nargs="?"); p.add_argument("--at")
    a = ap.parse_args()
    if getattr(a, "date", None):
        date.fromisoformat(a.date)
    {"add": cmd_add, "undo": cmd_undo, "list": cmd_list, "report": cmd_report,
     "suppliers": cmd_suppliers, "supplier": cmd_supplier, "ordered": cmd_ordered,
     "due": cmd_due, "remind": cmd_remind}[a.cmd](a)


if __name__ == "__main__":
    main()
