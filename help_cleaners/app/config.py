import os
from dataclasses import dataclass
from typing import Optional

# Railway handles environment variables automatically - no need for load_dotenv()

DEFAULT_TZ = "Europe/Moscow"
DEFAULT_DAY_START = "09:00"
DEFAULT_DAY_END = "21:00"
DEFAULT_NIGHT_START = "21:00"
DEFAULT_NIGHT_END = "09:00"
DEFAULT_REMINDER_INTERVAL_MIN = 45
DEFAULT_LUNCH_START = "13:00"
DEFAULT_LUNCH_END = "14:00"
MAX_NAME_LEN = 10

# 🔒 БЕЗОПАСНОСТЬ БОТА
OWNER_ID = int(os.getenv("OWNER_ID", "6405212136"))
ALLOWED_CHATS = [
    -4844509202,   # Disosks
    -5195004029,   # Devzis и CleaningBot
    -4845764165,
    -1003076688085,
    -1002529870711,
    -1002625004886,
    -1003407280086,
    1002582041447,
    -1003504309544,
]


@dataclass
class Settings:
    bot_token: str
    env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./data/dev.db"

    @staticmethod
    def from_env() -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            # Fallback for Railway deployment
            bot_token = "7669076544:AAF7D9FSNqzclEos9AP3NSDyZ0U3fjUsDbk"
        return Settings(
            bot_token=bot_token,
            env=os.getenv("ENV", "development"),
            database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/dev.db"),
        )


def default_group_config() -> dict:
    return {
        "tz": DEFAULT_TZ,
        "day_start": DEFAULT_DAY_START,
        "day_end": DEFAULT_DAY_END,
        "night_start": DEFAULT_NIGHT_START,
        "night_end": DEFAULT_NIGHT_END,
        "reminder_interval_min": DEFAULT_REMINDER_INTERVAL_MIN,
        "lunch_start": DEFAULT_LUNCH_START,
        "lunch_end": DEFAULT_LUNCH_END,
    }