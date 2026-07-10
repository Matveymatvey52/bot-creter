from __future__ import annotations

import json
import aiosqlite
from config import DATA_DIR

DB_PATH = DATA_DIR / "bots.db"
ADMINS_FILE = DATA_DIR / "admins.json"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT,
                description TEXT,
                token TEXT,
                file_path TEXT,
                status TEXT DEFAULT 'stopped',
                pid INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                bot_id INTEGER NOT NULL,
                telegram_id TEXT NOT NULL,
                PRIMARY KEY (bot_id, telegram_id)
            )
        """)
        for col in ("display_name TEXT", "group_chat_id TEXT"):
            try:
                await db.execute(f"ALTER TABLE bots ADD COLUMN {col}")
            except aiosqlite.OperationalError:
                pass
        await db.commit()


async def add_bot_admin(bot_id: int, telegram_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO bot_admins (bot_id, telegram_id) VALUES (?, ?)",
            (bot_id, telegram_id),
        )
        await db.commit()
    await sync_bot_admins_json(bot_id)


async def remove_bot_admin(bot_id: int, telegram_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM bot_admins WHERE bot_id = ? AND telegram_id = ?",
            (bot_id, telegram_id),
        )
        await db.commit()
    await sync_bot_admins_json(bot_id)


async def get_bot_admins(bot_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id FROM bot_admins WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def set_bot_group(bot_id: int, group_chat_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bots SET group_chat_id = ? WHERE id = ?", (group_chat_id, bot_id)
        )
        await db.commit()


async def set_bot_display_name(bot_id: int, display_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bots SET display_name = ? WHERE id = ?", (display_name, bot_id)
        )
        await db.commit()


async def sync_bot_admins_json(bot_id: int) -> None:
    b = await get_bot(bot_id)
    if not b:
        return
    ids = await get_bot_admins(bot_id)
    path = DATA_DIR / f"admins_{b['name']}.json"
    path.write_text(json.dumps({"ids": ids}, ensure_ascii=False))


async def create_bot_record(name: str, description: str, token: str, file_path: str, username: str | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO bots (name, username, description, token, file_path, status) VALUES (?, ?, ?, ?, ?, 'stopped')",
            (name, username, description, token, file_path),
        )
        await db.commit()
        return cursor.lastrowid


async def get_all_bots() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM bots ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_bot_by_name(name: str) -> dict | None:
    clean = name.lstrip("@").removesuffix("_bot")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bots WHERE name = ? OR username = ?",
            (clean, name.lstrip("@")),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_bot(bot_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def delete_bot(bot_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        await db.commit()


async def update_bot_username(bot_id: int, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bots SET username = ? WHERE id = ?", (username, bot_id))
        await db.commit()


async def update_bot_status(bot_id: int, status: str, pid: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if pid is not None:
            await db.execute(
                "UPDATE bots SET status = ?, pid = ? WHERE id = ?",
                (status, pid, bot_id),
            )
        else:
            await db.execute(
                "UPDATE bots SET status = ? WHERE id = ?",
                (status, bot_id),
            )
        await db.commit()
