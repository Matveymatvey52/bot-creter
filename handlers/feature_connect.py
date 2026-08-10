from __future__ import annotations

import functools
import logging
import re
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import handlers.manage_bots as manage_bots
from db.database import get_all_bots, get_bot, get_bot_features, get_bot_sheets_config
from handlers.admin_manager import _is_owner
from runtime.registry import discover_features, infer_template_id
from services.claude_service import classify_connect_feature_intent

logger = logging.getLogger(__name__)

router = Router()

# ── entry point (called from handlers/general.py, NOT registered as a message
#    handler here) ────────────────────────────────────────────────────────────
#
# handlers/general.py's general_text/general_voice are the single catch-all
# funnel for owner conversation (StateFilter(None), i.e. no other flow —
# /create, sheets-link entry, fixbug description, admin-id entry — is
# currently mid-flow). Both already gate on _is_owner() and transcribe voice
# the same way before reaching plain text, so maybe_handle_connect_request()
# below inherits that gating for free and only ever needs to worry about text.
#
# This module deliberately owns no message handler / router-level filter of
# its own for text — the cost-control requirement ("classifier must not run
# on every message") is enforced by general.py calling this function and
# checking its return value, not by aiogram filter ordering.

_FEATURE_LABELS: dict[str, str] = {
    "sheets": "📊 Google Таблицы",
    "payments": "💳 Оплата (Telegram Payments)",
}

_FEATURE_EXPLANATIONS: dict[str, str] = {
    "sheets": (
        "📊 <b>Google Таблицы</b>\n\n"
        "Бот сможет читать и писать данные в подключённую Google Таблицу через "
        "общий сервисный аккаунт фабрики — один и тот же для всех ботов на "
        "платформе, не отдельный робот лично под этого бота.\n\n"
        "После подтверждения я пришлю email сервисного аккаунта и попрошу "
        "ссылку на таблицу."
    ),
    "payments": (
        "💳 <b>Оплата (Telegram Payments)</b>\n\n"
        "Бот сможет принимать оплату через Telegram Payments — приём платежей "
        "включится для этого бота сразу после подтверждения."
    ),
}

# Cheap, local, synchronous pre-filter — runs on EVERY owner message that
# reaches general.py's catch-all, BEFORE any Claude call. Its only job is cost
# control (задача 4): ordinary factory chit-chat, /create-style bot
# descriptions, and unrelated questions must never reach the classifier. A
# false negative here just means "handled by ask_assistant as before" (safe —
# no functional loss for a genuine connect request that happens to use
# unusual wording, since the classifier's precision, not this regex's, is
# what determines correctness). A false positive costs one extra Haiku call,
# not a wrong action — classify_connect_feature_intent() itself is the
# precision gate; this regex is only ever a cost gate.
#
# Deliberately NOT gated on bare action verbs alone (подключ.../включи/
# отключи) — those are common, generic Russian verbs ("подключи админа",
# "включи бота") that show up constantly in unrelated factory chatter and
# would defeat the point of a cost gate if they alone were enough to fire the
# classifier. A genuine connect request always also names the feature
# ("таблицы"/"sheets"/"оплату"/"платежи") or says "фича"/"feature" outright —
# so the gate requires one of those, optionally combined with an action verb.
_FEATURE_WORD_RE = re.compile(r"таблиц|sheet|оплат|плат[её]ж|payment", re.IGNORECASE)
_ACTION_WORD_RE = re.compile(r"подключ|включи|отключ|connect", re.IGNORECASE)
_FEATURE_MENTION_RE = re.compile(r"фич|feature", re.IGNORECASE)


def _looks_like_feature_request(text: str | None) -> bool:
    if not text:
        return False
    if _FEATURE_WORD_RE.search(text):
        return True
    return bool(_ACTION_WORD_RE.search(text) and _FEATURE_MENTION_RE.search(text))


def _bot_label(b: dict) -> str:
    parts = [b.get("name") or "bot"]
    if b.get("username"):
        parts.append(f"(@{b['username']})")
    if b.get("display_name"):
        parts.append(f"— {b['display_name']}")
    label = " ".join(parts)
    # Telegram caps inline button text at 64 chars — same truncation
    # convention as manage_bots.py's sheet-title button label.
    if len(label) > 60:
        label = label[:59] + "…"
    return label


def _match_bots(bots: list[dict], query: str) -> list[dict]:
    q = query.strip().lstrip("@").lower()
    if not q:
        return list(bots)

    def fields(b: dict) -> list[str]:
        return [
            (b.get("name") or "").lower(),
            (b.get("username") or "").lower(),
            (b.get("display_name") or "").lower(),
        ]

    exact = [b for b in bots if q in fields(b)]
    if exact:
        return exact
    return [b for b in bots if any(f and q in f for f in fields(b))]


def _bots_list_text(bots: list[dict]) -> str:
    return "\n".join(f"• {_bot_label(b)}" for b in bots)


_GENERIC_FAILURE_TEXT = "Что-то пошло не так, попробуй ещё раз."


async def _guarded(respond, step) -> None:
    """Runs `step` (a zero-arg async callable), degrading to a generic failure
    message on any unexpected error — DB lock, TelegramForbiddenError from
    `respond` itself further down the chain, etc. — instead of letting it
    propagate unhandled out of a message/callback handler. This is the same
    graceful-fallback contract handlers/general.py's ask_assistant() call
    already has (try/except -> a "что-то пошло не так" message), extended to
    every DB-touching step of the conversational connect flow: the preview,
    the disambiguation lookups, and the confirm/cancel actions all go through
    this, not just the classifier call (which already had its own try/except).
    `respond` must be the same sink already used to talk to the owner in this
    step; if even sending the failure message fails, that's swallowed too —
    there's nothing further to do."""
    try:
        await step()
    except Exception as e:
        logger.error(f"feature-connect step failed: {e}")
        try:
            await respond(_GENERIC_FAILURE_TEXT)
        except Exception:
            pass


# Cheap in-memory dedup so someone spamming the exact same text (or a client
# retry / Telegram redelivering an update) doesn't spend a fresh classifier
# call per repeat — задача 4's cost-control requirement extended to repeats,
# not just to unrelated chatter. Keyed by user_id (this hook only ever runs
# for the single _is_owner() user, so this dict never holds more than one
# entry in practice) -> (last text sent through the classifier, monotonic
# timestamp). A different text from the same user is never suppressed —
# only an exact repeat within the cooldown window is.
_CLASSIFY_COOLDOWN_SECONDS = 15.0
_last_classify_call: dict[int, tuple[str, float]] = {}


def _is_duplicate_within_cooldown(user_id: int, text: str) -> bool:
    now = time.monotonic()
    last = _last_classify_call.get(user_id)
    if last is not None and last[0] == text and now - last[1] < _CLASSIFY_COOLDOWN_SECONDS:
        return True
    _last_classify_call[user_id] = (text, now)
    return False


async def maybe_handle_connect_request(message: Message, text: str) -> bool:
    """Called by handlers/general.py's general_text/general_voice AFTER their own
    _is_owner() + StateFilter(None) gating, with the already-resolved plain text
    (transcribed already, for voice). Returns True if this message was claimed
    and handled as a feature-connect request — caller must NOT also call
    ask_assistant for it. Returns False for anything else, meaning "not this,
    handle as normal conversation" — the caller's existing ask_assistant() path
    runs completely unchanged in that case.

    No FSMContext is needed here: disambiguation/preview are pure button
    round-trips (state is threaded through callback_data, not FSM state) — the
    only step that touches the FSM is the confirm callback's sheets branch
    (cb_fconn_confirm below), which receives its own `state` directly from
    aiogram, same as any other callback handler.
    """
    if not _looks_like_feature_request(text):
        return False
    if _is_duplicate_within_cooldown(message.from_user.id, text):
        return False
    try:
        intent = await classify_connect_feature_intent(text)
    except Exception as e:
        logger.error(f"classify_connect_feature_intent failed: {e}")
        return False
    if not intent["is_connect_request"]:
        return False
    await _guarded(
        message.answer,
        lambda: _start_connect_flow(message.answer, intent["feature"], intent["bot_query"]),
    )
    return True


async def _start_connect_flow(send_fn, feature: str | None, bot_query: str | None) -> None:
    bots = await get_all_bots()
    if not bots:
        await send_fn("У тебя пока нет ни одного бота — сначала создай через /create.")
        return

    candidates = _match_bots(bots, bot_query) if bot_query else list(bots)
    if not candidates:
        await send_fn(
            f"Не нашёл бота с именем «{bot_query}». Вот твои боты:\n{_bots_list_text(bots)}"
        )
        return

    if len(candidates) > 1:
        await _ask_which_bot(send_fn, feature, candidates)
        return

    await _proceed(send_fn, candidates[0]["id"], feature)


async def _ask_which_bot(send_fn, feature: str | None, candidates: list[dict]) -> None:
    feat_token = feature or "_"
    rows = [
        [InlineKeyboardButton(text=_bot_label(b), callback_data=f"fconn_bot:{feat_token}:{b['id']}")]
        for b in candidates
    ]
    await send_fn(
        "У тебя несколько подходящих ботов — какой из них?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _ask_which_feature(send_fn, bot_id: int) -> None:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"fconn_feat:{bot_id}:{name}")]
        for name, label in _FEATURE_LABELS.items()
    ]
    await send_fn("Какую фичу подключить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _proceed(send_fn, bot_id: int, feature: str | None) -> None:
    if feature is None:
        await _ask_which_feature(send_fn, bot_id)
        return
    await _present_preview_or_refusal(send_fn, bot_id, feature)


async def _present_preview_or_refusal(send_fn, bot_id: int, feature: str) -> None:
    b = await get_bot(bot_id)
    if not b:
        await send_fn("Бот не найден.")
        return

    template_id = infer_template_id(b.get("file_path"))
    feat = next((f for f in discover_features() if f["name"] == feature), None)
    if feat is None or template_id not in feat["compatible_with"]:
        await send_fn(
            f"⛔ Фича «{_FEATURE_LABELS.get(feature, feature)}» не подходит боту "
            f"«{b['name']}» — этот тип бота её не поддерживает."
        )
        return

    enabled = set(await get_bot_features(bot_id))
    if feature == "payments" and "payments" in enabled:
        await send_fn(f"ℹ️ У бота «{b['name']}» оплата уже подключена.")
        return
    if feature == "sheets" and "sheets" in enabled:
        cfg = await get_bot_sheets_config(bot_id)
        if cfg:
            title = cfg.get("sheet_title") or cfg.get("spreadsheet_id")
            await send_fn(f"ℹ️ У бота «{b['name']}» уже подключена таблица «{title}».")
            return
        # sheets was toggled on in the DB but never actually linked to a
        # spreadsheet (e.g. abandoned mid-flow) — fall through to the normal
        # preview/confirm so the owner can finish connecting it.

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подключить", callback_data=f"fconn_confirm:{bot_id}:{feature}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="fconn_cancel"),
    ]])
    await send_fn(
        f"Бот: <b>{b['name']}</b>\n\n{_FEATURE_EXPLANATIONS[feature]}",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _do_confirm(bot_id: int, feature: str, state: FSMContext, respond) -> None:
    """The actual confirm action, factored out of cb_fconn_confirm so it can be
    run through _guarded() below without a second copy of the try/except
    shape. `respond` is the same edit-in-place sink the callback handler
    passes in — see cb_fconn_confirm."""
    b = await get_bot(bot_id)
    if not b:
        await respond("Бот не найден.")
        return

    # Re-verified here (not just trusted from the preview step) — the
    # preview could be stale by the time the owner taps confirm (feature
    # toggled off/on again meanwhile via the button UI, template changed,
    # etc.), same defensive re-check cb_toggle_feature already does.
    template_id = infer_template_id(b.get("file_path"))
    feat = next((f for f in discover_features() if f["name"] == feature), None)
    if feat is None or template_id not in feat["compatible_with"]:
        await respond("⛔ Эта фича не подходит шаблону этого бота.")
        return

    enabled = set(await get_bot_features(bot_id))
    already = feature in enabled
    if feature == "payments" and already:
        await respond("ℹ️ Оплата уже подключена.")
        return
    if feature == "sheets" and already:
        cfg = await get_bot_sheets_config(bot_id)
        if cfg:
            await respond("ℹ️ Таблица уже подключена.")
            return

    # Identical call cb_toggle_feature's "turn on" branch makes — see
    # handlers/manage_bots.py's enable_feature_and_reload().
    if not already:
        await manage_bots.enable_feature_and_reload(bot_id, feature)

    if feature == "sheets":
        # Identical call cb_sheets_connect_start makes — see
        # handlers/manage_bots.py's begin_sheets_connect(). Feeds into the
        # SAME SheetsConnectFlow.waiting_for_link state, handled by the
        # SAME msg_sheets_connect_link, regardless of entry point.
        await manage_bots.begin_sheets_connect(bot_id, state, respond)
    else:
        await respond(f"✅ Оплата подключена для бота «{b['name']}».")


# ── callbacks ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fconn_bot:"))
async def cb_fconn_bot(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await manage_bots._deny_callback(callback)
        return
    _, feat_token, bot_id_str = callback.data.split(":", 2)
    bot_id = int(bot_id_str)
    if bot_id in manage_bots._busy_bots:
        await callback.answer(manage_bots._BUSY_TEXT, show_alert=True)
        return
    manage_bots._busy_bots.add(bot_id)
    try:
        await callback.answer()
        feature = None if feat_token == "_" else feat_token
        await _guarded(callback.message.answer, lambda: _proceed(callback.message.answer, bot_id, feature))
    finally:
        manage_bots._busy_bots.discard(bot_id)


@router.callback_query(F.data.startswith("fconn_feat:"))
async def cb_fconn_feat(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await manage_bots._deny_callback(callback)
        return
    _, bot_id_str, feature = callback.data.split(":", 2)
    bot_id = int(bot_id_str)
    if bot_id in manage_bots._busy_bots:
        await callback.answer(manage_bots._BUSY_TEXT, show_alert=True)
        return
    manage_bots._busy_bots.add(bot_id)
    try:
        await callback.answer()
        await _guarded(
            callback.message.answer,
            lambda: _present_preview_or_refusal(callback.message.answer, bot_id, feature),
        )
    finally:
        manage_bots._busy_bots.discard(bot_id)


@router.callback_query(F.data.startswith("fconn_confirm:"))
async def cb_fconn_confirm(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await manage_bots._deny_callback(callback)
        return
    _, bot_id_str, feature = callback.data.split(":", 2)
    bot_id = int(bot_id_str)

    if bot_id in manage_bots._busy_bots:
        await callback.answer(manage_bots._BUSY_TEXT, show_alert=True)
        return
    manage_bots._busy_bots.add(bot_id)
    try:
        await callback.answer()
        # Edits the tapped message in place (same _edit_or_resend helper the
        # button-driven panels in manage_bots.py already use) instead of
        # sending a new message — the preview's own "✅ Подключить"/
        # "❌ Отмена" buttons must stop being live once acted on, otherwise a
        # second tap after cancelling would still silently re-run the confirm.
        respond = functools.partial(manage_bots._edit_or_resend, callback)
        await _guarded(respond, lambda: _do_confirm(bot_id, feature, state, respond))
    finally:
        manage_bots._busy_bots.discard(bot_id)


@router.callback_query(F.data == "fconn_cancel")
async def cb_fconn_cancel(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await manage_bots._deny_callback(callback)
        return
    await callback.answer()
    # Edit in place, same reasoning as cb_fconn_confirm above — removes the
    # preview's buttons instead of leaving them live under a new message.
    await manage_bots._edit_or_resend(callback, "Отменено.")
