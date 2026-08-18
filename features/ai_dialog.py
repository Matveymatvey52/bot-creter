"""Tool schemas / system prompt / preview text for the "✨🎙️ Хочу
наговорить/написать что изменить" free-form voice/text dialog entry point on
the bot detail panel (handlers/manage_bots.py's AiDialogStates.chatting).

Not a toggleable bot feature like features/group_task.py or
features/sheets.py — this lives on the CREATOR bot's OWN management chat,
addressing a bot the owner is already looking at in its detail panel, not
inside a generated bot's own process. There's no per-bot enable/disable:
every owner viewing their own bot's panel gets this button. Kept as its own
module anyway (rather than inline in manage_bots.py) purely to keep that
already-2000+-line file from growing a second unrelated concern, and so this
module can stay import-cycle-free: manage_bots.py owns the actual
Claude-call/FSM/tool-execution wiring and its own _perform_start/_perform_
stop/_perform_restart/_perform_autofix helpers, this module only describes
what the tools ARE.

Design reference: same tool-use shape as features/group_task.py — Claude
proposes AT MOST one tool call per turn, it is turned into a preview text
with ✅/❌ buttons, and NOTHING executes until the owner taps ✅ (see
handlers/manage_bots.py's cb_aidialog_confirm). v1 tool surface is
deliberately limited to the cheapest/most-reversible of the detail panel's
~15 actions — stop/start/restart/autofix/show_logs. Explicitly NOT exposed
here: delete (irreversible), recreate/fixbug (need a free-text description
of their own, awkward to also drive through a tool call), features/offices/
payments (multi-step wizards with their own FSM flows). Extending this list
is a deliberate v2 step, not something to grow ad hoc.
"""
from __future__ import annotations

TOOLS: list[dict] = [
    {
        "name": "stop_bot",
        "description": (
            "Остановить (выключить) этого бота. Бот перестаёт отвечать пользователям, "
            "пока его не запустят снова."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "start_bot",
        "description": "Запустить (включить) этого бота, если он сейчас остановлен.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "restart_bot",
        "description": (
            "Перезапустить этого бота (остановить и сразу запустить снова) — например, "
            "если он завис или работает некорректно."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_autofix",
        "description": (
            "Запустить автоматическую диагностику и исправление кода бота — анализирует "
            "последние логи ошибок (или сам код, если логов нет), находит и чинит баги, "
            "затем перезапускает бота. Используй, когда владелец жалуется что бот не "
            "отвечает, падает или работает неправильно, и не знает точную причину."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "show_logs",
        "description": (
            "Показать последние логи этого бота. Только чтение, ничего не меняет в боте."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

# Tools that run immediately on the model's tool_use, with no ✅/❌
# confirmation step — read-only, nothing to undo, same rationale as the
# design doc's "logs — no confirmation" call. Every other tool above changes
# the bot's running/deployed state and goes through the confirm flow.
READONLY_TOOLS: frozenset[str] = frozenset({"show_logs"})

SYSTEM_PROMPT_TEMPLATE = (
    "Ты — ассистент владельца фабрики ботов. Он сейчас в панели управления конкретным "
    "ботом «{name}» (шаблон: {template_id}, сейчас {status}) и написал или наговорил "
    "голосом, что хочет с ним сделать. У тебя есть инструменты, которые реально "
    "управляют этим ботом — вызывай РОВНО ОДИН инструмент, только если просьба явно и "
    "однозначно соответствует одному из них. Если просьба не подходит ни под один "
    "инструмент (например, сменить оплату, добавить или изменить фичу, поменять текст "
    "или логику бота, подключить таблицы/офисы) — НЕ вызывай инструмент, а кратко "
    "объясни словами, какой кнопкой в панели бота для этого нужно воспользоваться "
    "(например «🧩 Фичи», «💳 Как подключить оплату», «🐛 Исправить баг», "
    "«🔄 Перегенерировать», «🏢 Офисы»), и не пытайся выполнить это через инструменты. "
    "Если просьба неоднозначна или мало данных — уточни вопросом, не вызывай "
    "инструмент наугад. Отвечай кратко, по-русски."
)


def describe_tool_call(tool_name: str, bot_name: str) -> str:
    """Best-effort human preview for the ✅/❌ confirmation message — same
    "must never raise" contract as group_task.py's _describe_tool_call.
    Every current tool is parameterless (they all act on "this bot", already
    known from FSM state), so this is simpler than group_task's; kept as its
    own function so a future parameterized tool has a place to grow into."""
    if tool_name == "stop_bot":
        return f"🔴 Остановить бота «{bot_name}»"
    if tool_name == "start_bot":
        return f"🟢 Запустить бота «{bot_name}»"
    if tool_name == "restart_bot":
        return f"🔁 Перезапустить бота «{bot_name}»"
    if tool_name == "run_autofix":
        return f"🔍 Запустить авто-диагностику и исправление бота «{bot_name}» (может занять пару минут)"
    if tool_name == "show_logs":
        return f"📋 Показать логи бота «{bot_name}»"
    return f"⚙️ {tool_name}"
