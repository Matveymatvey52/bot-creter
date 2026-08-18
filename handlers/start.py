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

from db.database import get_bot_admins

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


async def _is_creator_bot_admin(telegram_user_id: int) -> bool:
    # Same check runtime/miniapp_api.py's compute_metrics route uses to gate
    # a bot's own analytics to that bot's admins (get_bot_admins(bot_id),
    # membership by str(telegram_user_id)) — reused as-is here, scoped to
    # FACTORY_BOT_ID (bot_id=0, the Creator bot itself), rather than
    # inventing a separate check. Deliberately NOT the same thing as
    # OWNER_ID: any admin of the Creator bot (db/database.py's bot_admins
    # table for bot_id=0) qualifies, not just the single system owner.
    from runtime.registry import FACTORY_BOT_ID

    admin_ids = await get_bot_admins(FACTORY_BOT_ID)
    return str(telegram_user_id) in admin_ids


def _registry_url(telegram_user_id: int) -> str | None:
    # Same magic-link dashboard URL as _dashboard_url, with ?view=registry
    # appended — OwnerRegistryScreen.tsx's own routing convention
    # (FactoryDashboardScreen.tsx reads this query param).
    dashboard_url = _dashboard_url(telegram_user_id)
    if dashboard_url is None:
        return None
    separator = "&" if "?" in dashboard_url else "?"
    return f"{dashboard_url}{separator}view=registry"


async def _start_keyboard(telegram_user_id: int) -> InlineKeyboardMarkup:
    # "list" callback_data matches handlers/manage_bots.py's existing cb_list
    # handler exactly — reused as-is, not duplicated. "start_create"
    # matches handlers/create_bot.py's cb_start_create (added alongside this).
    rows = [
        [InlineKeyboardButton(text="➕ Создать бота", callback_data="start_create")],
        [InlineKeyboardButton(text="📋 Мои боты", callback_data="list")],
    ]
    # The dashboard itself is role-based (see runtime/factory_analytics_api.py's
    # is_owner) — every user gets this button, not just OWNER_ID. The owner
    # sees the full "Моя фабрика" view (all bots, templates, candidates);
    # everyone else sees only their own bots. Label differs only so the
    # owner keeps recognizing their existing entry point.
    dashboard_url = _dashboard_url(telegram_user_id)
    if dashboard_url is not None:
        label = "📊 Панель управления" if (_OWNER_ID != 0 and telegram_user_id == _OWNER_ID) else "🏭 Моя фабрика"
        rows.append([InlineKeyboardButton(text=label, web_app=WebAppInfo(url=dashboard_url))])

    # Registry button (multitenancy stage 3, item 4): visible to any admin of
    # the Creator bot itself (bot_admins for bot_id=0), not OWNER_ID-gated —
    # see _is_creator_bot_admin's docstring.
    if dashboard_url is not None and await _is_creator_bot_admin(telegram_user_id):
        registry_url = _registry_url(telegram_user_id)
        if registry_url is not None:
            rows.append([InlineKeyboardButton(text="📋 Реестр ботов", web_app=WebAppInfo(url=registry_url))])

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = await _start_keyboard(message.from_user.id)
    if WELCOME_IMAGE.exists():
        await message.answer_photo(FSInputFile(WELCOME_IMAGE), caption=_WELCOME_CAPTION, reply_markup=keyboard)
    else:
        await message.answer(_WELCOME_CAPTION, reply_markup=keyboard)


@router.callback_query(F.data == "start_menu")
async def cb_start_menu(callback: CallbackQuery):
    # Same content as /start — this is Баг 3's "◀ В главное меню" target,
    # reuses _WELCOME_CAPTION/_start_keyboard rather than duplicating the text.
    await callback.answer()
    keyboard = await _start_keyboard(callback.from_user.id)
    if WELCOME_IMAGE.exists():
        await callback.message.answer_photo(FSInputFile(WELCOME_IMAGE), caption=_WELCOME_CAPTION, reply_markup=keyboard)
    else:
        await callback.message.answer(_WELCOME_CAPTION, reply_markup=keyboard)
