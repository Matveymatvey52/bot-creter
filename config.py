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

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in .env")
if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY is not set in .env")
