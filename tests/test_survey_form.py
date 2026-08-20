import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User

import aiosqlite

from templates import survey_form
from templates.survey_form import (
    SurveyFormConfig, _create_survey, _fetch_export_rows, _get_active_survey,
    _save_response, _split_questions_locally, answer_text, init_db,
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


if __name__ == "__main__":
    unittest.main()
