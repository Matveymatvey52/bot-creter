# Office-хук для from-scratch (не-шаблонных) ботов — инвентаризация и дизайн

## 1. Точная структура текущего разрыва (факты из кода)

### 1.1 Генерация кода: `services/claude_service.py`

`_generate_bot_code_inner()` (services/claude_service.py:1569) пробует шаблоны в порядке убывания уверенности:

- **1 совпавший шаблон** → `_customize_from_template()`. Результат — модифицированная копия `templates/<id>.py`, **сохраняет `# TEMPLATE: <id>` маркер** (customize-промпт не трогает первые строки файла) и модуль-уровневые `router`/`config_from_bot_row`/`init_db`, потому что стартует с них.
- **2 совпавших шаблона** → `_synthesize_from_templates()` — то же самое, маркер и by-convention экспорты сохраняются (иначе синтез не смог бы объединить роутеры).
- **0 или >2 совпадений** → падает в `GENERATE_SYSTEM_PROMPT`-ветку (claude_service.py:1589-1638): чистый from-scratch код с нуля. Промпт не требует `# TEMPLATE:` маркера и не требует module-level `router`/`config_from_bot_row`/`init_db` — единственное жёсткое требование в этой ветке — `asyncio.run(main())` как entry point (проверяется на claude_service.py:1620, с retry-раундом на до-генерацию, если код обрезан лимитом токенов).

`office_hook_config` **генерируется всегда**, независимо от ветки (claude_service.py:1541, `_generate_office_hook_config` вызывается конкурентно с miniapp_config через `asyncio.gather`). Это дешёвый Haiku-вызов, который не проверяет, есть ли у бота `router` — он просто анализирует текст кода и требования. Конфиг сохраняется в БД (`bot_office_hook_config` таблица) **независимо от того, будет ли он когда-либо использован**.

Итог: сегодня уже возможна ситуация «office_hook_config существует в БД, но бот физически не может его получить» — именно это и описывает комментарий в `build_entry()`.

### 1.2 Подключение хука: `runtime/registry.py` `build_entry()`

`build_entry()` (registry.py:581-698) резолвит бота в модуль так:

```
template_id = маркер "# TEMPLATE: <id>" из первых строк файла бота (registry.py:41-54, _TEMPLATE_MARKER_RE)
module = await _load_template_module_async(template_id) if template_id else None
```

Далее всё построено by-convention **вокруг существования `module`**:

- `module.router` → клонируется и монтируется в `Dispatcher` (registry.py:624-641). Без этого атрибута бот не отвечает ни на одно сообщение (явный WARNING в логах, registry.py:632-637).
- `module.config_from_bot_row` + `module.init_db` → дают `typed_config` с реальным `db_path` (registry.py:618-619, `_build_generic_middleware`).
- Office-хук (registry.py:667-696) — **два уровня fallback, оба требуют `module is not None`**:
  1. `module.on_office_event` есть → используется напрямую (ручной хук шаблона).
  2. `module.on_office_event` нет, но `typed_config is not None` (значит модуль резолвился и дал db_path через `config_from_bot_row`) → универсальный `generic_on_office_event()` из `features/office_events.py`, управляемый строкой `bot_office_hook_config` из БД.
  3. **`module is None`** (from-scratch бот без `# TEMPLATE:` маркера) → **ни одна из веток не выполняется**. Блок `if module is not None:` на registry.py:667 просто не входит — нет `else`, нет третьего fallback-пути.

Причина разрыва — не забытая строка кода, а структурная: universal-fallback (ветка 2) получает `db_path` **из `typed_config`**, а `typed_config` в принципе не существует без резолва в `templates/*.py` модуль (`_build_generic_middleware` берёт `db_path` из `module.config_from_bot_row(bot_row, DATA_DIR)` — вызов метода на модуле). У from-scratch бота нет отдельного `.py`-модуля в `templates/`, который можно было бы импортировать этим путём — весь код бота живёт в одном сгенерированном файле.

Иными словами: **весь механизм построен вокруг импортируемого модуля с двумя специфическими атрибутами (`router`, `config_from_bot_row`)**, а from-scratch боты по конструкции этого не имеют.

## 2. Варианты решения

### Вариант A — обязать генератор экспортировать `router`/`config`-конвенцию

Изменить `GENERATE_SYSTEM_PROMPT` так, чтобы from-scratch код обязательно:
1. Экспортировал module-level `router = Router()`, `config_from_bot_row()`, `init_db()`, опционально `on_office_event()` — та же форма, что и `templates/*.py`.
2. Получал `# TEMPLATE: <synthetic-id>` маркер и сохранялся так, чтобы реестр мог импортировать его тем же путём, что и обычные шаблоны.
3. AST-проверка на выходе (аналог существующей `_ast.parse` + retry-на-`asyncio.run(main())` в claude_service.py:1603-1632): парсить сгенерированный код, проверять наличие module-level `router`-присвоения; при отсутствии — просить Claude дополнить/переписать.

**Плюсы:** единый механизм для всех типов ботов, `build_entry()` не меняется вообще — from-scratch боты перестают быть особым случаем.

**Минусы:** ломает базовое предположение from-scratch ветки — что это произвольный однофайловый скрипт. Более инвазивное изменение и без того длинного промпта — риск деградации качества генерации в остальных аспектах. Неясно (см. §3), выполняются ли from-scratch боты сегодня вообще через `build_entry()` — если нет, это изменение не решает проблему само по себе.

### Вариант B — прямая инъекция office-хук кода в сгенерированный текст

Не менять промпт генерации. После получения `code` от `_generate_bot_code_inner()`, если ветка была from-scratch и `office_hook_config` не `None` — программно (не через LLM) вставить блок вида:
```python
async def on_office_event(event, config):
    from features.office_events import generic_on_office_event
    await generic_on_office_event(event, config["db_path"], HOOK_CONFIG, bot_id=config["bot_id"])
```
перед `asyncio.run(main())`.

Это не решает главную проблему само по себе: `build_entry()` не видит from-scratch бота как модуль (`module is None`), значит инъекция кода в текст файла ничего не подключает, пока `build_entry()` не научится импортировать from-scratch файлы напрямую (`importlib.util.spec_from_file_location` по `file_path`, а не по `template_id`-неймспейсу) — то есть B неизбежно тянет почти тот же объём изменений в `registry.py`, что и A, просто без изменения промпта генерации и без требования полной router-конвенции (более узкое требование: только `on_office_event(event, config)` с фиксированной сигнатурой).

Альтернатива внутри B — не трогать `build_entry()` вообще, а доставлять событие иным транспортом (например HTTP-вызов на локальный порт standalone-процесса), если from-scratch боты сегодня выполняются вне общего вебхук-диспетчера реестра. Требует сначала выяснить, как they вообще запускаются — см. §3.

## 3. Риски и открытый вопрос о существующих ботах

- **Не удалось подтвердить из этой рабочей копии, сколько from-scratch ботов уже есть в проде.** Локальная `data/bots.db` (тестовая среда) пуста — 0 строк в таблице `bots`, и в её схеме вообще нет колонки `template_id` (он резолвится каждый раз заново по маркеру в файле бота, не хранится). В проде нужно проверить: (а) файлы ботов на диске без `# TEMPLATE:`-маркера, (б) наличие WARNING-строк `build_entry: bot_id=... has template_id=... that does not match` / `module has no 'router' attribute` в Deploy Logs — их появление доказывает, что такие боты уже проходят через `build_entry()` сегодня.
- Если такие WARNING в проде есть — значит from-scratch боты **уже** регистрируются через `build_entry()` с `module=None` и отвечают ли они вообще на апдейты (без router) — открытый вопрос, требующий отдельной проверки логов/поведения. Если WARNING нет — вероятно from-scratch боты сегодня запускаются иначе (отдельный standalone-процесс вне реестра), и весь фрейминг задачи меняется: хук нужно подключать не в `build_entry()`, а в их отдельном процессе-жизненном-цикле.
- Ни A, ни B не ломают существующие from-scratch боты автоматически — изменение применяется только к новой генерации/новому импорту. Но и не восстанавливают им office-хук задним числом: существующий файл на диске не содержит того, что требует новый путь. Нужна либо регенерация файла (риск: перезапись ручных правок владельца, если такие были), либо разовый скрипт, который программно допишет `on_office_event` в существующие файлы, используя уже сохранённый `office_hook_config` из БД.

## 4. Решение и реализация

Выбран **гибридный вариант B**, но легче, чем описано в §2: не полная router-конвенция (вариант A), и не отдельный код-транспорт события (крайний B) — программная (не LLM) дописка тех же четырёх экспортов, что даёт template, прямо в сгенерированный файл, плюс прямой file-based импорт в `build_entry()`.

Ответы на открытые вопросы §3:

1. **A vs B** → B. Полная router-конвенция (A) избыточна: `generic_on_office_event()` не требует `router` вообще, только `db_path`+`bot_id`+`hook_config`. Требовать от LLM писать `router`/`config_from_bot_row`/`init_db` руками — риск галлюцинаций на каждый бот; вместо этого `services/claude_service.py`'s `append_from_scratch_registry_wiring()` дописывает их ДЕТЕРМИНИРОВАННО (regex + AST-проверка, не второй LLM-вызов) поверх уже сгенерированного `DB_PATH`/`BOT_NAME`/`init_db(db_path)`, которые промпт и так требует.
2. **Как запускаются** → подтверждено: через `build_entry()`/реестр. `add_or_replace()`/`reload_all()` вызывают `infer_template_id(bot_row["file_path"])` безусловно для ЛЮБОГО бота, включая from-scratch — WARNING на registry.py (тогдашние строки 610/632) для них не стрелял только потому, что `template_id=None` не входит в тот `if template_id and module is None` guard, а `module is None` тихо резолвился в "нет router" без лога. Значит from-scratch боты уже проходили через `build_entry()`, просто без office-хука и (что важнее) без вообще какой-либо связи с реестровым `config`/`db_path`.
3. **Миграция существующих ботов** → НЕ делалась в этой итерации. Новый путь применяется только к вновь сгенерированному/перегенерированному коду (`_generate_bot_code_inner`'s from-scratch ветка, плюс re-apply в `handlers/manage_bots.py`'s `cb_recreate`/`cb_auto_diagnose`/`_apply_fix` после `improve_bot_code`/`fix_bot_code`, которые иначе тихо стирали дописанный блок при "Улучшить"/"Исправить"). Существующие from-scratch файлы на диске без блока wiring продолжат резолвиться как `module is None` до следующего regenerate/improve/fix — миграционный скрипт не писался (нет данных о том, сколько таких ботов есть в проде и активны ли они, см. бывший §3 п.3 этого документа).

### Реализация (services/claude_service.py, runtime/registry.py, handlers/manage_bots.py)

- `append_from_scratch_registry_wiring(code)` — дописывает `config_from_bot_row`/`ConfigMiddleware`/`on_office_event` (плюс no-op `init_db` fallback, если бот его не определил) перед `if __name__ == "__main__":`. AST-валидация после инъекции; при синтаксической ошибке — no-op, возвращает исходный код нетронутым. Идемпотентна (проверка `"def config_from_bot_row(" not in code`), безопасна для повторного вызова на template-based файлах (уже содержат этот экспорт → no-op).
- `_generate_bot_code_inner()`'s from-scratch ветка вызывает её последней, после всех LLM-ревью проходов — ревью не знают об этом блоке и могли бы его "исправить"/выбросить, если бы видели его раньше.
- `runtime/registry.py`'s `build_entry()` получил новый опциональный параметр `file_path`; когда `template_id` не резолвится в module (в т.ч. когда `template_id is None`), пробует `_load_generated_bot_module_async(bot_id, file_path)` — прямой `importlib.util.spec_from_file_location` импорт файла из `data/generated_bots/`, не через `templates.<id>` namespace. Деликатно НЕкэшируемый (в отличие от template-модулей) — `cb_recreate`/`cb_auto_diagnose` перезаписывают тот же `file_path` на месте, кэш по имени файла отдавал бы устаревший код до рестарта процесса.
- Дальше — тот же код `build_entry()`, что и для template-based ботов: `module is not None` → `_build_generic_middleware` даёt `typed_config`, `on_office_event` находится через `getattr` и подключается тем же путём.
- `handlers/manage_bots.py`'s `cb_recreate`/`cb_auto_diagnose`/`_apply_fix` вызывают `append_from_scratch_registry_wiring()` повторно перед записью файла на диск — без этого "Улучшить"/"Исправить" молча стирали бы дописанный блок при каждом вызове (найдено на ревью).

Тесты: `tests/test_from_scratch_office_hook.py` — сквозной сценарий (from-scratch бот на диске → `build_entry()` с `template_id=None`+`file_path` → office-событие → generic-хук пишет в `office_notes` в СОБСТВЕННОМ db_path бота), плюс изоляция `append_from_scratch_registry_wiring` (идемпотентность, no-op на сломанном коде, no-op без entry point, fallback `init_db` для stateless бота).
