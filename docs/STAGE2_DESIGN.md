# Stage 2 — вебхук-рантайм: дизайн-заметка (Фаза 1)

## Цель
Заменить модель «один подпроцесс на бота» на мультитенантный вебхук-сервер: один Python-процесс, один `aiohttp`-сервер, все боты — лёгкие объекты в памяти, а не отдельные ОС-процессы.

## Целевая схема

```
                     ┌─────────────────────────────────────────┐
Telegram ──HTTPS──▶  │  aiohttp app (runtime/webhook_app.py)    │
                     │                                           │
                     │  POST /webhook/{bot_id}                  │
                     │    ├─ проверка X-Telegram-Bot-Api-        │
                     │    │  Secret-Token == WEBHOOK_SECRET      │
                     │    ├─ реестр[bot_id] → (Bot, Dispatcher)  │
                     │    └─ dp.feed_webhook_update(bot, update) │
                     │                                           │
                     │  GET  /health  → 200 OK                  │
                     └─────────────────────────────────────────┘
                                    │
                     реестр в памяти: bot_id -> BotEntry
                     BotEntry = { bot: Bot, dispatcher: Dispatcher,
                                   template_id: str, config: dict }
                                    │
                     строится один раз при старте из db/database.py:
                     get_all_bots() → для каждой записи создать Bot(token),
                     подобрать диспетчер по template_id, положить в реестр
```

**Реестр** — обычный `dict[int, BotEntry]` в памяти процесса. Один `Bot`-объект и один `Dispatcher` на каждого бота (Dispatcher — лёгкий, это не подпроцесс). Диспетчеры разных ботов ОДНОГО шаблона переиспользуют один и тот же `Router` с хендлерами шаблона (роутер — просто набор обработчиков, его можно подключать к нескольким `Dispatcher` одновременно) — но НЕ шарят рантайм-конфиг (DB-путь, admin-список и т.п.), это идёт через middleware.

**Диспетчер-на-шаблон.** Для каждого `template_id` (`accountant`, `tour_operator`, ...) есть billed-функция `build_dispatcher_for_template(template_id) -> Router`, которая возвращает роутер шаблона. Разным ботам одного шаблона соответствуют разные `Dispatcher`, но один и тот же `Router`.

**Middleware конфига.** `ConfigMiddleware` кладёт в `data["config"]` конкретного бота (взятый из реестра при регистрации) перед тем, как апдейт дойдёт до хендлеров — это то, через что хендлеры шаблона со временем смогут получать свой `DB_PATH`/admin-список/токены вместо чтения модульных констант. **Важно (см. `STAGE2_REPORT.md`):** сами шаблоны сейчас не читают `data["config"]` — они всё ещё используют модульные константы (`DB_PATH`, `ADMINS_FILE`, вычисленные из `Path(__file__).stem` и `os.getenv`). Механизм прокидывания готов и протестирован; переписать сами шаблоны на чтение из `config` — отдельная, более крупная задача следующей фазы.

## Регистрация вебхука
`https://<railway-domain>/webhook/<bot_id>` — публичный HTTPS-домен уже даёт сама платформа Railway (домен сервиса из коробки, с валидным сертификатом). **Nginx и ручная настройка SSL не нужны** — в отличие от VDS-варианта плана, где это было бы отдельным шагом. Регистрация — вызов Telegram Bot API `setWebhook` с этим URL и секретом; в Фазе 1 это делается только вручную/сухим прогоном (см. `runtime/webhook_setup.py`), реальный вызов к Telegram не выполняется.

## Что НЕ входит в Фазу 1
- Postgres (боты по-прежнему читаются из существующего SQLite через `db/database.py`).
- Перенос всех шаблонов на чтение конфига вместо модульных констант — сделан только частичный, минимально рабочий каркас для `accountant` (см. `STAGE2_REPORT.md`).
- Реальное включение вебхуков на живых ботах, реальные вызовы `setWebhook`, деплой.
- Удаление или изменение подпроцессной модели (`services/bot_runner.py`, `main.py`) — она остаётся рабочей.

---

# Фаза 2 — Config-контракт шаблона `accountant`

## Почему это нужно
В Фазе 1 `ConfigMiddleware` уже кладёт `config` в `data["config"]`, но сам `templates/accountant.py` его не читает — все пути (БД, файл админов, Excel/HTML-экспорт, welcome-картинка) вычисляются ОДИН РАЗ при импорте модуля из `BOT_NAME = Path(__file__).stem`. В подпроцессной модели это работает, потому что каждый сгенерированный бот — это ОТДЕЛЬНАЯ КОПИЯ файла с уникальным именем (`generated_bots/<bot_name>.py`), и `Path(__file__).stem` у каждой копии свой. В вебхук-рантайме мы импортируем ОДИН канонический `templates/accountant.py` (см. `_load_accountant_router()` в `runtime/registry.py`) для ВСЕХ ботов на этом шаблоне сразу — значит `BOT_NAME` у них у всех совпадает, и они пишут в один и тот же `accountant_data.db` / `admins_accountant.json`.

## Инвентаризация: что сейчас берётся из модульных констант

**Различается от бота к боту (переносится в `config`):**

| Константа сейчас | Откуда берётся | Где используется |
|---|---|---|
| `DB_PATH` | `DATA_DIR / f"{BOT_NAME}_data.db"` | во всех хендлерах/хелперах, работающих с БД (`init_db`, `_all_projects`, `_get_active_project`, `_set_active_project`, `_save_tx`, `show_balance`, `show_history`, `cb_tx_view`, `cb_tx_del`, `report_start`, `cb_period`, `cb_cat_filter`, `cmd_excel`, `cmd_html_report`, `_tg_token`, `_publish_project`, `cmd_publish`, `cmd_weblink`) |
| `ADMINS_FILE` | `DATA_DIR / f"admins_{BOT_NAME}.json"` | `_load_admins`, `_save_admins` (используются в `cmd_start`, `cmd_excel`, `cmd_html_report`, `cmd_publish`, `cmd_addadmin`, `cmd_removeadmin`, `cmd_admins`) |
| `EXCEL_PATH` | `DATA_DIR / f"{BOT_NAME}_data.xlsx"` | `cmd_excel` |
| `HTML_PATH` | `DATA_DIR / f"{BOT_NAME}_report.html"` | `cmd_html_report` |
| `WELCOME_IMAGE` | `DATA_DIR / "bot_images" / f"{BOT_NAME}.jpg"` | `cmd_start` |
| `BOT_NAME` (как строка) | `Path(__file__).stem` | `_tg_token()` — `short_name=BOT_NAME[:31]` для Telegraph |

**Формально бот-специфичны, но пока не читаются кодом accountant.py** (добавляю в `config` для единообразия с общей формой реестра, задел на будущее — сейчас нигде не используются внутри шаблона):
- `display_name`, `group_chat_id` — уже кладутся в общий `config`-словарь реестром (Фаза 1), но сам accountant.py их никогда не читал даже в подпроцессной модели.

**НЕ переносится — общее для всех, не зависит от конкретного бота:**
- `TELEGRAPH_API` — фиксированный внешний URL.
- `MAIN_BUTTONS` — статичная UI-константа.
- `DATA_DIR` — общий для всей фабрики каталог (задаётся `config.py`/env один раз), меняется не пер-бот, а пер-инсталляцию; входит в вычисление путей выше, но сам по себе в `config` не переезжает.

## ⚠️ Не переносится чисто — секция `# CUSTOMIZE` (нужно решение владельца)
`BOT_DESCRIPTION`, `WELCOME_TEXT`, `EXPENSE_CATEGORIES`, `INCOME_CATEGORIES` — по замыслу шаблона это не про-инстанс runtime-конфиг, а место, которое **Claude физически переписывает в тексте исходника** при генерации конкретного бота (маркер `# CUSTOMIZE: sections marked with # CUSTOMIZE`). У каждого сгенерированного бота — свой текст приветствия/категорий, буквально прописанный в его копии файла.

В вебхук-рантайме мы грузим ОДИН канонический `templates/accountant.py`, а не персональную копию каждого бота — значит любая кастомизация этих полей, которую Claude сделал под конкретного клиента, **сейчас не подтянется через `config`**, потому что её негде взять: в таблице `bots` нет колонок для welcome-текста/категорий, только свободный `description` (промпт для генерации, не готовый пользовательский текст).

**Не хачу это вслепую.** Варианта два, оба за рамки этой фазы:
1. Оставить `BOT_DESCRIPTION`/`WELCOME_TEXT`/категории как есть (модульные константы шаблона) — тогда ВСЕ боты на `accountant` через вебхуки получат ОДИНАКОВЫЙ welcome-текст/категории (дефолтные, из канонического файла), без персональной кастомизации. Это не ломает изоляцию ДАННЫХ (главный критерий этой фазы), только контент.
2. Завести в таблице `bots` колонки под кастомизацию (или JSON-блок) и научить registry.py собирать `config` из них — отдельная, более крупная задача (меняет схему БД, затрагивает `handlers/create_bot.py`).

**Предлагаю на этой фазе — вариант 1** (оставить как модульные константы, не блокирует изоляцию данных), явно задокументировав ограничение. Задачи 2–5 идут с этим допущением.

**TODO (архитектурный, не эта фаза, утверждено владельцем):** это упирается в суть Stage 2 — «данные отдельно от логики, персонализация через `config`, а не через переписывание кода». По-настоящему решится, когда персональный контент (`welcome_text`, категории и т.п.) переедет в БД/JSON как ДАННЫЕ бота, а шаблон будет читать его оттуда через `config`, как остальные поля. Требует: колонки/JSON-блок в таблице `bots`, изменения в `handlers/create_bot.py` (сохранять кастомизацию отдельно от кода при генерации), и соответствующее поле в `AccountantConfig`. Отдельная будущая фаза.

## ✅ Проверка идентичности формул путей (standalone vs webhook)

Владелец справедливо потребовал явно сверить, что `config_from_env()` (standalone) и `config_from_bot_row()` (webhook) для ОДНОГО И ТОГО ЖЕ бота дают идентичные пути — иначе вебхук-режим тихо создаст пустой файл вместо того, чтобы найти существующие данные бота.

**Имя бота — подтверждено идентично.** `handlers/create_bot.py:469,563`: `bot_file = GENERATED_BOTS_DIR / f"{bot_name}.py"` и `create_bot_record_with_admins(name=bot_name, ...)` — это буквально ОДНА И ТА ЖЕ переменная `bot_name` в обоих местах, без каких-либо преобразований между ними. Значит `Path(__file__).stem` сгенерированного файла бота **всегда равен** `bots.name` в БД для этого бота — расхождения в имени быть не может.

**`DATA_DIR` — нашёл реальный риск, чиню в дизайне.** Стандартный fallback у обеих сторон выглядит одинаково по тексту (`os.getenv("DATA_DIR", "./data")`), но это ОБМАНЧИВО:
- `config.py` (факторный процесс и весь вебхук-рантайм, т.к. `runtime/registry.py` → `db.database` → `config.py`) резолвит `DATA_DIR` в **абсолютный** путь, если переменная не задана: `Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))` — привязан к расположению `config.py` (корень репозитория), не зависит от текущей рабочей директории процесса. Это намеренный фикс из Stage 1.
- Если бы `templates/accountant.py`'s `config_from_bot_row()` самостоятельно вызывал `os.getenv("DATA_DIR", "./data")` — **относительный** путь резолвился бы от cwd вебхук-процесса на момент операции с файлом, что при иной рабочей директории (не корень репозитория) даст **другой физический каталог**, чем у факторного процесса.

Сегодня оба пути совпадают только "случайно" — потому что подпроцессы наследуют cwd родителя (= корень репозитория по соглашению деплоя), и потому что на Railway `DATA_DIR` сейчас явно задан как переменная окружения (расхождения не возникает). Но это **не гарантия**, а совпадение по конвенции — ровно тот класс ошибки, о котором предупреждал владелец: если `DATA_DIR` не задан явно (а Stage 1 Round 1 явно РЕКОМЕНДОВАЛ его не задавать, полагаясь на безопасный дефолт `config.py`) и вебхук-процесс однажды запустится не из корня репо — пути разойдутся молча.

**Решение:** `config_from_bot_row()` НЕ вычисляет `DATA_DIR` сама — принимает его явным параметром (`data_dir: Path`) от вызывающего кода. `runtime/registry.py` (которому, в отличие от шаблонов, ПОЗВОЛЕНО зависеть от проектных модулей) передаёт туда канонический `config.DATA_DIR` — тот же самый объект, который уже используют `main.py` и все подпроцессы. Это даёт железную гарантию совпадения путей независимо от cwd вебхук-процесса, не нарушая при этом самодостаточность `accountant.py` (файл по-прежнему не импортирует ничего из проекта — просто принимает путь параметром). `config_from_env()` (standalone) остаётся с прежним самостоятельным `os.getenv("DATA_DIR", "./data")` — это ТОЧНО повторяет текущее поведение подпроцесса, менять не нужно (и нельзя — обязаны сохранить 1-в-1 совместимость).

## Форма `config`

```python
@dataclass
class AccountantConfig:
    bot_name: str                  # для Telegraph short_name и т.п.
    db_path: str
    admins_file: Path
    excel_path: str
    html_path: str
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
```

Типы полей сохраняют то, что использовалось раньше (`DB_PATH`/`EXCEL_PATH`/`HTML_PATH` были `str`, `ADMINS_FILE`/`WELCOME_IMAGE` — `Path`) — поведение хендлеров не меняется, меняется только источник значения.

## Как `config` попадёт в хендлеры — оба режима, одна форма

**Standalone (`main()`):**
```python
async def main():
    config = config_from_env()          # НОВОЕ — та же логика, что раньше была в модульных константах
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))   # НОВОЕ
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    await dp.start_polling(bot)
```
`config_from_env()` — использует `Path(__file__).stem` и `os.getenv("DATA_DIR", ...)`, т.е. даёт **байт-в-байт то же самое**, что сейчас дают модульные константы. Поведение standalone-режима не меняется.

**Webhook (`runtime/registry.py`):** вместо общей `ConfigMiddleware` с сырым словарём, для `template_id == "accountant"` реестр строит типизированный `AccountantConfig` через `config_from_bot_row(bot_row)` — новую функцию в САМОМ `accountant.py` (не в `runtime/`, чтобы шаблон оставался самодостаточным, единый файл, без обратной зависимости на `runtime/`). `bot_row["name"]` (реальное имя бота из таблицы `bots`) занимает место `Path(__file__).stem` — так пути получаются per-bot, но по тому же принципу именования, что уже используется в подпроцессной модели (`generated_bots/<name>.py` → `<name>_data.db`) — **если у бота уже есть данные от старого подпроцессного запуска, вебхук-режим найдёт тот же файл, не создаст пустой новый.**

`ConfigMiddleware` — маленький класс (`BaseMiddleware`, кладёт `config` в `data["config"]`) определяется ПРЯМО В `accountant.py`, не импортируется из `runtime/` — сохраняет инвариант «шаблон ничего не импортирует из проекта, только сторонние библиотеки», который сейчас верен для всех `templates/*.py`. `runtime/registry.py` дёргает `templates.accountant.config_from_bot_row(...)` и `templates.accountant.ConfigMiddleware(...)` тем же ленивым паттерном, что уже используется для роутера (`_TEMPLATE_LOADERS`), плюс параллельный реестр `_TEMPLATE_MIDDLEWARE_BUILDERS` для конфиг-мидлвари по `template_id`.

## Как хендлеры/хелперы получают значения
- Хендлеры aiogram получают `config: AccountantConfig` через инжекцию по имени параметра (аналогично `state: FSMContext`, `bot: Bot`) — механизм уже проверен в Фазе 1.
- Внутренние хелперы (`_all_projects`, `_get_active_project`, `_set_active_project`, `_save_tx`, `init_db`, `_tg_token`, `_publish_project`, `_load_admins`, `_save_admins`) сейчас замыкаются на модульные `DB_PATH`/`ADMINS_FILE` — переводятся на явный параметр (`db_path: str`, `admins_file: Path`), который им передаёт вызывающий хендлер из своего `config`. Не весь `AccountantConfig` целиком — только то поле, которое реально нужно хелперу (более узкая связанность, легче тестировать).

---

# Фаза 4 — Config-контракт шаблона `manager_secretary` (второй эталон)

Структурно почти идентичен `accountant.py` — тот же паттерн, без изобретения нового.

## Инвентаризация

**Переезжает в `config` (различается от бота к боту):**

| Константа сейчас | Откуда берётся | Где используется |
|---|---|---|
| `DB_PATH` | `DATA_DIR / f"{BOT_NAME}_data.db"` | `init_db`, `_seed_faqs`, `kb_faqs`, `cb_faq`, `_save_lead`, `admin_leads`, `cb_lead_status`, `admin_stats`, `cmd_addfaq`, `cmd_listfaq`, `cmd_delfaq`, `handle_group_mention` |
| `ADMINS_FILE` | `DATA_DIR / f"admins_{BOT_NAME}.json"` | `_load_admins`/`_save_admins` (используются в `cmd_start`, `_save_lead`, `admin_leads`, `admin_stats`, `cmd_addfaq/listfaq/delfaq`, `cmd_addadmin/removeadmin/admins`) |
| `WELCOME_IMAGE` | `DATA_DIR / "bot_images" / f"{BOT_NAME}.jpg"` | `cmd_start` |
| `BOT_NAME` (как строка) | `Path(__file__).stem` | нигде за пределами построения путей выше (в отличие от `accountant`, где ещё шёл в Telegraph `short_name`) — просто оставляем поле `bot_name` в конфиге для единообразия с `AccountantConfig`, реально не используется хендлерами |

**Отличие от `accountant` — реально потребляемое поле.** `BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "").strip()` в `handle_group_mention` (group-упоминания → ответ через Claude) — это ЕДИНСТВЕННОЕ отличие от `accountant`, где `display_name`/`group_chat_id` лежали в конфиге, но не читались нигде. Здесь `display_name` — не мёртвое поле, а активно используется. Маппится напрямую на уже существующее поле `config.display_name` (та же generic-форма, что и в реестре `_config_from_row`) — переносится 1-в-1, без новых полей.

**НЕ переезжает — общее для всех:**
- `ANTHROPIC_API_KEY` (`os.getenv("ANTHROPIC_API_KEY", "")` в `handle_group_mention`) — общий ключ фабрики, не бот-специфичен, как `TELEGRAPH_API` в `accountant`.
- `group_chat_id` — как и в `accountant`, поле присутствует в общей форме конфига, но самим шаблоном не читается (нет логики, завязанной на конкретный group id).

**`# CUSTOMIZE`-контент (тот же TODO, что в `accountant`):** `BOT_DESCRIPTION`, `WELCOME_TEXT`, `ADMIN_NEW_LEAD`, `FAQS` (используется для сида таблицы `faqs` при первой инициализации БД конкретного бота) — остаются модульными константами, тот же принятый на Фазе 2 компромисс.

## Проверка идентичности формул путей
Тот же аргумент, что и для `accountant` (см. выше): `bot_name` в `handlers/create_bot.py` — одна переменная и для имени файла, и для `bots.name`, расхождения по имени быть не может. `DATA_DIR` — та же поправка: `config_from_bot_row(bot_row, data_dir)` принимает `data_dir` параметром от вызывающего (`runtime/registry.py`, канонический `config.DATA_DIR`), не резолвит `os.getenv("DATA_DIR")` сама — идентично решению для `accountant`.

## Форма `config`

```python
@dataclass
class ManagerSecretaryConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None   # реально используется в handle_group_mention
    group_chat_id: str | None = None  # не используется шаблоном, как и в accountant
```

Нет `excel_path`/`html_path` — у этого шаблона нет Excel/HTML-экспорта, поля просто не нужны (не копирую лишнее из `AccountantConfig`).

`config_from_env()` / `config_from_bot_row()` / `ConfigMiddleware` — тот же паттерн, что и в `accountant.py`, определены в самом файле шаблона.
