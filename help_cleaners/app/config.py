import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

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
OWNER_ID = 6405212136  # ID владельца бота (только он может добавлять группы)
ALLOWED_CHATS = [
	# Добавьте сюда ID групп, где бот может работать
	-4844509202,  # Текущая группа где бот работает
	# -1001234567890,  # Пример группы
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
			raise RuntimeError("BOT_TOKEN is not set. Create a .env with BOT_TOKEN or export it in the environment.")
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