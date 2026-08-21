import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User

import aiosqlite

import db.database as db_module
from templates import survey_form
from templates.survey_form import (
    SurveyFormConfig, _create_survey, _fetch_export_rows, _get_active_survey,
    _is_admin, _load_admins, _save_admins, _save_response, _split_questions_locally,
    answer_text, cmd_start, init_db,
)


def _make_message(user_id: int, text: str) -> Message:
    return Message.model_construct(
        message_id=1,
        date=0,
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


class TestSurveyFormSmoke(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)
        self.bot_id = 111
        self.config = SurveyFormConfig(
            bot_name="bot1",
            db_path=str(data_dir / f"bot_{self.bot_id}_data.db"),
            admins_file=data_dir / f"admins_{self.bot_id}.json",
            welcome_image=data_dir / f"bot_{self.bot_id}.jpg",
        )
        await init_db(self.config.db_path)
        self.bot = Bot(token="123:fake")
        self.storage = MemoryStorage()

    async def asyncTearDown(self):
        self._bot_call_patcher.stop()
        await self.bot.session.close()
        self._tmp.cleanup()

    def test_split_questions_locally_strips_numbering(self):
        text = "1. Как вас зовут?\n2) Сколько вам лет?\n- Любимый цвет?\n\n"
        questions = _split_questions_locally(text)
        self.assertEqual(questions, ["Как вас зовут?", "Сколько вам лет?", "Любимый цвет?"])

    async def test_create_survey_deactivates_previous(self):
        survey_id_1 = await _create_survey(self.config.db_path, "Анкета 1", ["Q1?"])
        survey_id_2 = await _create_survey(self.config.db_path, "Анкета 2", ["Q1?", "Q2?"])

        active = await _get_active_survey(self.config.db_path)
        self.assertEqual(active["id"], survey_id_2)
        self.assertEqual(len(active["questions"]), 2)
        self.assertNotEqual(survey_id_1, survey_id_2)

    async def test_save_response_and_export_rows_align_with_questions(self):
        survey_id = await _create_survey(self.config.db_path, "Анкета", ["Имя?", "Возраст?"])
        survey = await _get_active_survey(self.config.db_path)
        q_ids = [q["id"] for q in survey["questions"]]

        await _save_response(
            self.config.db_path, survey_id, 42, "Alice",
            [(q_ids[0], "Alice"), (q_ids[1], "30")],
        )

        q_texts, rows = await _fetch_export_rows(self.config.db_path, survey_id)
        self.assertEqual(q_texts, ["Имя?", "Возраст?"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_name"], "Alice")
        self.assertEqual(rows[0]["answers"], ["Alice", "30"])

    async def test_answer_flow_end_to_end_via_handler(self):
        survey_id = await _create_survey(self.config.db_path, "Анкета", ["Q1?", "Q2?"])
        survey = await _get_active_survey(self.config.db_path)

        state = FSMContext(storage=self.storage, key=MagicMock(chat_id=1, user_id=1, bot_id=1))
        await state.update_data(
            survey_id=survey_id, questions=survey["questions"], idx=0, answers=[], started_at=__import__("time").time(),
        )

        msg1 = _make_message(1, "answer one")
        object.__setattr__(msg1, "_bot", self.bot)
        await answer_text(msg1, state, self.config, self.bot)
        data = await state.get_data()
        self.assertEqual(data["idx"], 1)

        msg2 = _make_message(1, "answer two")
        object.__setattr__(msg2, "_bot", self.bot)
        await answer_text(msg2, state, self.config, self.bot)

        q_texts, rows = await _fetch_export_rows(self.config.db_path, survey_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["answers"], ["answer one", "answer two"])
        final_state = await state.get_data()
        self.assertEqual(final_state, {})


class SurveyFormMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s
    real schema — miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in survey_form.miniapp_config["resources"]}
        self.assertEqual(names, {"surveys", "survey_responses"})

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await survey_form.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in survey_form.miniapp_config["resources"]:
                    cur = await db.execute(f"PRAGMA table_info({resource['table']})")
                    real_columns = {row[1] for row in await cur.fetchall()}
                    declared = {f["name"] for f in resource["fields"]} | {"id"}
                    self.assertTrue(
                        declared.issubset(real_columns),
                        f"{resource['name']}: declared fields {declared} not all in "
                        f"real columns {real_columns}",
                    )


class SurveyFormAdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: previously, whoever sent /start FIRST permanently became
    the bot admin — a client testing the bot link before the owner did would
    silently seize the admin panel. See tests/test_shop_catalog_isolation.py
    for the original of this fix, applied identically here."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

        self.bot = Bot(token="123:fake")
        self.storage = MemoryStorage()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._bot_call_patcher.stop()
        await self.bot.session.close()
        self._tmp.cleanup()

    def _config(self, bot_id: int, owner_telegram_id) -> SurveyFormConfig:
        config = SurveyFormConfig(
            bot_name=f"bot{bot_id}",
            db_path=str(self.data_dir / f"bot_{bot_id}_data.db"),
            admins_file=self.data_dir / f"admins_{bot_id}.json",
            welcome_image=self.data_dir / f"bot_{bot_id}.jpg",
        )
        config.bot_id = bot_id
        config.owner_telegram_id = owner_telegram_id
        return config

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = self._config(701, owner_telegram_id=12345)
        await init_db(config.db_path)
        state = FSMContext(storage=self.storage, key=MagicMock(chat_id=555, user_id=555, bot_id=701))

        CLIENT_ID = 555  # not the owner, messages first
        msg1 = _make_message(CLIENT_ID, "/start")
        object.__setattr__(msg1, "_bot", self.bot)
        await cmd_start(msg1, state, config)
        self.assertEqual(_load_admins(config.admins_file), set())
        self.assertFalse(_is_admin(CLIENT_ID, config))

        state2 = FSMContext(storage=self.storage, key=MagicMock(chat_id=12345, user_id=12345, bot_id=701))
        msg2 = _make_message(12345, "/start")
        object.__setattr__(msg2, "_bot", self.bot)
        await cmd_start(msg2, state2, config)
        self.assertTrue(_is_admin(12345, config))
        self.assertEqual(_load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = self._config(702, owner_telegram_id=777)
        await init_db(config.db_path)
        _save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(_is_admin(777, config))  # owner: always admin
        self.assertTrue(_is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(_is_admin(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = self._config(703, owner_telegram_id=None)
        await init_db(config.db_path)
        state = FSMContext(storage=self.storage, key=MagicMock(chat_id=321, user_id=321, bot_id=703))
        msg = _make_message(321, "/start")
        object.__setattr__(msg, "_bot", self.bot)
        await cmd_start(msg, state, config)

        central_admins = await db_module.get_bot_admins(703)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
