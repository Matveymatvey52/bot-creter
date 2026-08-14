import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
GOOGLE_SHEETS_SA_KEY_PATH = os.getenv("GOOGLE_SHEETS_SA_KEY_PATH")
# Base64 of the same service-account JSON key, decoded in memory at runtime
# (never written to disk) — an alternative to GOOGLE_SHEETS_SA_KEY_PATH for
# environments where pasting raw JSON isn't reliable (e.g. Railway Console)
# but Railway Variables are. See features/sheets.py's _load_credentials_info().
GOOGLE_SHEETS_SA_KEY_B64 = os.getenv("GOOGLE_SHEETS_SA_KEY_B64")
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# userbot (Telethon Client API) — channel_monitor product. Deliberately
# separate secrets from the rest of the factory: a client-account session
# string is equivalent to a password to that Telegram account, and API_ID/
# API_HASH identify the application making the connection. See
# docs/USERBOT_CHANNEL_MONITOR_DESIGN.md §1.
USERBOT_ENCRYPTION_KEY = os.getenv("USERBOT_ENCRYPTION_KEY")
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in .env")
if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY is not set in .env")
