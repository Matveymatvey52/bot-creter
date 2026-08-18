from __future__ import annotations

import os
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

router = Router()

WELCOME_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "welcome.png"

_WELCOME_CAPTION = (
    "👋 Привет! Я Bot-Creator — создаю Telegram-ботов по твоему описанию.\n\n"
    "Опиши, какого бота хочешь — текстом или голосовым 🎤 или перейди к своим уже созданным ботам."
)

try:
    _OWNER_ID = int(os.getenv("OWNER_ID", "0"))
except ValueError:
    _OWNER_ID = 0


def _dashboard_url(telegram_user_id: int) -> str | None:
    # Same magic-link pattern as handlers/admin_manager.py's _analytics_url —
    # opens the owner-only factory dashboard (/app/{FACTORY_BOT_ID}) inside
    # Telegram via WebAppInfo instead of a plain outbound link.
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


def _start_keyboard(telegram_user_id: int) -> InlineKeyboardMarkup:
    # "list" callback_data matches handlers/manage_bots.py's existing cb_list
    # handler exactly — reused as-is, not duplicated. "start_create"
    # matches handlers/create_bot.py's cb_start_create (added alongside this).
    rows = [
        [InlineKeyboardButton(text="➕ Создать бота", callback_data="start_create")],
        [InlineKeyboardButton(text="📋 Мои боты", callback_data="list")],
    ]
    if _OWNER_ID != 0 and telegram_user_id == _OWNER_ID:
        dashboard_url = _dashboard_url(telegram_user_id)
        if dashboard_url is not None:
            rows.append([InlineKeyboardButton(
                text="📊 Панель управления",
                web_app=WebAppInfo(url=dashboard_url),
            )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = _start_keyboard(message.from_user.id)
    if WELCOME_IMAGE.exists():
        await message.answer_photo(FSInputFile(WELCOME_IMAGE), caption=_WELCOME_CAPTION, reply_markup=keyboard)
    else:
        await message.answer(_WELCOME_CAPTION, reply_markup=keyboard)


@router.callback_query(F.data == "start_menu")
async def cb_start_menu(callback: CallbackQuery):
    # Same content as /start — this is Баг 3's "◀ В главное меню" target,
    # reuses _WELCOME_CAPTION/_start_keyboard rather than duplicating the text.
    await callback.answer()
    keyboard = _start_keyboard(callback.from_user.id)
    if WELCOME_IMAGE.exists():
        await callback.message.answer_photo(FSInputFile(WELCOME_IMAGE), caption=_WELCOME_CAPTION, reply_markup=keyboard)
    else:
        await callback.message.answer(_WELCOME_CAPTION, reply_markup=keyboard)
