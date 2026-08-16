# Дизайн: самопополнение библиотеки шаблонов (Этап 5) — логирование кандидатов

Статус: **ЧЕРНОВИК, СТОП перед реализацией.** Ждёт решения владельца по двум пунктам в конце.

## 1. Текущий пайплайн (как есть)

`services/claude_service.py`:

- `_select_template(summary)` (:1330) — Haiku-классификатор, возвращает 0/1/2 имени шаблона из `templates/*.py`. Любой мусорный/невалидный ответ → `[]`.
- `_generate_bot_code_inner(requirements_summary)` (:1569) — оркестратор:
  - `len(templates) == 1` → `_customize_from_template()` (:1364, Sonnet, правит только `# CUSTOMIZE` блок).
  - `len(templates) == 2` → `_synthesize_from_templates()` (:1385, Sonnet, сливает два шаблона в один).
  - **`len(templates) == 0`** (или ветки выше не дали `asyncio.run(main())` в коде) → **from-scratch генерация** (:1589 и ниже): `classify_bot_type()` + прямой Sonnet-вызов с `GENERATE_SYSTEM_PROMPT`.
- `generate_bot_code()` (:1535) — публичная обёртка, вызывает `_generate_bot_code_inner`, затем параллельно генерирует `miniapp_config` и `office_hook_config`.

Вызывается из `handlers/create_bot.py:545` (`_run_generation`):
```python
code, miniapp_config, office_hook_config = await asyncio.wait_for(generate_bot_code(summary), timeout=360.0)
```
На этом этапе доступны только `summary` и `bot_name` (из FSM-состояния диалога создания). **`bot_id` ещё не существует** — запись в БД (`create_bot_record_with_admins`) создаётся позже, после генерации кода и создания самого Telegram-бота (~:717 и ~:797). Так что зацепиться за "какой fallback сработал" нужно **внутри `_generate_bot_code_inner`**, а не у вызывающей стороны — там нет `bot_id` в момент, когда решение "template vs synthesis vs from-scratch" уже принято.

**Ключевой сигнал "подходящего шаблона нет"**: `len(templates) == 0` после `_select_template()`. Это единственная точка, где однозначно известно "совпадения не нашлось, идём from-scratch". (Есть ещё вырожденный случай — 1 или 2 шаблона выбраны, но `_customize_from_template`/`_synthesize_from_templates` не дали валидный код с `asyncio.run(main())`, и код проваливается в тот же from-scratch блок ниже. Это два разных по смыслу случая: первый — "шаблона нет", второй — "шаблон был, но кастомизация не смогла" — для кандидатов имеет смысл логировать оба, но с разным `fallback_reason`.)

## 2. Механизм логирования кандидатов

### Где хранить
Новая factory-level таблица в `db/database.py::init_db()`, тем же паттерном, что и существующие (`bot_feedback` и т.п.) — `CREATE TABLE IF NOT EXISTS` внутри общего блока, без индексов (по аналогии).

```sql
CREATE TABLE IF NOT EXISTS template_candidates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id         INTEGER REFERENCES bots(id),   -- NULL допустим: см. проблему с таймингом ниже
    creator_user_id INTEGER NOT NULL,
    bot_name       TEXT,
    summary        TEXT NOT NULL,                  -- requirements_summary как есть
    fallback_reason TEXT NOT NULL,                  -- 'no_template_match' | 'customize_failed' | 'synthesis_failed'
    selected_templates TEXT,                        -- JSON-список того, что _select_template вернул (может быть пусто или 1-2 имени при customize_failed)
    bot_type       TEXT,                             -- результат classify_bot_type(), если уже посчитан
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

**Проблема с `bot_id`**: как выяснено в п.1, на момент генерации кода `bot_id` ещё не существует. Варианты:
- (a) писать кандидата с `bot_id = NULL` в момент генерации (внутри `_generate_bot_code_inner`, доступен `summary`, но не `creator_user_id`/`bot_name` без явной передачи параметров) — потребует прокинуть `creator_user_id`/`bot_name` в `generate_bot_code()`/`_generate_bot_code_inner()` как новые параметры;
- (b) писать кандидата из `handlers/create_bot.py` после генерации, где уже есть `bot_name` и `creator_user_id` (`message.from_user.id`/`chat_id`) — но тогда `services/claude_service.py` должен как-то сообщить наружу, что сработал fallback (сейчас `generate_bot_code()` возвращает только `(code, miniapp_config, office_hook_config)` — нет сигнала о ветке). Потребует либо добавить 4-й элемент в возвращаемый tuple (`fallback_reason: str | None`), либо завести module-level логирование внутри `claude_service.py` напрямую в БД.

Рекомендация (не финал, на решение владельца): **(b) с расширением tuple** — `generate_bot_code()` возвращает `(code, miniapp_config, office_hook_config, fallback_info)`, где `fallback_info` — `dict | None` с `reason` и `selected_templates`. Тогда запись в БД делает `create_bot.py` уже с реальным `bot_id`, если к этому моменту он появился (можно писать кандидата на втором проходе — сразу после `create_bot_record_with_admins`, если `fallback_info` не `None`). Это держит `claude_service.py` чистым от прямых обращений к БД (сейчас он их не делает вообще — хороший прецедент, стоит сохранить).

### Что логировать
- `summary` — сырой текст требований клиента (для будущего NLP-анализа паттернов).
- `fallback_reason` — чтобы отличать "нет подходящего шаблона вообще" от "шаблон подошёл, но кастомизация не смогла завершиться".
- `selected_templates` — даже пустой список полезен (модель вообще не увидела совпадений vs увидела, но недостаточно уверенно).
- `bot_type` — уже вычисляется (`classify_bot_type`) в этой же ветке, почти бесплатно приложить для группировки.
- НЕ логировать сам сгенерированный код — он и так хранится в самом боте; для кандидатов важен только запрос, не результат.

## 3. Механизм анализа накопленного

Учитывая, что `/analytics` уже существует и представляет собой мини-апп (React SPA поверх `runtime/factory_analytics_api.py`, JSON API, не текстовые сообщения) — **органичнее добавить кандидатов как новую секцию/вкладку в этот существующий дашборд**, а не заводить отдельную команду `/candidates`. Это соответствует уже принятому UX-паттерну (владелец открывает один мини-апп для всей аналитики фабрики) и не плодит утилитарные текстовые команды в чате.

Предлагаемая секция дашборда "Кандидаты на новый шаблон":
- Новый REST-эндпоинт в `runtime/factory_analytics_api.py`, тем же `_authenticate_owner()`, что у остальных.
- Группировка накопленных `template_candidates` по кластерам похожих `summary` — для MVP можно обойтись без ML: группировка по `bot_type` + простой подсчёт частых слов/фраз в `summary`, либо (проще и честнее для MVP) просто топ-N самых свежих записей с сырым текстом — пусть владелец сам увидит паттерн глазами, не нужно изобретать кластеризацию сразу.
- Вывод: список записей с `summary`, `fallback_reason`, `created_at`, счётчик "сколько раз похожее" (если решим делать группировку) — и явная пометка "не подходит ни один шаблон N раз за месяц" как сигнал.

Финальное решение "добавить как постоянный шаблон" — **всегда вручную**, через существующий процесс ручного ТЗ разработчику (как сейчас). Автоматического создания `templates/*.py` из кандидатов не предлагается и не должно быть.

## СТОП — нужны решения владельца

1. **Хранение**: подходит ли вариант (b) — расширить возвращаемый tuple `generate_bot_code()` четвёртым элементом `fallback_info`, запись в БД делает `create_bot.py` — или предпочтителен вариант (a) (писать прямо из `claude_service.py`, потребует передать туда `creator_user_id`/`bot_name`)?
2. **Отображение**: встроить как новую секцию/вкладку в существующий `/analytics` мини-апп (рекомендация выше), или всё же отдельная команда `/candidates` в чате Creator-бота с текстовым выводом?

До получения ответов реализация не начинается.
