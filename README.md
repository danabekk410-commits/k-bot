# K-Bot — учёт расходов кофейни в Telegram

Telegram-бот, через которого владелец кофейни ведёт расходы, поставщиков и
напоминалки обычными сообщениями и голосовыми: «купил молоко 36 л по 570 каспи» —
и запись уже в таблице Notion.

Бот — это сессия Claude Code (модель Sonnet) с официальным Telegram-плагином.
Модель понимает сообщение, а все деньги считает детерминированный скрипт
`bin/kb.py`: модель никогда не складывает суммы сама.

## Что умеет

- **Расходы по дням** в Notion: строка — день, столбец — статья, в клетке — сумма.
  Подробности покупки (что, сколько, чем платили) — в скрытом столбце «Журнал».
- **Идемпотентность**: у каждой покупки есть `--op` (id сообщения + номер), повтор
  не задваивает запись; при обрыве связи с Notion скрипт перечитывает, а не шлёт повторно.
- **Отчёты** за неделю и месяц: по статьям, по способам оплаты, ручные правки клеток видны отдельно.
- **Поставщики**: как часто заказываем, когда следующий заказ, кому пора сегодня.
- **Напоминалки**: разовые («напомни завтра в 9 про молоко») и ежедневный
  вечерний чек в 17:00 по Алматы — крон `bin/remind.py`.
- **Голосовые**: хук `UserPromptSubmit` (`bin/pretranscribe.py`) скачивает голосовое
  через Bot API и расшифровывает локально faster-whisper ещё до начала хода.
- **База заметок PARA** (`vault/`): решения по меню, персоналу, закупу — markdown,
  коммит на каждое изменение. В репозитории — только пустой каркас.

## Стек

Python 3 (только stdlib в `kb.py`/`remind.py`), Notion API, Telegram Bot API,
Claude Code + `plugin:telegram`, faster-whisper (CPU, int8), systemd, cron.

## Структура

```
bin/kb.py                 CLI учёта: add / undo / list / report / suppliers / due / remind
bin/remind.py             крон каждые 5 минут: напоминалки и вечерний чек
bin/pretranscribe.py      хук Claude Code: голосовое → текст до начала хода
bin/transcribe_local.py   faster-whisper small, русский
CLAUDE.md                 инструкции боту (персона, правила, команды)
vault/                    каркас базы заметок PARA
deploy/                   примеры systemd-юнита, crontab и настроек Claude Code
```

## Как запустить

1. В Notion создать две базы:
   - «Расходы по дням»: `День` (title), `Дата` (date), числовые столбцы под каждую статью
     из `CATS` в `kb.py`, `Журнал` (text);
   - «Поставщики»: `Поставщик` (title), `Что берём`, `Контакт` (text), `Как часто, дней` (number),
     `Следующий заказ` (date), `Активен` (checkbox), `Статья` (multi-select).
   Подключить к ним интеграцию.
2. `cp .env.example .env` и заполнить токен Notion, ID баз, chat_id владельца.
3. Проверить: `python3 bin/kb.py list`.
4. Бот: установить Claude Code, включить плагин `telegram@claude-plugins-official`,
   сохранить токен бота, разрешить доступ только своему chat_id. Пример запуска —
   `deploy/kbot-claude.service.example`, настройки с хуком — `deploy/claude-settings.example.json`.
5. Напоминалки: строка из `deploy/crontab.example`.
6. Голосовые (по желанию): `pip install faster-whisper`.

## Скриншоты

<!-- TODO: скрин переписки с ботом и таблицы Notion (без реальных сумм) -->

## Демо

Публичного демо нет: бот работает в одной кофейне и отвечает только владельцу.

---

## English summary

K-Bot is a Telegram bot for a coffee shop owner to log expenses, suppliers and
reminders in plain Russian text or voice messages. It runs as a Claude Code
session (Sonnet) with the official Telegram plugin; the model parses the message,
while all money math is done by a deterministic Python CLI (`bin/kb.py`) that writes
to Notion with idempotent operation IDs. Includes weekly/monthly reports, supplier
reorder schedule, cron reminders and local voice transcription with faster-whisper.
Stack: Python (stdlib), Notion API, Telegram Bot API, Claude Code, systemd, cron.

## Лицензия

MIT — см. [LICENSE](LICENSE).
