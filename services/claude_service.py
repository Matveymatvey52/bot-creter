from anthropic import AsyncAnthropic
from config import ANTHROPIC_API_KEY

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

GATHER_SYSTEM_PROMPT = """You are a Telegram bot development assistant. Your job is to understand what bot the user wants to create.

Ask 1-2 concise clarifying questions at a time to understand:
- The bot's main purpose and functionality
- Key commands or features needed
- Any specific behaviors (stores data per user, sends notifications, etc.)

When you have enough information (usually after 2-4 exchanges), output exactly:
===READY_TO_GENERATE===
[Structured summary of the bot to build, in English, with all key requirements]

Always respond in the same language as the user. Keep questions short."""

GENERATE_SYSTEM_PROMPT = """You are an expert Python developer specializing in Telegram bots using aiogram 3.

Generate a complete, working Python bot file based on the requirements.

Rules:
- Use aiogram 3.x (Bot, Dispatcher, Router, FSM if needed)
- Single self-contained file
- Read token: os.getenv("BOT_TOKEN")
- Include ALL handlers, commands, and logic
- Use async/await throughout
- Include basic error handling
- Use FSM for multi-step conversations if needed
- Include logging setup at the top

Return ONLY valid Python code, no markdown fences, no explanations."""


async def chat_gather_requirements(conversation: list[dict]) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=GATHER_SYSTEM_PROMPT,
        messages=conversation,
    )
    return response.content[0].text


async def extract_bot_name(requirements_summary: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system="Extract a short snake_case bot filename (no 'bot' suffix, max 20 chars, only a-z 0-9 _). Return ONLY the name, nothing else.",
        messages=[{"role": "user", "content": requirements_summary}],
    )
    raw = response.content[0].text.strip().lower()
    name = "".join(c for c in raw.replace(" ", "_") if c.isalnum() or c == "_")[:20]
    return name or "my_bot"


async def generate_bot_code(requirements_summary: str) -> str:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=GENERATE_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Create a Telegram bot with these requirements:\n\n{requirements_summary}",
            }
        ],
    )
    return response.content[0].text
