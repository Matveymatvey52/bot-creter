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


def _bots_keyboard(bots: list[dict], action: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"🤖 {b['name']}",
        callback_data=f"adm_{action}:{b['id']}",
    )] for b in bots]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="adm_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
