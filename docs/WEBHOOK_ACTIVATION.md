# Включение вебхуков в проде (Railway)

Справочная процедура. Пройдена вручную один раз — зафиксирована здесь, чтобы
не восстанавливать по памяти при повторном включении/откате.

Контекст: до этого прод работал на `main.py` (long-polling). Вебхук-режим
поднимает `runtime/combined_app.py` — тот же процесс обслуживает и фабричный
бот (sentinel `id=0`), и клиентских ботов через один HTTPS-эндпоинт
`/webhook/{bot_id}` (см. `STAGE2_REPORT.md`, разделы «Фабрика как житель
реестра» и «WEBHOOK_SECRET fail-closed»).

**Предпосылки:**
- Ветка с вебхук-кодом уже в `master` (`combined_app.py`, реестр, fail-closed
  секрет, фикс `sys.path` для прямого запуска — см. `STAGE2_REPORT.md`).
- Клиентские боты сейчас запускаются как отдельные подпроцессы (polling,
  `services/bot_runner`). Регистрация вебхука на конкретного бота переводит
  ЕГО на вебхук; неохваченные боты продолжают polling.

---

## Шаг 1. Публичный домен в Railway

Railway → сервис → **Settings → Networking → Public Networking → Generate
Domain**, порт **8080** (тот же порт, что слушает `combined_app.py`:
`int(os.getenv("PORT", "8080"))`).

Получается адрес вида:

```
bot-creter-production.up.railway.app
```

Это host без схемы — `https://` добавляется в `PUBLIC_BASE_URL` на следующем
шаге.

---

## Шаг 2. Переменные окружения (Railway → Variables)

| Переменная       | Значение                                              | Примечание |
|------------------|-------------------------------------------------------|------------|
| `PUBLIC_BASE_URL`| `https://bot-creter-production.up.railway.app`        | **С** `https://`, **без** завершающего слэша. Из него строится `{base}/webhook/{bot_id}`. |
| `WEBHOOK_SECRET` | `<секрет>`                                             | Уже был задан. Telegram шлёт его в заголовке `X-Telegram-Bot-Api-Secret-Token`; эндпоинт fail-closed — без секрета отвечает 403 на всё. |
| `BOT_SCRIPT`     | `runtime/combined_app.py`                             | Переключает точку входа с `main.py` на `combined_app.py`. `start.sh`: `exec python ${BOT_SCRIPT:-main.py}`. |

После сохранения переменных Railway передеплоит сервис — теперь запущен
`combined_app.py`, HTTPS-эндпоинт `/webhook/{bot_id}` доступен, но Telegram
ещё не знает, куда слать апдейты (вебхук на стороне Telegram не зарегистрирован
— это Шаг 3).

Проверка, что процесс поднялся:

```
https://bot-creter-production.up.railway.app/health   →   {"status": "ok"}
```

---

## Шаг 3. Регистрация вебхука в Telegram (Railway Console)

Открыть **Railway → сервис → Console** (интерактивный шелл в контейнере).

### ⚠️ Важный нюанс: venv

Интерактивная Console использует **системный python**, в котором НЕТ пакетов
проекта — `python -m runtime.webhook_setup` упадёт с
`ModuleNotFoundError: No module named 'aiogram'`. Пакеты установлены в
**`/opt/venv`**. Поэтому команду запускать явно через venv-python:

```bash
/opt/venv/bin/python -m runtime.webhook_setup <bot_id> --token <token> --apply
```

- `PUBLIC_BASE_URL` и `WEBHOOK_SECRET` подхватываются из env автоматически
  (`webhook_setup.py`: дефолты аргументов `--base-url`/`--secret` берутся из
  этих переменных).
- `--apply` — реальный вызов `setWebhook` у Telegram. Без него — сухой прогон
  (только печатает URL).
- Запускается **через `-m` (модульная форма)** — тогда корень проекта на
  `sys.path`, `ModuleNotFoundError: config` не возникает (это отдельный класс
  проблемы от venv выше; `webhook_setup.py` и так всегда запускался только
  через `-m`).

### Фабричный бот (sentinel id=0)

```bash
/opt/venv/bin/python -m runtime.webhook_setup 0 --token $BOT_TOKEN --apply
```

`$BOT_TOKEN` — токен фабрики, уже есть в env. `id=0` — тот самый sentinel
`FACTORY_BOT_ID`; регистрируется URL `.../webhook/0`, который `combined_app.py`
маршрутизирует на фабричный `Dispatcher` (см. `STAGE2_REPORT.md`).

### Клиентский бот

```bash
/opt/venv/bin/python -m runtime.webhook_setup <bot_id> --token <token> --apply
```

`<bot_id>` — `bots.id` из таблицы; `<token>` — токен этого бота.
`webhook_setup.py` работает по одному боту за вызов (список ботов из БД сам не
читает) — для нескольких клиентских ботов команду повторить с их id/токенами.

Успешный вывод:

```
Webhook URL for bot <id>: https://bot-creter-production.up.railway.app/webhook/<id>
Webhook registered with Telegram.
```

---

## Шаг 4. Проверка

Написать боту в Telegram — он должен ответить. Апдейты идут через вебхук, не
через polling. Если бот молчит, проверить в порядке вероятности:

- `/health` отвечает `{"status": "ok"}` — процесс жив.
- В Railway-логах при старте есть строка вида
  `Combined registry built: N bot(s), including the factory bot`.
- Секрет совпадает: `WEBHOOK_SECRET` в Variables == секрет, с которым
  регистрировали (иначе Telegram шлёт запросы, а эндпоинт отвечает 403 —
  в логах будет `webhook secret not configured` при пустом секрете либо тихий
  403 при несовпадении).
- Статус вебхука на стороне Telegram:
  `https://api.telegram.org/bot<token>/getWebhookInfo` — поле `url` должно
  указывать на `.../webhook/<bot_id>`, `pending_update_count` не должен расти.

---

## Шаг 5. Откат на polling

Откат состоит из **двух** действий — недостаточно просто убрать `BOT_SCRIPT`:
если у бота остался зарегистрированный вебхук, Telegram отдаёт апдейты только
по вебхуку, и вернувшийся polling их не увидит (для одного бота одновременно
активен либо вебхук, либо `getUpdates`, не оба).

**5.1. Убрать `BOT_SCRIPT` из Railway → Variables** (или задать
`BOT_SCRIPT=main.py`) и передеплоить — точка входа снова `main.py` (polling).

**5.2. Снять вебхук у Telegram** для каждого бота, которому его ставили.
Отдельного режима в `webhook_setup.py` нет (там только `setWebhook`), поэтому
одноразовой командой через venv-python:

```bash
/opt/venv/bin/python -c "import asyncio; from aiogram import Bot; \
b=Bot(token='<token>'); \
asyncio.run(b.delete_webhook(drop_pending_updates=True))"
```

Для фабричного бота `<token>` = `$BOT_TOKEN`.

> Примечание: `main.py` при старте своего polling для ФАБРИЧНОГО бота и так
> вызывает `bot.delete_webhook(drop_pending_updates=True)` (см. `main.py`,
> перед `start_polling`) — то есть после отката п.5.1 вебхук фабрики снимется
> сам при первом же запуске `main.py`. Явная команда 5.2 нужна прежде всего
> для КЛИЕНТСКИХ ботов (их подпроцессы-polling через `services/bot_runner`
> не гарантируют вызов `delete_webhook`), а также как страховка, если хочется
> снять вебхук, не дожидаясь передеплоя.

Проверка отката: `getWebhookInfo` возвращает пустой `url`; бот отвечает,
работая на polling.
