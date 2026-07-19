# Stage 2, Фаза 1 — каркас вебхук-рантайма

Ветка `stage2-webhooks`. Ничего не задеплоено, `master` не тронут, текущий polling-бот (`main.py`) жив и не изменён — проверено (`import main` работает как прежде). Postgres не вводился — новый рантайм читает ботов из существующего `db/database.py` (SQLite, токен уже расшифрован внутри).

## Что готово

**Задача 1 — `docs/STAGE2_DESIGN.md`**: целевая схема (один aiohttp-сервер, `POST /webhook/{bot_id}`, реестр в памяти, диспетчер-на-шаблон, middleware конфига). Отмечено: на Railway HTTPS даёт сама платформа, Nginx/SSL не нужны.

**Задача 2 — `runtime/webhook_app.py`**: aiohttp-приложение, `POST /webhook/{bot_id}` + `GET /health`, проверка `X-Telegram-Bot-Api-Secret-Token` против `WEBHOOK_SECRET`, точка входа `python -m runtime.webhook_app` (порт из `PORT`), отдельная от `main.py`.

**Задача 3 — `runtime/registry.py`**: реестр `bot_id -> BotEntry(bot, dispatcher, template_id, config)`, `ConfigMiddleware` кладёт config бота в `data["config"]`, эталонно подключён шаблон `accountant`.

**Задача 4 — `runtime/webhook_setup.py`**: `build_webhook_url()` + `set_webhook_for_bot()`, реальный вызов только за флагом `--apply` (по умолчанию — печать URL, без сети). Проверено вручную: без `--apply` в Telegram ничего не уходит.

**Задача 5 — `tests/test_webhook_routing.py`**: 5 тестов на `unittest.IsolatedAsyncioTestCase` + `aiohttp.test_utils` (без pytest — не хотел добавлять новую зависимость лишний раз, `unittest` хватает). Роутинг к нужному боту, 404 на неизвестный `bot_id`, 403 на неверный секрет, 200 на верный, health-эндпоинт. Все проходят, без реальных токенов/сети.

## Ревью и что починено

Прогнал `review-orchestrator`. Нашёлся один настоящий блокер (не гипотетический):

**🔴 Было: `Router` нельзя подключать к двум `Dispatcher` одновременно.** Изначально `get_template_router()` кэшировал и отдавал ОДИН И ТОТ ЖЕ объект `Router` для всех ботов одного шаблона — в aiogram `Router` можно подключить (`include_router`) ровно один раз за весь lifetime процесса, второй `include_router` того же инстанса кидает `RuntimeError`. Значит **два бота на шаблоне `accountant` гарантированно роняли весь `build_registry()`** при старте — не редкий эдж-кейс, а обычный сценарий. Воспроизвёл локально, починил: `get_template_router()` теперь возвращает **клон** роутера (`_clone_router()` — копирует регистрации хендлеров на свежий `Router` через `observer.handlers`/`FilterObject.callback`, сами callback-функции и фильтры переиспользуются по ссылке, они stateless). Протестировано: два бота на `accountant` теперь строятся без ошибки, с независимыми `Dispatcher`.

**🟡 Починено дополнительно:**
- `build_registry()` — тело цикла обёрнуто в try/except на каждого бота: битый токен/нечитаемый файл больше не роняют весь бутстрап, только пропускают конкретного бота с `logger.exception`.
- `infer_template_id()` — ловит теперь и `UnicodeDecodeError`, не только `OSError`.
- Сравнение `WEBHOOK_SECRET` переведено на `hmac.compare_digest` (было `!=`).
- Явный комментарий-TODO в коде: при незаданном `WEBHOOK_SECRET` проверка сейчас fail-open (пропускается) — осознанно для локального смок-теста без реального вебхука, но **перед любым реальным деплоем это должно стать fail-closed**.

**🟡 Задокументировано как известное ограничение Фазы 1:**
- ~~Шаблон `accountant` — межтенантная утечка (пишет в один SQLite-файл)~~ — **закрыто в Фазе 2**, см. ниже.
- Реестр строится **один раз** при старте процесса (`_bootstrap_app`) — бот, созданный через `/create` уже после старта вебхук-сервера, не попадёт в реестр (webhook вернёт 404) до рестарта процесса. Ожидаемо для каркаса Фазы 1, зафиксировано комментарием в коде.
- `feed_webhook_update` обёрнут в try/except, при падении хендлера пользователя ответ всё равно 200 (осознанно — чтобы Telegram не спамил ретраями). Видимость поломки сейчас — только через `logger.exception`; счётчик/алертинг не заводил, это Фаза 1.

---

# Stage 2, Фаза 2 — перевод `accountant` на `config`

## Что готово

`templates/accountant.py` полностью переписан (все ~30 хендлеров и хелперов) — вместо модульных констант, вычисленных один раз из `Path(__file__).stem`, теперь `@dataclass AccountantConfig` (`bot_name`, `db_path`, `admins_file`, `excel_path`, `html_path`, `welcome_image`, `display_name`, `group_chat_id`), инжектируемый в хендлеры через `data["config"]` (та же механика, что и в Фазе 1). Два конструктора:
- **`config_from_env()`** — standalone/подпроцесс, воспроизводит старую формулу 1-в-1 (проверено тестом), поведение не изменилось.
- **`config_from_bot_row(bot_row, data_dir)`** — вебхук-режим, имя бота берётся из таблицы `bots` (не из имени файла шаблона), `data_dir` **обязательный параметр от вызывающего** — принципиальное решение по итогам ревью дизайна (см. `docs/STAGE2_DESIGN.md`, «Проверка идентичности формул путей»): функция не резолвит `DATA_DIR` из env сама, чтобы не разойтись с каноническим `config.DATA_DIR` фабрики при ином cwd процесса.

`ConfigMiddleware` определена ПРЯМО в `accountant.py` (не в `runtime/`) — шаблон остаётся самодостаточным, без обратной зависимости на проектные модули, как и все `templates/*.py`.

`runtime/registry.py` дополнен `_TEMPLATE_MIDDLEWARE_BUILDERS` (параллельно уже существующему `_TEMPLATE_LOADERS` для роутеров) — для `template_id == "accountant"` `build_entry()` строит типизированный `AccountantConfig` через `templates.accountant.config_from_bot_row(...)`, для остальных (пока не перенесённых) шаблонов — прежний generic dict-based `ConfigMiddleware`.

**⚠️ Осознанно не тронуто (решение владельца, зафиксировано в `docs/STAGE2_DESIGN.md`):** секция `# CUSTOMIZE` (`BOT_DESCRIPTION`, `WELCOME_TEXT`, категории) осталась модульными константами — это текст, который Claude физически переписывает в исходнике при генерации конкретного бота, а не runtime-конфиг; персонального контента для вебхук-ботов взять неоткуда (в таблице `bots` нет таких колонок). Задокументирован явный архитектурный TODO: полное решение — перенос персонального контента в БД/JSON как данные бота, читаемые через `config`, отдельная будущая фаза. Изоляцию **данных** (главный критерий этой фазы) это не нарушает — только контент у вебхук-ботов на `accountant` пока одинаковый.

## Главный критерий фазы — тест изоляции ✅

`tests/test_accountant_isolation.py`: два бота на `accountant`, разный `config` (разные временные каталоги), приводятся в действие **одним и тем же Telegram user_id** (нарочно — худший случай для случайного шаринга состояния). Каждому отправлен апдейт создания проекта (`proj_new` → текст с названием). Результат:

```
test_two_bots_same_user_write_to_separate_db_files ... ok
  → SQLite-файл бота A содержит только "Alpha Project"
  → SQLite-файл бота B содержит только "Beta Project"
test_admin_bootstrap_isolated_per_bot ... ok
  → admins.json бота A: {111}, бота B: {999} — не смешались
```

Плюс smoke-тест standalone-режима (`config_from_env()` даёt прежний путь) и структурный (роутер/`main` на месте). **Итого 5/5 новых тестов + все 5 тестов Фазы 1 (`test_webhook_routing.py`) — 10/10.**

По ходу тест сначала завис: реальные хендлеры (`message.answer()` и т.п.) пытались достучаться до настоящего Telegram API с фейковым токеном. Починил патчем единой точки входа aiogram (`Bot.__call__`) через `unittest.mock.patch.object` в `asyncSetUp`/`asyncTearDown` — реальных сетевых вызовов в тестах нет.

## Ревью

`review-orchestrator` — **0 блокеров**. Отдельно проверено по запросу (все подтверждено):
- Полнота переноса: `grep` на старые имена (`DB_PATH`, `ADMINS_FILE`, `EXCEL_PATH`, `HTML_PATH`, `WELCOME_IMAGE`, `BOT_NAME`) по всему файлу — 0 совпадений, нигде не осталось битых ссылок.
- Баг двойного подключения роутера (Фаза 1) не воспроизведён — тест использует ровно `get_template_router()` (клонирующий), тот же код, что и рантайм.
- `config_from_bot_row()` не шарит объект между ботами — каждый вызов строит свежий `AccountantConfig`.
- Патч `Bot.__call__` — потенциальная утечка между тестами возможна только при параллельном раннере (`pytest-xdist`); при текущем последовательном `unittest discover` — не проблема, зафиксировано как заметка на будущее.
- Риск для Claude-генерации новых ботов на основе этого шаблона — низкий: весь новый код (config, middleware, сигнатуры хендлеров) живёт ВНЕ секции `# CUSTOMIZE`, которую Claude обычно редактирует.

## Как запускать локально
```bash
# главный тест фазы — изоляция данных
python -m unittest tests.test_accountant_isolation -v

# всё вместе (Фаза 1 + Фаза 2)
python -m unittest discover -s tests -v
```

## Как запускать локально

```bash
# тесты (без реальных токенов)
python -m unittest tests.test_webhook_routing -v

# сервер (нужны переменные, как у main.py — BOT_TOKEN/ANTHROPIC_API_KEY/ENCRYPTION_KEY и т.д. из .env)
python -m runtime.webhook_app

# сухой прогон URL вебхука (не стучится в Telegram)
python -m runtime.webhook_setup <bot_id> --base-url https://example.up.railway.app
```

## Что осталось (после Фазы 2)
1. **Перенос остальных шаблонов на `config`** — `accountant` готов (Фаза 2), паттерн эталонный и повторяемый. Остались: `tour_operator`, `trip_manager`, `manager_secretary`, `booking_beauty`.
1а. **Персональный `# CUSTOMIZE`-контент → данные бота** — отдельная будущая фаза (welcome-текст/категории и т.п. переехать из исходника в БД/JSON), см. TODO в `docs/STAGE2_DESIGN.md`.
2. ~~Живое обновление реестра~~ — **готово в Фазе 3**, см. ниже.
3. **Postgres** — отдельная фаза, реестр пока читает SQLite как есть.
4. **Реальное включение вебхуков** — `webhook_setup.py --apply`, `set_webhook` на реальных ботах, деплой на Railway. Ничего из этого не делал — по прямому ограничению задания.
5. **Fail-closed `WEBHOOK_SECRET`** для публичного `/webhook/{bot_id}` перед реальным деплоем (сейчас fail-open, см. выше; admin-эндпоинты Фазы 3 уже fail-closed).

---

# Stage 2, Фаза 3 — живое обновление реестра

## Что готово

`runtime/registry.py`: класс `Registry` — обёртка над `bot_id -> BotEntry` с методами `get()`, `add_or_replace(bot_row)`, `remove(bot_id)`, `reload_one(bot_id)`, `reload_all()`. `build_registry()` теперь строит `Registry` через `reload_all()` вместо возврата разового `dict`.

**Ключевое архитектурное решение (одобрено владельцем):** `get()` — синхронный, без лока. Единичный `dict.get()`/`__setitem__` под GIL уже атомарен; `asyncio.Lock` нужен только чтобы сериализовать сами write-операции между собой (например, два одновременных `reload_all()`), а не блокировать каждое чтение на горячем пути вебхука. Благодаря этому `Registry` — прозрачная drop-in замена старого `dict`: **`webhook_app.py` не пришлось трогать в части чтения вообще**, все 10 тестов Фаз 1-2 прошли без изменений.

**`reload_all()` — проверенный атомарный swap, не `clear()+refill()`.** Владелец отдельно попросил это подтвердить: весь новый `dict` (`new_entries`) строится **целиком вне лока** (полный цикл по `get_all_bots()`, с try/except на каждую битую запись — одна плохая запись не портит остальные), и только ПОСЛЕ этого под локом — ровно одна операция подмены ссылки (`old_entries = self._entries; self._entries = new_entries`). Старые `bot.session` закрываются уже после выхода из-под лока (чтобы сетевой I/O не держал блокировку).

**Новые эндпоинты в `runtime/webhook_app.py`:** `POST /admin/reload/{bot_id}` и `POST /admin/reload-all` — внутренний механизм, Telegram их не вызывает. Защищены тем же секретом (`X-Telegram-Bot-Api-Secret-Token` / `WEBHOOK_SECRET`), но **намеренно fail-closed** — в отличие от публичного `/webhook/{bot_id}` (fail-open при пустом секрете, задокументированный trade-off Фазы 1), если `WEBHOOK_SECRET` не задан, admin-эндпоинты всегда отвечают 403. Это осознанная асимметрия: админ-поверхность может пересобрать любого бота из БД, риск выше, чем у публичного вебхука.

## Тесты (`tests/test_registry_reload.py`, новый файл)

Используют реальную SQLite-таблицу `bots` (через `create_bot_record_with_admins`/`delete_bot`/`set_bot_display_name`), с очисткой в `finally`. 3 теста:

```
test_add_or_replace_makes_a_new_bot_start_routing ... ok
  → бот не в реестре: POST /webhook/<id> → 404
  → add_or_replace(bot_row) → POST /webhook/<id> → 200
  → remove(bot_id) → POST /webhook/<id> → снова 404

test_reload_one_picks_up_changed_bot_row ... ok
  → reload_one → config.display_name = None
  → set_bot_display_name("Renamed Bot") в БД
  → reload_one снова → config.display_name = "Renamed Bot" (не протухший старый)
  → бот удалён из БД → reload_one → None, registry.get() тоже None

test_concurrent_webhook_traffic_survives_reload_all ... ok
  → 2 задачи хаммерят registry.get(bot_id) (по 200 итераций), 2 задачи —
    reload_all() (по 20 раз), всё через asyncio.gather
  → 0 исключений, 0 промахов get() на реально существующем боте
```

Итого **13/13** тестов (10 из Фаз 1-2 + 3 новых).

## Ревью и что починено

`review-orchestrator` — 0 блокеров, 3 важных, все обработаны:
- **Починено:** `add_or_replace()` теперь оборачивает `build_entry()` в try/except (как и `reload_all()`) — битый токен возвращает `None` с логом вместо необработанного исключения (которое раньше дало бы голый 500 на `/admin/reload/{bot_id}`).
- **Починено:** докстринг теста на конкурентность переформулирован — он честно проверяет «не падает и не теряет живых ботов под нагрузкой», а не «отличит атомарный swap от `clear()+refill()`» (в asyncio оба варианта без `await` внутри критической секции недостижимы для интерливинга в одном event loop — реальная польза сборки `new_entries` вне лока — exception-safety, а не защита читателей от пустоты, которая и так гарантирована однопоточностью event loop).
- **Проверено и задокументировано как принятое ограничение:** узкое окно гонки — если `remove()`/`reload`-замена бота происходит РОВНО в момент, когда для этого же бота уже выполняется in-flight `feed_webhook_update` (например, хендлер шлёт ответ пользователю), закрытие `bot.session` может прервать этот КОНКРЕТНЫЙ исходящий вызов. Проверил исходники aiogram (`AiohttpSession.create_session()`): сессия прозрачно пересоздаётся при следующем запросе — это не постоянная поломка, а разовый шанс потерять один ответ в узком временном окне. `webhook_handler` уже оборачивает `feed_webhook_update` в try/except — процесс не падает, максимум одно сообщение не дойдёт. Не хачил защиту от этого сейчас — вероятность крайне низкая, а решение (например, задержанное закрытие сессии) добавило бы сложность, которую фаза просила не вносить.

## Как запускать локально
```bash
# новый набор тестов Фазы 3
python -m unittest tests.test_registry_reload -v

# всё вместе (Фазы 1-3)
python -m unittest discover -s tests -v
```

## Что осталось
- Связать `Registry.reload_one`/`reload_all` с реальным `/create` (фабрика будет сама дёргать `add_or_replace` при создании бота) — отдельный шаг, сознательно не делал в этой фазе.
- Узкое окно гонки с in-flight запросом при удалении/замене бота (см. выше) — принято как есть, не блокер.
- Всё остальное из «Что осталось» Фазы 2 актуально без изменений.
