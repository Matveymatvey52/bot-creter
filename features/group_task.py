# FEATURE: group_task
# COMPATIBLE_WITH: accountant, booking_beauty, booking_fitness, booking_medical, booking_restaurant, campaign_tracker, car_rental, coworking_space, debtors, delivery_tracker, event_manager, event_rsvp, expense_tracker, feedback_survey, habit_tracker, inventory, loyalty_program, manager_secretary, moderator, orders_tracker, referral_program, rental_equipment, repair_tracker, shop_catalog, staff_scheduler, support_tickets, tour_operator, tourist_documents, trip_manager, vehicle_service
"""Owner-only "give this bot a task in a Telegram group" channel
(docs/GROUP_TASK_CHANNEL_DESIGN.md). A toggleable feature module, loaded
exactly like features/sheets.py/payments.py through the normal bot_features/
COMPATIBLE_WITH path in runtime/registry.py's _load_and_include_features()
— unlike features/office_events.py (a passive receive-hook, always wired,
never toggled), this module has a real Router with an active message
handler, so it goes through the standard feature-toggle UI
(handlers/manage_bots.py's "🧩 Фичи" panel) the same as sheets/payments.

Deliberately NOT an agent: on_group_message() below answers with a short,
context-aware CHAT reply (Haiku, no tool-use, no access to this bot's own
data/handlers) — it does not execute the task, only discusses it. See
design doc §6 "Не агент, а чат" for why this boundary is intentional for
v1, not an oversight.

Only OWNER_ID may address a bot here (checked on every message, not just at
connect time) — anyone else's mention/reply is silently ignored. Without
this, a bot connected to a group becomes an open, factory-owner-funded
Anthropic API endpoint for anyone who ends up in that group.

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

from aiogram import Bot, F, Router
from aiogram.types import Message

from handlers.admin_manager import OWNER_ID
from db.database import get_bot, get_group_task_config, set_group_task_config

logger = logging.getLogger(__name__)

router = Router()

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

_SYSTEM_PROMPT_TEMPLATE = (
    "Ты — {name}, Telegram-бот для «{template_id}». Владелец фабрики ботов "
    "пишет тебе рабочую задачу или вопрос в общей группе. Отвечай по существу, "
    "кратко, на русском языке. Ты не выполняешь действия в своей базе данных "
    "по этой задаче — только обсуждаешь её словами."
)


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
async def on_group_message(message: Message, bot: Bot, bot_id: int) -> None:
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

    try:
        from anthropic import AsyncAnthropic
        from config import ANTHROPIC_API_KEY

        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=_SYSTEM_PROMPT_TEMPLATE.format(name=bot_name, template_id=template_id),
            messages=[{"role": role, "content": content} for role, content in history],
        )
        reply_text = response.content[0].text if response.content else ""
    except Exception:
        logger.exception(f"on_group_message: Haiku call failed — bot_id={bot_id}")
        return

    if not reply_text:
        return
    history.append(("assistant", reply_text))
    del history[:-_CONTEXT_TURNS]

    try:
        await message.reply(reply_text)
    except Exception:
        logger.exception(f"on_group_message: failed to send reply — bot_id={bot_id}")
