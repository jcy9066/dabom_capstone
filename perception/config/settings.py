from dotenv import load_dotenv
from pathlib import Path

try:
    from perception.env_config import env_text
except ModuleNotFoundError:  # Direct perception script execution.
    from env_config import env_text

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

TELEGRAM_TOKEN = env_text("TELEGRAM_TOKEN", allow_empty=True)
TELEGRAM_CHAT_ID = env_text("TELEGRAM_CHAT_ID", allow_empty=True)
