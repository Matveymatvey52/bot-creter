"""Test-only fixture template — exercises features/payments.py the way a real
template would, without being a real template (deliberately NOT in templates/,
so it is never picked up by discover_templates()/the registry). booking_fitness
does not exist yet; this fixture stands in for "some template that sells
something" per the Phase B brief.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import LabeledPrice, Message

from features.payments import (
    create_invoice,
    init_payments_tables,
    on_pre_checkout_query,
    on_successful_payment,
    record_refund,
)

router = Router()


@dataclass
class FixtureConfig:
    bot_id: int
    db_path: str
    admins_file: Path | None = None


def _load_admins(admins_file: Path | None) -> set[str]:
    """Same read-only convention as templates/inventory.py's _load_admins —
    review flagged that a /refund with no admin check would let any user mark
    any charge refunded, a real risk since this fixture is what future paid
    templates will copy."""
    if not admins_file:
        return set()
    try:
        return set(json.loads(Path(admins_file).read_text()).get("ids", []))
    except Exception:
        return set()


class ConfigMiddleware(BaseMiddleware):
    def __init__(self, config: FixtureConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


async def init_db(db_path: str) -> None:
    await init_payments_tables(db_path)


router.pre_checkout_query.register(on_pre_checkout_query)
router.message.register(on_successful_payment, F.successful_payment)


@router.message(Command("buy"))
async def cmd_buy(msg: Message, config: FixtureConfig) -> None:
    try:
        await create_invoice(
            bot=msg.bot,
            bot_id=config.bot_id,
            chat_id=msg.chat.id,
            title="Тестовый товар",
            description="Фикстура для теста платежей",
            payload="fixture-item-1",
            currency="RUB",
            prices=[LabeledPrice(label="Тестовый товар", amount=10000)],
        )
    except ValueError:
        await msg.answer("⚠️ Оплата временно недоступна — обратитесь к администратору.")
    except TelegramBadRequest:
        await msg.answer("⚠️ Не удалось выставить счёт. Попробуйте ещё раз позже.")


@router.message(Command("refund"))
async def cmd_refund(msg: Message, config: FixtureConfig) -> None:
    if str(msg.from_user.id) not in _load_admins(config.admins_file):
        await msg.answer("⛔ Нет доступа")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.answer("Использование: /refund <charge_id>")
        return
    ok = await record_refund(config.db_path, parts[1].strip(), str(msg.from_user.id))
    if ok:
        await msg.answer("✅ Отмечено как возвращённый.")
    else:
        await msg.answer("⚠️ Не найдено или уже возвращено.")
