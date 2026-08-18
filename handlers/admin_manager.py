from __future__ import annotations

import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from db.database import (
    add_bot_admin,
    get_all_bots,
    get_bot,
    get_bot_admins,
    remove_bot_admin,
)

router = Router()

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0


class AdminStates(StatesGroup):
    choosing_bot_to_add = State()
    entering_id_to_add = State()
    choosing_bot_to_remove = State()
    entering_id_to_remove = State()
    choosing_bot_to_list = State()


def _is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def _can_manage_bot(user_id: int, bot_row: dict) -> bool:
    """Per-bot authorization gate for the Telegram bot's own UI (Stage 1 of
    the multitenancy rollout). The system owner can manage every bot; a
    customer can manage only the bot(s) they created
    (bots.owner_telegram_id == user_id, set at creation time in
    handlers/create_bot.py). Mirrors the same "owner-equivalent" semantics
    runtime/factory_analytics_api.py's _authenticate_bot_access() already
    applies to the miniapp REST API — this is the Telegram-bot-side
    counterpart of that check."""
    return _is_owner(user_id) or bot_row.get("owner_telegram_id") == user_id


def _bots_keyboard(bots: list[dict], action: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"🤖 {b['name']}",
        callback_data=f"adm_{action}:{b['id']}",
    )] for b in bots]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="adm_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /analytics ─────────────────────────────────────────────────────────────────
# Opens the owner-only factory dashboard (runtime/factory_analytics_api.py) —
# same magic-link pattern as templates/tour_operator.py's cmd_app/_miniapp_url,
# just targeting FACTORY_BOT_ID (the mini-app SPA's /app/0 route, see
# runtime/miniapp_api.py's serve_app_shell special case for that bot_id and
# miniapp/src/App.tsx's isFactoryBotPath()) instead of a tenant bot.

def _analytics_url(telegram_user_id: int) -> str | None:
    from runtime.miniapp_api import mint_magic_link_token
    from runtime.registry import FACTORY_BOT_ID

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    port = os.getenv("PORT", "8080")
    base_url = f"https://{railway_domain}" if railway_domain else f"http://localhost:{port}"
    try:
        token = mint_magic_link_token(FACTORY_BOT_ID, telegram_user_id)
    except RuntimeError:
        return None
    return f"{base_url}/app/{FACTORY_BOT_ID}?token={token}"


@router.message(Command("analytics"))
async def cmd_analytics(message: Message):
    if not _is_owner(message.from_user.id):
        return
    url = _analytics_url(message.from_user.id)
    if url is None:
        await message.answer("Дашборд недоступен: не настроен MINIAPP_SECRET.")
        return
    await message.answer(
        f'<a href="{url}">📊 Открыть аналитику фабрики</a>',
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ── /addadmin ──────────────────────────────────────────────────────────────────

@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    bots = await get_all_bots()
    if not bots:
        await message.answer("Ботов пока нет.")
        return
    await state.set_state(AdminStates.choosing_bot_to_add)
    await message.answer("Выбери бота, которому хочешь добавить админа:", reply_markup=_bots_keyboard(bots, "add"))


@router.callback_query(F.data.startswith("adm_add:"), AdminStates.choosing_bot_to_add)
async def cb_bot_selected_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    await state.update_data(bot_id=bot_id, bot_name=b["name"] if b else "?")
    await state.set_state(AdminStates.entering_id_to_add)
    await callback.message.edit_text(
        f"Отправь Telegram ID пользователя, которого хочешь добавить как админа для <b>{b['name'] if b else '?'}</b>.\n\n"
        "Узнать ID можно через @userinfobot.",
        parse_mode="HTML",
    )


@router.message(AdminStates.entering_id_to_add, ~F.text.startswith("/"))
async def msg_id_to_add(message: Message, state: FSMContext):
    if not message.text or not message.text.lstrip("-").isdigit():
        await message.answer("Нужно отправить числовой Telegram ID.")
        return
    data = await state.get_data()
    await add_bot_admin(data["bot_id"], message.text.strip())
    await state.clear()
    await message.answer(
        f"✅ Пользователь <code>{message.text.strip()}</code> теперь админ бота <b>{data['bot_name']}</b>.",
        parse_mode="HTML",
    )


# ── /removeadmin ───────────────────────────────────────────────────────────────

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    bots = await get_all_bots()
    if not bots:
        await message.answer("Ботов пока нет.")
        return
    await state.set_state(AdminStates.choosing_bot_to_remove)
    await message.answer("Выбери бота, у которого хочешь убрать админа:", reply_markup=_bots_keyboard(bots, "rem"))


@router.callback_query(F.data.startswith("adm_rem:"), AdminStates.choosing_bot_to_remove)
async def cb_bot_selected_remove(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    admins = await get_bot_admins(bot_id)
    if not admins:
        await state.clear()
        await callback.message.edit_text(f"У бота <b>{b['name'] if b else '?'}</b> нет дополнительных админов.", parse_mode="HTML")
        return
    await state.update_data(bot_id=bot_id, bot_name=b["name"] if b else "?")
    await state.set_state(AdminStates.entering_id_to_remove)
    lines = "\n".join(f"• <code>{i}</code>" for i in admins)
    await callback.message.edit_text(
        f"Текущие админы <b>{b['name'] if b else '?'}</b>:\n{lines}\n\nОтправь ID пользователя, которого убрать.",
        parse_mode="HTML",
    )


@router.message(AdminStates.entering_id_to_remove, ~F.text.startswith("/"))
async def msg_id_to_remove(message: Message, state: FSMContext):
    if not message.text or not message.text.lstrip("-").isdigit():
        await message.answer("Нужно отправить числовой Telegram ID.")
        return
    data = await state.get_data()
    await remove_bot_admin(data["bot_id"], message.text.strip())
    await state.clear()
    await message.answer(
        f"✅ Пользователь <code>{message.text.strip()}</code> убран из админов бота <b>{data['bot_name']}</b>.",
        parse_mode="HTML",
    )


# ── /admins ────────────────────────────────────────────────────────────────────

@router.message(Command("admins"))
async def cmd_list_admins(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    bots = await get_all_bots()
    if not bots:
        await message.answer("Ботов пока нет.")
        return
    await state.set_state(AdminStates.choosing_bot_to_list)
    await message.answer("Выбери бота, чтобы посмотреть его админов:", reply_markup=_bots_keyboard(bots, "lst"))


@router.callback_query(F.data.startswith("adm_lst:"), AdminStates.choosing_bot_to_list)
async def cb_bot_selected_list(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    admins = await get_bot_admins(bot_id)
    if not admins:
        await callback.message.edit_text(f"У бота <b>{b['name'] if b else '?'}</b> нет дополнительных админов.", parse_mode="HTML")
        return
    lines = "\n".join(f"• <code>{i}</code>" for i in admins)
    await callback.message.edit_text(
        f"👥 Админы бота <b>{b['name'] if b else '?'}</b>:\n{lines}",
        parse_mode="HTML",
    )


# ── cancel ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Отменено.")
