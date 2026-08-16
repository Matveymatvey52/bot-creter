# Критические алерты владельцу через office_events

**Статус:** реализовано (v1, только `unhandled_exception`/`webhook_failure`), полный сьют (1114 тестов) зелёный, узкий ревью-проход пройден и находки исправлены. Ветка/worktree: `design-critical-alerts` / `/Users/matvej/bot-creter-critical-alerts-design`. Ждёт коммита — см. §6.

## 0. Что реализовано (по ответам владельца на §"Стоп" ниже)

- `runtime/registry.py`: `register_critical_error_handler(dp, bot_id)` — регистрирует aiogram `dp.errors()`, вызывается для каждого Dispatcher (и тенантов в `build_entry()`, и фабричного в `combined_app.py`'s `_build_factory_dispatcher()`).
- `runtime/webhook_app.py`: except-блок вокруг `feed_webhook_update()` теперь дополнительно зовёт `report_critical_error(bot_id, "webhook_failure", exc)` — покрывает то, что не поймано aiogram-уровнем.
- `features/office_events.py`: `report_critical_error()` — безусловная доставка в `FACTORY_BOT_ID` (в обход `bot_office_links`), с:
  - rate-limit по `(bot_id, category, полный_очищенный_текст)`, окно 300с, in-memory, cap на 2000 ключей;
  - двухслойной очисткой секретов: (1) литеральная замена известных секретов процесса из `config.py` (BOT_TOKEN, ANTHROPIC_API_KEY и т.д.), (2) паттерны по форме (Telegram-токен, Bearer, URL-креды, `key=`/`secret=`-подобные);
  - graceful degradation на каждом шаге (нет реестра / нет фабричного бота / `OWNER_ID` не задан / `send_message` упал) — логирует и не поднимает исключение дальше.
- v1 сознательно НЕ включает: квота/rate-limit трекинг внешних API (Sheets/Telegram 429/Claude) — отдельная задача.

## 1. Проблема

Владелец узнаёт о падениях/ошибках ботов только случайно. Систематического уведомления нет.

Факт: `runtime/webhook_app.py:65-68` — единственное место, где необработанное исключение хендлера сейчас перехватывается:

## 1. Проблема

Владелец узнаёт о падениях/ошибках ботов только случайно. Систематического уведомления нет.

Факт: `runtime/webhook_app.py:65-68` — единственное место, где необработанное исключение хендлера сейчас перехватывается:

```python
try:
    await entry.dispatcher.feed_webhook_update(entry.bot, update_data)
except Exception:
    logger.exception(f"Failed to process webhook update for bot_id={bot_id}")
return web.json_response({"ok": True})
```

Логируется и **проглатывается** — ни алерта, ни ретрая, ни видимости для владельца. Aiogram-уровня `errors.router`/`ErrorEvent`-хендлера нигде в репозитории нет (проверено grep по всему дереву).

Существующий `/analytics`-дашборд владельца (`runtime/factory_analytics_api.py`, `/api/factory/bots`) читает `bots`/`bot_features`/`bot_feedback` **напрямую из central DB** — это pull, не push, и не использует `office_events` вообще. Он не подходит как канал живых алертов сам по себе, но его существование подтверждает, что owner-only авторизация (`_authenticate_owner()`, `OWNER_ID`) уже есть и можно переиспользовать паттерн.

## 2. Существующий механизм office_events — что можно и нельзя переиспользовать

`features/office_events.py`:
- `publish_event(source_bot_id, event_type, payload)` — рассылает подписчикам, каждый вызов хука изолирован в try/except, никогда не роняет паблишера.
- Payload — закрытый набор датаклассов в `_EVENT_TYPES` (сейчас только `order.created` → `OrderCreatedEvent`). Ловит хаотичные dict-пейлоады уже на этапе `publish_event()`.
- Подписки — таблица `bot_office_links(source_bot_id, target_bot_id, event_type)`, **явный опт-ин**: строка добавляется вручную через `add_office_link()` (владелец линкует двух СВОИХ ботов в UI). `get_office_subscribers()` — плоский `SELECT ... WHERE source_bot_id = ? AND event_type = ?`.
- Доставка требует, чтобы target был в **живом Registry** (`entry = registry.get(target_bot_id)`) и имел `on_office_event` в `entry.config`. `FACTORY_BOT_ID = 0` **уже в реестре** — устанавливается вручную в `runtime/combined_app.py:133` через `build_factory_entry()` (`runtime/registry.py:574-578`), это псевдо-`BotEntry` без строки в таблице `bots`.

**Ключевой вывод:** `bot_office_links` — не то место для "Creator подписан на всех". Она рассчитана на явные пары "бот A ↔ бот B", созданные владельцем через UI для конкретных бизнес-связей (например ресторан ↔ доставка). Если завести туда системную подписку Creator на каждого бота, придётся вставлять строку при **создании каждого нового бота** — а любой бот, забытый в этой цепочке (баг, race при `reload_one`), тихо перестаёт слать алерты. Это прямо тот же класс проблем, что уже в бэклоге ([[backlog_moderator_admin_panel_concurrency]], [[backlog_create_flow_pending_race]]).

## 3. Что публиковать как "критическое событие"

Новый закрытый тип в `_EVENT_TYPES`, по аналогии с `OrderCreatedEvent`:

```python
@dataclass(frozen=True)
class CriticalErrorEvent:
    category: str        # "unhandled_exception" | "webhook_failure" | "payment_error" | "quota_exceeded"
    detail: str           # короткое сообщение, БЕЗ трейсбека целиком (см. риски §5)
    occurred_at: str      # ISO timestamp
```

Источники и где технически перехватывать:

| Категория | Где | Факт |
|---|---|---|
| Необработанное исключение в хендлере | `runtime/webhook_app.py:65-68`, `except Exception: logger.exception(...)` | точка уже существует, просто сейчас no-op после лога — добавить publish_event здесь |
| Ошибка на уровне aiogram-диспетчера (до входа в хендлер, middleware) | Нет `errors.router`/`ErrorEvent`-регистрации нигде в репо | нужно **добавлять новую** точку перехвата — `dp.errors.register()` в `runtime/registry.py`'s `build_entry()`, там же где сейчас регистрируется `on_office_event` (строки 656-696) |
| Ошибка платежа | `features/sellable_items.py:923-928` — уже ловит `TelegramAPIError`/`TelegramRetryAfter` вокруг `send_invoice`, сейчас только логирует warning + сообщение юзеру | добавить publish_event в этот except |
| Квота Google Sheets исчерпана | `features/sheets.py:178-184` — комментарий признаёт, что "could be distinguishable as quota failure", но трекинга нет | нужно **сначала** классифицировать ошибку (сейчас generic `logger.error`), потом публиковать |
| Telegram API 429 / flood control | Нигде не трекается системно (grep подтвердил — только 2 ad-hoc места, оба просто логируют) | тот же паттерн, что и Sheets — добавлять классификацию с нуля |

Важно: `publish_event()` требует **живой Registry**, значит доставка возможна только из процесса, где Registry уже поднят (webhook_app/combined_app) — то есть все перечисленные точки перехвата и так внутри этого процесса, ограничение не мешает.

## 4. Системная подписка Creator-бота (не через bot_office_links)

Предложение: НЕ трогать `bot_office_links`. Вместо этого — отдельный, безусловный путь доставки специально для критических событий, обходящий таблицу подписок вообще:

- В `publish_event()` (или отдельной функции `publish_critical_event()`) для `event_type="bot.critical_error"` доставка идёт **всегда** в `FACTORY_BOT_ID`, дополнительно к обычным `get_office_subscribers()`-получателям (которых для этого типа обычно не будет — пользовательские связи ботов не должны подписываться на чужие крэши).
- Технически: `registry.get(FACTORY_BOT_ID)` уже возвращает псевдо-entry, установленный в `combined_app.py:133`. Нужно, чтобы у этого entry тоже был `on_office_event`-хук — сейчас `build_factory_entry()` его не ставит (Factory bot не проходит через обычный `build_entry()`-путь с шаблоном, значит не проходит и авто-wiring на строках 656-696). Значит хук для Factory-бота нужно регистрировать явно, отдельно, рядом с местом установки записи в `combined_app.py:133` — не переиспользуя `register_office_event_hook` общего конфига, а прямой Telegram `bot.send_message(OWNER_ID, ...)`.
- Это делает подписку **системной, а не пользовательской**: не завязана на строку в `bot_office_links`, не может быть случайно удалена через UI-линковку офисов, не требует вставки при создании нового бота — работает для ЛЮБОГО bot_id автоматически, т.к. FACTORY_BOT_ID есть в реестре всегда.

## 5. Риски

**Спам одинаковыми ошибками.** Без rate-limit — цикл ретраев Telegram (webhook доставляется повторно при таймауте) или флапающий баг может слать одно и то же сообщение владельцу раз в секунду. Нужен дедуп/rate-limit **до** `publish_event`, не после — иначе сама доставка алерта станет источником нагрузки. Вариант: ключ `(bot_id, category, detail_hash)` с окном (например, не чаще раза в N минут на один и тот же ключ) — in-memory дешевле, чем в БД, но теряется при рестарте процесса (приемлемо для MVP, как и остальной office_events — "no persistence" уже осознанный trade-off всего модуля).

**Трейсбек целиком в сообщении владельцу.** Полный стектрейс может содержать данные пользователя (payload запроса, токены в контексте исключения). `detail` должен быть коротким классифицированным сообщением, не `traceback.format_exc()` целиком — отдельный вопрос, нужен ли владельцу способ дотянуться до полного лога (ссылка на Railway logs?) отдельно от алерта.

**FACTORY_BOT_ID сам может быть недоступен/не в реестре в момент ошибки.** Тот же паттерн деградации, что и у обычных подписчиков в `publish_event()` — "offline получатель тихо теряет событие" уже принятый MVP-trade-off (docs/OFFICES_DESIGN.md §6.3). Для критических алертов это менее приемлемо, чем для обычных офисов — стоит явно решить, ок ли это для v1.

**Кто вызывает publish для каждой категории — единообразие.** 5 разных точек перехвата (webhook_app, будущий errors.router, sellable_items, sheets, будущий Telegram-429-трекер) — если каждая пишет свой формат `detail`, дашборд/сообщения будут неконсистентны. Нужен один общий helper (`report_critical_error(bot_id, category, exc)`), а не 5 копий `publish_event(...)`.

**Отсутствие квота/rate-limit трекинга — это отдельная фича, не часть алертинга.** Сейчас нет вообще никакого счётчика для Sheets/Telegram/Claude API квот — "квота исчерпана" как категория критического события требует сначала классификации самой ошибки (retryable vs quota vs auth), которой сегодня нет нигде в коде. Это увеличивает объём задачи за пределы "перехватить и переслать".

## Решения владельца (зафиксированы, реализованы)

1. Добавлен aiogram `errors.router` (`register_critical_error_handler`), не только `webhook_app.py`.
2. Обход `bot_office_links`, прямая доставка в `FACTORY_BOT_ID` — реализовано как описано в §4.
3. Квота/rate-limit трекинг внешних API — НЕ делается сейчас. v1 = только `unhandled_exception` (aiogram errors) + `webhook_failure` (webhook_app.py except).
4. Закрыты все риски из §5: rate-limit на повторы, очистка секретов, graceful degradation.

## 6. Найдено на ревью, исправлено

Три независимых ревью-агента прогнали код через прямую трассировку источника aiogram и файлы диффа. Подтверждённые находки — исправлены, с regression-тестами:

- **Санитайзер пропускал реалистичные секреты этой кодовой базы** (YooKassa `secret_key=`, Google API `key=AIza...`, непрозрачные base64-значения без узнаваемой формы). Добавлен второй слой — литеральная замена известных секретов процесса из `config.py`, плюс паттерн `key=`/`secret=`/`password=`-подобных пар. Тесты: `test_sanitize_detail_redacts_key_value_shaped_secret`, `test_sanitize_detail_redacts_known_process_secret_value`.
- **Коллизия ключа rate-limit при обрезке текста** — два РАЗНЫХ инцидента с одинаковым длинным префиксом (>500 символов) схлопывались в один ключ, второй тихо подавлялся. Исправлено: ключ теперь строится по полному очищенному тексту ДО обрезки, обрезка применяется только к тексту, который реально уходит в сообщение. Тест: `test_distinct_errors_with_same_long_prefix_are_not_rate_limited_together`.

Найдено, но НЕ исправлено (принятый trade-off для v1, симметричный с уже принятым в docs/OFFICES_DESIGN.md §6.3 "offline получатель тихо теряет событие"):

- **Если ломается сам фабричный бот** — `report_critical_error` пытается доставить алерт через `factory_entry.bot`, тот же самый (потенциально сломанный) экземпляр. Если у фабричного бота невалидный токен или его банит Telegram, `send_message` падает, алерт теряется (только логируется). Отдельный канал доставки (например, прямой HTTP-вызов Telegram Bot API с BOT_TOKEN из env, в обход `registry`) — возможное будущее расширение, не в v1.
- **`runtime/webhook_app.py`, если запущен отдельно** (`python -m runtime.webhook_app`, не через `combined_app.py`) не вызывает `office_events.set_registry()` — в этом режиме алерты молча не доставляются (только `logger.warning`). Сам файл документирует, что не используется в проде ("Not wired into any live deployment") — `combined_app.py` (реальная точка входа Railway) вызывает `set_registry()` штатно.
