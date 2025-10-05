import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.db.session import create_engine, create_session_maker
from app.db.models import Base
from app.handlers import commands_router, photos_router
from app.scheduler import scheduler
from app.middleware import FSMTimerMiddleware


async def on_startup(bot: Bot, engine: AsyncEngine):
	# Create tables and run lightweight migrations
	async with engine.begin() as conn:
		await conn.run_sync(Base.metadata.create_all)
	from app.db.session import run_startup_migrations
	await run_startup_migrations(engine)

	# Set bot commands
	bot_commands = [
		BotCommand(command="register", description="Зарегистрироваться как клинер"),
		BotCommand(command="cleaners", description="Все клинеры"),
		BotCommand(command="shift", description="Записаться на смену"),
		BotCommand(command="today", description="Кто сегодня работает"),
		BotCommand(command="myshifts", description="Мои смены"),
		BotCommand(command="add_shift", description="➕ Добавить смену в табель"),
		BotCommand(command="remove_shift", description="➖ Удалить смену из табеля"),
		BotCommand(command="tabel", description="📋 Мой табель за 30 дней"),
		BotCommand(command="duplicates", description="Список дубликатов фото"),
		BotCommand(command="remove", description="Удалить клинера"),
		BotCommand(command="reset", description="Сбросить состояние"),
		BotCommand(command="help", description="Помощь"),
		# 🔒 Команды безопасности (только для владельца)
		BotCommand(command="addchat", description="Добавить группу (владелец)"),
		BotCommand(command="removechat", description="Убрать группу (владелец)"),
		BotCommand(command="listchats", description="Список групп (владелец)"),
	]
	await bot.set_my_commands(bot_commands)


async def main():
	settings = Settings.from_env()

	# Logging level
	logging.basicConfig(level=logging.INFO)

	engine = create_engine(settings)
	SessionLocal = create_session_maker(engine)

	bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
	dp = Dispatcher(storage=MemoryStorage())

	# Provide session per-update via middleware
	class SessionMiddleware:
		def __init__(self, session_maker):
			self._SessionLocal = session_maker

		async def __call__(self, handler, event, data):
			async with self._SessionLocal() as session:
				data["session"] = session
				return await handler(event, data)

	# Attach session middleware globally
	dp.update.middleware(SessionMiddleware(SessionLocal))
	
	# Attach FSM timer middleware
	dp.update.middleware(FSMTimerMiddleware())

	dp.include_router(commands_router)
	dp.include_router(photos_router)

	scheduler.start()
	await on_startup(bot, engine)
	await dp.start_polling(bot, polling_timeout=20)


if __name__ == "__main__":
	asyncio.run(main())