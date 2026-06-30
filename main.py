import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from db.database import get_all_bots, init_db, update_bot_status
from handlers.create_bot import router as create_router
from handlers.manage_bots import router as manage_router
from handlers.start import router as start_router
from services.bot_runner import start_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


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
    await restore_bots()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(start_router)
    dp.include_router(create_router)
    dp.include_router(manage_router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
