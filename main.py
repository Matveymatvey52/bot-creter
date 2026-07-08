import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from config import BOT_TOKEN
from db.database import get_all_bots, init_db, update_bot_status
from handlers.create_bot import auto_launch_managed_bot, router as create_router, set_bot_id, set_manager_username
from handlers.manage_bots import router as manage_router
from handlers.start import router as start_router
from services.bot_runner import start_bot

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
    bots = await get_all_bots()
    for bot in bots:
        if bot["status"] == "running" and bot["file_path"] and bot["token"]:
            try:
                pid = await start_bot(bot["id"], bot["file_path"], bot["token"])
                await update_bot_status(bot["id"], "running", pid)
                logger.info(f"Restored bot '{bot['name']}' (ID: {bot['id']}, PID: {pid})")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Failed to restore bot '{bot['name']}' (ID: {bot['id']}): {e}")
                await update_bot_status(bot["id"], "error")


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

    dp.include_router(start_router)
    dp.include_router(create_router)
    dp.include_router(manage_router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "inline_query",
            "managed_bot",  # Telegram Bot API 9.6+
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
