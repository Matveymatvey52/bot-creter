import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Update

from config import BOT_TOKEN
from db.database import get_all_bots, init_db, set_bot_group, update_bot_status
from handlers.admin_manager import router as admin_router
from handlers.create_bot import auto_launch_managed_bot, router as create_router, set_bot_id, set_manager_username
from handlers.general import router as general_router
from handlers.manage_bots import router as manage_router
from handlers.start import router as start_router
from services.bot_runner import _make_extra_env, start_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


class ManagedBotMiddleware(BaseMiddleware):
    """Intercepts managed_bot updates (Bot API 9.6+) before aiogram routing."""

    def __init__(self, storage):
        self.storage = storage
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        extra = getattr(event, "model_extra", None) or {}
        if "managed_bot" in extra:
            bot: Bot = data["bot"]
            asyncio.create_task(auto_launch_managed_bot(extra["managed_bot"], bot, self.storage))
            return  # don't forward to normal handlers
        return await handler(event, data)


async def restore_bots():
    try:
        bots = await get_all_bots()
    except Exception as e:
        logger.error(f"restore_bots: could not read bots from DB, skipping restore: {e}")
        return

    restored = 0
    failed = 0
    for bot in bots:
        if bot["status"] == "running" and bot["file_path"] and bot["token"]:
            try:
                pid = await start_bot(bot["id"], bot["file_path"], bot["token"], extra_env=_make_extra_env(bot))
                await update_bot_status(bot["id"], "running", pid)
                logger.info(f"Restored bot '{bot['name']}' (ID: {bot['id']}, PID: {pid})")
                restored += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Failed to restore bot '{bot['name']}' (ID: {bot['id']}): {e}")
                await update_bot_status(bot["id"], "error")
                failed += 1
    log_fn = logger.warning if failed else logger.info
    log_fn(f"restore_bots: {restored} restored, {failed} failed")


async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN)

    # Store our username for managed-bot deep links
    try:
        me = await bot.get_me()
        set_manager_username(me.username)
        set_bot_id(me.id)
        logger.info(f"Manager bot: @{me.username} (id={me.id})")
    except Exception as e:
        logger.warning(f"Could not fetch bot info: {e}")

    # Set intro page shown before user presses Start
    try:
        await bot.set_my_description(
            "Создаю Telegram-ботов по твоему описанию 🤖\n\n"
            "⚡ Быстро — бот готов за несколько минут\n"
            "🧩 Умно — понимаю задачи и делаю лучшее решение\n"
            "🛡 Надёжно — стабильные боты на долгую перспективу\n\n"
            "Нажми «Начать» и опиши своего бота!"
        )
        await bot.set_my_short_description("Создаю Telegram-ботов по описанию за несколько минут")
    except Exception as e:
        logger.warning(f"Could not set description: {e}")

    await restore_bots()

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.update.outer_middleware(ManagedBotMiddleware(storage))

    dp.include_router(admin_router)
    dp.include_router(start_router)
    dp.include_router(create_router)
    dp.include_router(manage_router)
    dp.include_router(general_router)  # must be last — catch-all

    # ── group auto-detect ───────────────────────────────────────────────────────
    group_router = Router()

    @group_router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
    async def on_added_to_group(update: ChatMemberUpdated):
        chat = update.chat
        if chat.type not in ("group", "supergroup"):
            return
        user_id = update.from_user.id
        group_id = str(chat.id)
        group_name = chat.title or str(chat.id)
        bots = await get_all_bots()
        if not bots:
            await bot.send_message(
                user_id,
                f"Я добавлен в группу «{group_name}» (ID: <code>{group_id}</code>), но ботов пока нет. Создай бота через /create.",
                parse_mode="HTML",
            )
            return
        rows = [[InlineKeyboardButton(
            text=f"🤖 {b['name']}" + (f" ({b['display_name']})" if b.get("display_name") else ""),
            callback_data=f"setgroup:{b['id']}:{group_id}",
        )] for b in bots]
        rows.append([InlineKeyboardButton(text="✅ Всем ботам", callback_data=f"setgroup:all:{group_id}")])
        await bot.send_message(
            user_id,
            f"Я добавлен в группу <b>«{group_name}»</b>.\n\nДля каких ботов настроить эту группу?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @group_router.callback_query(lambda c: c.data and c.data.startswith("setgroup:"))
    async def cb_set_group(callback):
        await callback.answer()
        parts = callback.data.split(":")
        bot_target = parts[1]
        group_id = ":".join(parts[2:])
        bots = await get_all_bots()
        if bot_target == "all":
            targets = bots
        else:
            targets = [b for b in bots if str(b["id"]) == bot_target]
        from services.bot_runner import start_bot, stop_bot, is_running
        for b in targets:
            await set_bot_group(b["id"], group_id)
            if is_running(b["id"]) and b.get("token") and b.get("file_path"):
                await stop_bot(b["id"])
                extra = {"GROUP_CHAT_ID": group_id}
                if b.get("display_name"):
                    extra["BOT_DISPLAY_NAME"] = b["display_name"]
                try:
                    pid = await start_bot(b["id"], b["file_path"], b["token"], extra_env=extra)
                    await update_bot_status(b["id"], "running", pid)
                except Exception:
                    await update_bot_status(b["id"], "error")
        names = ", ".join(b["name"] for b in targets)
        await callback.message.edit_text(
            f"✅ Группа настроена для: <b>{names}</b>\n\nТеперь сделай этих ботов администраторами группы — тогда они смогут видеть все сообщения и отвечать на имя.",
            parse_mode="HTML",
        )

    dp.include_router(group_router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "inline_query",
            "my_chat_member",
            "managed_bot",  # Telegram Bot API 9.6+
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
