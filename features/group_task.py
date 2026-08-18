# FEATURE: group_task
# COMPATIBLE_WITH: accountant, booking_beauty, booking_fitness, booking_medical, booking_restaurant, campaign_tracker, car_rental, coworking_space, debtors, delivery_tracker, event_manager, event_rsvp, expense_tracker, feedback_survey, habit_tracker, inventory, loyalty_program, manager_secretary, moderator, orders_tracker, referral_program, rental_equipment, repair_tracker, shop_catalog, staff_scheduler, support_tickets, tour_operator, tourist_documents, trip_manager, vehicle_service
"""Owner-only "give this bot a task in a Telegram group" channel
(docs/GROUP_TASK_CHANNEL_DESIGN.md, docs/GROUP_TASK_TOOL_USE_DESIGN.md). A
toggleable feature module, loaded exactly like features/sheets.py/payments.py
through the normal bot_features/COMPATIBLE_WITH path in
runtime/registry.py's _load_and_include_features() — unlike
features/office_events.py (a passive receive-hook, always wired, never
toggled), this module has a real Router with an active message handler, so
it goes through the standard feature-toggle UI (handlers/manage_bots.py's
"🧩 Фичи" panel) the same as sheets/payments.

Two modes, chosen per-message by which OTHER features are enabled on this
bot_id (see _TOOL_FEATURES below):

- **Chat mode** (no tool-enabling feature enabled): answers with a short,
  context-aware CHAT reply (Haiku, no tool-use, no access to this bot's own
  data) — it does not execute the task, only discusses it. This is the
  entire v1 behavior described in docs/GROUP_TASK_CHANNEL_DESIGN.md §6 "Не
  агент, а чат".
- **Tool-use mode** (sellable_items and/or payments enabled): Claude
  (Sonnet — tool-call reliability matters more than cost here) gets a
  handful of tools that wrap those two features' OWN CRUD functions
  (features/sellable_items.py's _create_item/_update_item_field/
  _toggle_item_active, features/payments.py's record_refund) plus a
  snapshot of current items/payments as context. A tool call is NEVER
  executed from the model's response directly — it is turned into a
  human-readable preview with ✅/❌ buttons (cb_grouptask_confirm/
  cb_grouptask_cancel below) and only runs once the owner taps ✅. See
  docs/GROUP_TASK_TOOL_USE_DESIGN.md for the full design and why the tool
  surface is deliberately limited to these two shared feature modules
  rather than any template-specific table.

Only OWNER_ID may address a bot here (checked on every message, not just at
connect time) — anyone else's mention/reply is silently ignored. Without
this, a bot connected to a group becomes an open, factory-owner-funded
Anthropic API endpoint for anyone who ends up in that group. The same check
gates the confirm/cancel buttons below — a tool call proposed to the owner
must also only be confirmable by the owner, not by anyone else added to the
group later.

Requires Telegram Privacy Mode DISABLED for the bot (BotFather → Bot
Settings → Group Privacy → Turn off) — with Privacy Mode on, ordinary group
messages (including ones that @-mention the bot or reply to it) never reach
this handler at all; Telegram simply doesn't deliver them. This module has
no way to detect or fix that from inside the bot — it's a one-time setup
step surfaced explicitly in handlers/manage_bots.py's connection guide, not
something this code can compensate for.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Protocol

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from handlers.admin_manager import OWNER_ID, _is_owner
from db.database import get_bot, get_bot_features, get_group_task_config, set_group_task_config

logger = logging.getLogger(__name__)

router = Router()


class _DbPathConfig(Protocol):
    """Any template Config dataclass with a db_path works — same duck-typed
    pattern as features/payments.py's PaymentsConfig. group_task.py rides on
    the SAME Dispatcher/ConfigMiddleware as the host template and the
    sellable_items/payments feature routers, so this is already injected
    into data["config"] for free; no separate get_bot()-based lookup needed."""
    db_path: str

# Minimum seconds between two Haiku calls for the SAME bot_id — guards
# against an accidental double-send/rapid edit-and-resend from the owner
# triggering two API calls for what's really one task, not a hard usage cap
# (only OWNER_ID can ever reach this handler at all — see module docstring).
# Same "cheap in-memory guard, no persistence" MVP tradeoff as
# features/office_events.py's _CRITICAL_ERROR_RATE_LIMIT_SECONDS.
_RATE_LIMIT_SECONDS = 3

# bot_id -> monotonic timestamp of the last reply sent, for _RATE_LIMIT_SECONDS above.
_last_reply_sent: dict[int, float] = {}

# bot_id -> list of (role, text) tuples, most recent last — short scrollback
# so a multi-message task ("...", "actually also do X") keeps context. Capped
# per module docstring's "no persistence" MVP choice: cleared on process
# restart, which just means the next message starts a fresh conversation,
# not a bug.
_CONTEXT_TURNS = 6
_conversation_context: dict[int, list[tuple[str, str]]] = {}


def invalidate_group_task_state(bot_id: int) -> None:
    """Drops this bot_id's in-memory rate-limit/conversation-context entries
    — review found these two dicts (unlike bot_group_task_config's DB row,
    already cleaned up by db.database.delete_bot) had no cleanup on bot
    deletion, so a deleted bot's entries would sit in memory for the rest of
    the process's life. Harmless at the factory's current scale (a handful
    of tuples per deleted bot_id, same "not unbounded, just untidy" shape as
    the gap this fixes), but worth closing rather than leaving asymmetric
    with features/office_events.py's _CRITICAL_ERROR_RATE_LIMIT_MAX_KEYS cap
    on its own equivalent dict. Called from handlers/manage_bots.py's
    cb_delete_bot right after db.database.delete_bot(bot_id), same
    "registry-adjacent module owns its own cache invalidation" pattern as
    runtime/registry.py's invalidate_custom_feature_cache(). Safe to call
    even if this bot_id never had an entry (dict.pop's default)."""
    _conversation_context.pop(bot_id, None)
    _last_reply_sent.pop(bot_id, None)
    _pending_actions.pop(bot_id, None)

_SYSTEM_PROMPT_TEMPLATE = (
    "Ты — {name}, Telegram-бот для «{template_id}». Владелец фабрики ботов "
    "пишет тебе рабочую задачу или вопрос в общей группе. Отвечай по существу, "
    "кратко, на русском языке. Ты не выполняешь действия в своей базе данных "
    "по этой задаче — только обсуждаешь её словами."
)

# ── Tool-use mode (docs/GROUP_TASK_TOOL_USE_DESIGN.md) ─────────────────────
#
# Feature-name -> Anthropic tool schemas wrapping THAT feature's own CRUD
# functions. Deliberately limited to feature modules shared across every
# template (sellable_items.py/payments.py) — no template-specific table
# (tour_operator.py's tours, shop_catalog's products, ...) gets a tool here;
# see the design doc for why a per-template tool surface isn't v1.
_TOOL_SCHEMAS_BY_FEATURE: dict[str, list[dict]] = {
    "sellable_items": [
        {
            "name": "create_sellable_item",
            "description": "Добавить новую позицию (товар/услугу) в каталог этого бота.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Название позиции"},
                    "description": {"type": "string", "description": "Необязательное описание"},
                    "price": {"type": "integer", "description": "Цена в рублях, целое число больше 0"},
                },
                "required": ["name", "price"],
            },
        },
        {
            "name": "update_sellable_item",
            "description": "Изменить название, описание или цену уже существующей позиции по её id.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": "id позиции из списка ниже"},
                    "field": {"type": "string", "enum": ["name", "description", "price"]},
                    "value": {"type": "string", "description": "Новое значение (для price — число)"},
                },
                "required": ["item_id", "field", "value"],
            },
        },
        {
            "name": "set_sellable_item_active",
            "description": (
                "Показать или скрыть позицию в каталоге (не удаляет её — удаления позиций нет). "
                "active=false скрывает, active=true возвращает в продажу."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": "id позиции из списка ниже"},
                    "active": {"type": "boolean"},
                },
                "required": ["item_id", "active"],
            },
        },
    ],
    "payments": [
        {
            "name": "refund_payment",
            "description": (
                "Отметить платёж как возвращённый по его telegram_payment_charge_id. "
                "Это ТОЛЬКО меняет статус в базе — реального возврата денег через Telegram "
                "Payments API не существует, деньгами владелец распоряжается сам через провайдера."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "telegram_payment_charge_id": {
                        "type": "string",
                        "description": "Точный charge_id из списка платежей ниже",
                    },
                },
                "required": ["telegram_payment_charge_id"],
            },
        },
    ],
}

_TOOL_SYSTEM_PROMPT_SUFFIX = (
    "\n\nУ тебя есть инструменты для реального выполнения простых действий в базе этого "
    "бота (см. список ниже и текущие данные). Вызывай ОДИН инструмент, только если владелец "
    "явно просит выполнить конкретное действие (добавить/изменить/скрыть позицию, отметить "
    "возврат платежа) и у тебя достаточно данных для его параметров — например, id позиции "
    "или точный charge_id бери из списка ниже, не придумывай. Если просьба неоднозначна, не "
    "хватает данных, или это просто вопрос/обсуждение — НЕ вызывай инструмент, ответь текстом "
    "и уточни. Вызванный инструмент ещё не выполняется сразу — владелец увидит превью и должен "
    "подтвердить его сам, так что можно предлагать действие даже при небольшой неуверенности, "
    "лишь бы параметры были осмысленными."
)

# Model used only when this bot_id has at least one of the features above
# enabled (tool-use branch) — Sonnet, not Haiku: tool-call/parameter
# selection reliability matters more than the extra cost here, and only
# OWNER_ID can ever trigger a call at all (see module docstring), so the
# volume this cost applies to is inherently small.
_TOOL_MODEL = "claude-sonnet-5"
_CHAT_MODEL = "claude-haiku-4-5-20251001"

# How long a proposed-but-unconfirmed tool call stays valid — same rationale
# as sellable_items.py's FLOW_TIMEOUT_SECONDS: an owner who taps ✅ on a
# days-old preview after the underlying data (item price, payment status)
# has since changed would get a confusingly stale action, not a fresh one.
_PENDING_TTL_SECONDS = 300

# bot_id -> {"token", "tool_name", "tool_input", "chat_id", "created_at"} —
# at most ONE pending tool call per bot at a time (a second proposal before
# the first is confirmed/cancelled simply replaces it, same "last one wins"
# tradeoff as _conversation_context's fixed-size scrollback).
_pending_actions: dict[int, dict] = {}


async def _recent_payments(db_path: str, limit: int = 20) -> list[dict]:
    """Read-only listing for tool-use context (§ below) — features/payments.py
    has no such helper of its own (record_refund only ever looks up ONE row
    by charge_id), so this stays local rather than growing that module's
    surface for a need only this tool-context building has."""
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT telegram_payment_charge_id, user_id, total_amount, currency, status "
            "FROM payments ORDER BY id DESC LIMIT ?",
            (limit,),
        )).fetchall()
        return [dict(r) for r in rows]


async def _build_tool_context(db_path: str, tool_features: set[str]) -> str:
    """Renders a short snapshot of THIS bot's current items/payments so
    Claude can resolve "скрой тур в Париж"/"верни деньги Ивану" to a real
    item_id/charge_id instead of guessing one — without this, every
    update/refund tool call would need the owner to already know and quote
    an internal id, defeating the point of a chat interface."""
    parts: list[str] = []
    if "sellable_items" in tool_features:
        from features.sellable_items import _all_items

        try:
            items = await _all_items(db_path)
        except Exception:
            logger.exception("_build_tool_context: failed to read sellable_items")
            items = []
        if items:
            lines = [
                f"id={i['id']} «{i['name']}» цена={i['price']}₽ "
                f"{'активна' if i['active'] else 'скрыта'}"
                for i in items[:30]
            ]
            parts.append("Текущие позиции каталога:\n" + "\n".join(lines))
        else:
            parts.append("Каталог позиций пока пуст.")
    if "payments" in tool_features:
        try:
            payments_rows = await _recent_payments(db_path)
        except Exception:
            logger.exception("_build_tool_context: failed to read payments")
            payments_rows = []
        if payments_rows:
            lines = [
                f"charge_id={p['telegram_payment_charge_id']} сумма={p['total_amount']} "
                f"{p['currency']} статус={p['status']}"
                for p in payments_rows
            ]
            parts.append("Последние платежи:\n" + "\n".join(lines))
        else:
            parts.append("Платежей пока не было.")
    return "\n\n".join(parts)


def _describe_tool_call(tool_name: str, tool_input: dict) -> str:
    """Best-effort human preview of a proposed tool call for the ✅/❌
    confirmation message — tolerant of missing/malformed fields (Claude's
    tool_input is untrusted model output, not yet validated the way
    _execute_tool_call below validates it before actually writing), so this
    must never raise: a broken preview is a worse failure than a slightly
    imprecise one."""
    if tool_name == "create_sellable_item":
        name = tool_input.get("name", "?")
        price = tool_input.get("price", "?")
        description = tool_input.get("description")
        extra = f", описание: «{description}»" if description else ""
        return f"➕ Добавить позицию «{name}», цена {price}₽{extra}"
    if tool_name == "update_sellable_item":
        return (
            f"✏️ Изменить позицию id={tool_input.get('item_id', '?')}: "
            f"{tool_input.get('field', '?')} → {tool_input.get('value', '?')!r}"
        )
    if tool_name == "set_sellable_item_active":
        action = "показать" if tool_input.get("active") else "скрыть"
        return f"👁 {action.capitalize()} позицию id={tool_input.get('item_id', '?')}"
    if tool_name == "refund_payment":
        return (
            "💸 Отметить как возвращённый платёж "
            f"charge_id={tool_input.get('telegram_payment_charge_id', '?')}"
        )
    return f"⚙️ {tool_name}({tool_input})"


async def _execute_tool_call(db_path: str, tool_name: str, tool_input: dict, admin_id: str) -> str:
    """Validates tool_input strictly (unlike _describe_tool_call above) and
    performs the real write through the wrapped feature's OWN CRUD function
    — never raw SQL from this module. Raises ValueError on anything
    malformed/not-found; callers turn that into a user-facing error instead
    of letting a bad model-generated parameter reach the database."""
    from features import payments, sellable_items

    if tool_name == "create_sellable_item":
        name = str(tool_input.get("name") or "").strip()[: sellable_items.NAME_MAX_LEN]
        if not name:
            raise ValueError("название позиции не может быть пустым")
        description = tool_input.get("description")
        description = str(description).strip()[: sellable_items.DESCRIPTION_MAX_LEN] if description else None
        try:
            price = int(tool_input["price"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("цена должна быть целым числом")
        if not (0 < price <= sellable_items.PRICE_MAX):
            raise ValueError(f"цена должна быть от 1 до {sellable_items.PRICE_MAX}")
        item_id = await sellable_items._create_item(db_path, name, description, price)
        return f"Добавлена позиция «{name}» (id={item_id}), цена {price}₽."

    if tool_name == "update_sellable_item":
        try:
            item_id = int(tool_input["item_id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("item_id должен быть числом")
        field = tool_input.get("field")
        if field not in ("name", "description", "price"):
            raise ValueError(f"неизвестное поле {field!r}")
        row = await sellable_items._item_row(db_path, item_id)
        if row is None:
            raise ValueError(f"позиция id={item_id} не найдена")
        raw_value = tool_input.get("value")
        if field == "price":
            try:
                value: object = int(raw_value)
            except (TypeError, ValueError):
                raise ValueError("цена должна быть целым числом")
            if not (0 < value <= sellable_items.PRICE_MAX):
                raise ValueError(f"цена должна быть от 1 до {sellable_items.PRICE_MAX}")
        elif field == "name":
            value = str(raw_value or "").strip()[: sellable_items.NAME_MAX_LEN]
            if not value:
                raise ValueError("название позиции не может быть пустым")
        else:
            value = str(raw_value or "").strip()[: sellable_items.DESCRIPTION_MAX_LEN]
        await sellable_items._update_item_field(db_path, item_id, field, value)
        return f"Позиция id={item_id}: {field} → {value!r}."

    if tool_name == "set_sellable_item_active":
        try:
            item_id = int(tool_input["item_id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("item_id должен быть числом")
        active = bool(tool_input.get("active"))
        row = await sellable_items._item_row(db_path, item_id)
        if row is None:
            raise ValueError(f"позиция id={item_id} не найдена")
        if bool(row["active"]) != active:
            await sellable_items._toggle_item_active(db_path, item_id)
        return f"Позиция id={item_id} теперь {'видна в каталоге' if active else 'скрыта'}."

    if tool_name == "refund_payment":
        charge_id = str(tool_input.get("telegram_payment_charge_id") or "").strip()
        if not charge_id:
            raise ValueError("telegram_payment_charge_id не может быть пустым")
        ok = await payments.record_refund(db_path, charge_id, admin_id)
        if not ok:
            raise ValueError(f"платёж {charge_id} не найден или уже возвращён")
        return f"Платёж {charge_id} отмечен как возвращённый."

    raise ValueError(f"неизвестный инструмент {tool_name!r}")


def _confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"gt_ok:{token}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"gt_no:{token}"),
    ]])


def _bot_is_mentioned_or_replied(message: Message, bot_username: str | None) -> bool:
    """True if this group message is explicitly addressed to the bot — either
    a reply to one of the bot's own messages, or an @-mention of its
    username in the text/caption. Deliberately does NOT match on the bot's
    display name appearing in plain text (unlike the from-scratch generator
    prompt's draft example in services/claude_service.py's
    GENERATE_SYSTEM_PROMPT) — with Privacy Mode required off (see module
    docstring), the bot receives every group message regardless, so a
    plain-text-substring match here would make it reply to any mention of
    its name in someone else's unrelated conversation, not just messages
    actually directed at it."""
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.is_bot and message.reply_to_message.from_user.username == bot_username:
            return True
    if bot_username:
        text = message.text or message.caption or ""
        if f"@{bot_username}".lower() in text.lower():
            return True
    return False


def _strip_mention(text: str, bot_username: str | None) -> str:
    if bot_username:
        text = text.replace(f"@{bot_username}", "").replace(f"@{bot_username.lower()}", "")
    return text.strip()


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text | F.caption)
async def on_group_message(message: Message, bot: Bot, bot_id: int, config: _DbPathConfig) -> None:
    """Handles every text/caption message in a group this bot is a member
    of. Three gates, in order, each a silent no-op on failure (a group has
    many messages not meant for this bot — most of this function's job is
    recognizing which single message actually is):

    1. Sender must be OWNER_ID (see module docstring).
    2. Message must address this bot (mention/reply — _bot_is_mentioned_or_replied).
    3. If this bot has no bot_group_task_config row yet, THIS message's chat
       becomes its bound group (auto-connect, docs/GROUP_TASK_CHANNEL_DESIGN.md
       §5) — a confirmation is sent instead of a task reply. Otherwise, the
       message's chat must match the ALREADY bound group (a bot bound to
       group A ignores mentions from group B, same isolation principle as
       _attach_bot_id_middleware's bot_id inject)."""
    if not message.from_user or message.from_user.id != OWNER_ID:
        return

    me = await bot.get_me()
    if not _bot_is_mentioned_or_replied(message, me.username):
        return

    existing = await get_group_task_config(bot_id)
    if existing is None:
        await set_group_task_config(bot_id, message.chat.id)
        await message.reply(
            f"✅ Готово! Теперь пиши мне сюда задачи, упоминая @{me.username} "
            "или отвечая на мои сообщения."
        )
        return

    if not existing["enabled"] or existing["group_chat_id"] != message.chat.id:
        return

    now = time.monotonic()
    last_sent = _last_reply_sent.get(bot_id)
    if last_sent is not None and now - last_sent < _RATE_LIMIT_SECONDS:
        return
    _last_reply_sent[bot_id] = now

    text = _strip_mention(message.text or message.caption or "", me.username)
    if not text:
        return

    bot_row = await get_bot(bot_id)
    bot_name = bot_row["name"] if bot_row else me.username
    # bots table has no template_id column — infer_template_id() re-derives it
    # from the bot's own file's '# TEMPLATE: <id>' marker (runtime/registry.py's
    # existing convention, same one build_entry() uses); a from-scratch bot
    # (no marker) falls back to "general" the same as an unrecognized bot_row.
    # Imported here, not at module top, for the same reason
    # features/office_events.py's report_critical_error() locally imports
    # from runtime.registry: that module imports THIS one (via
    # _load_and_include_features()'s dynamic feature loading), so a
    # module-level import here would be circular.
    from runtime.registry import infer_template_id

    template_id = (
        infer_template_id(bot_row["file_path"]) if bot_row and bot_row.get("file_path") else None
    ) or "general"

    history = _conversation_context.setdefault(bot_id, [])
    history.append(("user", text))
    del history[:-_CONTEXT_TURNS]

    # Tool-use mode only kicks in for features that actually HAVE a tool
    # wrapper (_TOOL_SCHEMAS_BY_FEATURE) AND are enabled for this specific
    # bot_id — a bot without sellable_items/payments enabled gets the same
    # chat-only behavior as before this feature existed.
    enabled_features = set(await get_bot_features(bot_id))
    tool_features = enabled_features & _TOOL_SCHEMAS_BY_FEATURE.keys()
    tools = [schema for feature in tool_features for schema in _TOOL_SCHEMAS_BY_FEATURE[feature]]

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(name=bot_name, template_id=template_id)
    if tools:
        tool_context = await _build_tool_context(config.db_path, tool_features)
        system_prompt += _TOOL_SYSTEM_PROMPT_SUFFIX
        if tool_context:
            system_prompt += "\n\n" + tool_context

    try:
        from anthropic import AsyncAnthropic
        from config import ANTHROPIC_API_KEY

        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        create_kwargs: dict = {}
        if tools:
            create_kwargs["tools"] = tools
        response = await client.messages.create(
            model=_TOOL_MODEL if tools else _CHAT_MODEL,
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": role, "content": content} for role, content in history],
            **create_kwargs,
        )
    except Exception:
        logger.exception(f"on_group_message: Claude call failed — bot_id={bot_id}")
        return

    text_block = next((b for b in response.content if b.type == "text"), None)
    tool_block = next((b for b in response.content if b.type == "tool_use"), None)

    if tool_block is not None:
        token = uuid.uuid4().hex[:12]
        _pending_actions[bot_id] = {
            "token": token,
            "tool_name": tool_block.name,
            "tool_input": dict(tool_block.input or {}),
            "chat_id": message.chat.id,
            "created_at": time.monotonic(),
        }
        preview = _describe_tool_call(tool_block.name, dict(tool_block.input or {}))
        history.append(("assistant", f"[Предложил действие: {preview}]"))
        del history[:-_CONTEXT_TURNS]
        try:
            await message.reply(
                f"{preview}\n\nПодтвердить выполнение?",
                reply_markup=_confirm_keyboard(token),
            )
        except Exception:
            logger.exception(f"on_group_message: failed to send confirmation — bot_id={bot_id}")
        return

    reply_text = text_block.text if text_block is not None else ""
    if not reply_text:
        return
    history.append(("assistant", reply_text))
    del history[:-_CONTEXT_TURNS]

    try:
        await message.reply(reply_text)
    except Exception:
        logger.exception(f"on_group_message: failed to send reply — bot_id={bot_id}")


@router.callback_query(F.data.startswith("gt_ok:"))
async def cb_grouptask_confirm(callback: CallbackQuery, bot_id: int, config: _DbPathConfig) -> None:
    """Owner tapped ✅ on a tool-call preview — the ONLY path in this module
    that actually writes anything. Re-checks ownership AND group binding
    (not just token match) for the same reason on_group_message's own gates
    do: a stale/forwarded callback_data from outside the bound group must
    not be honorable just because it happens to carry a valid-looking token."""
    if not callback.from_user or not _is_owner(callback.from_user.id):
        await callback.answer()
        return

    token = callback.data.split(":", 1)[1] if callback.data else ""
    pending = _pending_actions.get(bot_id)
    if pending is None or pending["token"] != token:
        await callback.answer("Действие уже выполнено, отменено или устарело.", show_alert=True)
        return

    # Claim (pop) the pending entry BEFORE the first `await` below — two
    # near-simultaneous ✅ taps (double-tap, or Telegram redelivering the
    # callback) both reaching the token-match check above is otherwise a
    # real TOCTOU: the original version only popped after awaiting
    # get_group_task_config, giving the event loop a chance to run both
    # callbacks' checks before either claimed the entry, letting a
    # non-idempotent tool (create_sellable_item) execute twice. Popping
    # here — a plain dict operation, no await in between — means the
    # second concurrent invocation always finds pending is None and exits
    # via the check above instead.
    _pending_actions.pop(bot_id, None)

    existing = await get_group_task_config(bot_id)
    chat_id = callback.message.chat.id if callback.message else None
    if existing is None or not existing["enabled"] or existing["group_chat_id"] != chat_id:
        await callback.answer()
        return

    if time.monotonic() - pending["created_at"] > _PENDING_TTL_SECONDS:
        await callback.answer("Действие устарело — повторите задачу заново.", show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_text("⏳ Действие устарело и было отменено автоматически.")
            except Exception:
                logger.exception(f"cb_grouptask_confirm: failed to edit stale message — bot_id={bot_id}")
        return

    await callback.answer()

    try:
        result_text = await _execute_tool_call(
            config.db_path, pending["tool_name"], pending["tool_input"], str(callback.from_user.id)
        )
        outcome = f"✅ {result_text}"
    except ValueError as exc:
        outcome = f"❌ Не выполнено: {exc}"
    except Exception:
        logger.exception(f"cb_grouptask_confirm: tool execution failed — bot_id={bot_id}")
        outcome = "❌ Не выполнено — внутренняя ошибка, подробности в логах бота."

    history = _conversation_context.setdefault(bot_id, [])
    history.append(("user", f"[Владелец подтвердил действие. Результат: {outcome}]"))
    del history[:-_CONTEXT_TURNS]

    if callback.message:
        try:
            await callback.message.edit_text(outcome)
        except Exception:
            logger.exception(f"cb_grouptask_confirm: failed to edit result message — bot_id={bot_id}")


@router.callback_query(F.data.startswith("gt_no:"))
async def cb_grouptask_cancel(callback: CallbackQuery, bot_id: int) -> None:
    if not callback.from_user or not _is_owner(callback.from_user.id):
        await callback.answer()
        return

    token = callback.data.split(":", 1)[1] if callback.data else ""
    pending = _pending_actions.get(bot_id)
    if pending is not None and pending["token"] == token:
        _pending_actions.pop(bot_id, None)

    await callback.answer("Отменено.")
    if callback.message:
        try:
            await callback.message.edit_text("❌ Отменено владельцем.")
        except Exception:
            logger.exception(f"cb_grouptask_cancel: failed to edit message — bot_id={bot_id}")
