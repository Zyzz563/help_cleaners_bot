import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from app.handlers import commands_router, photos_router
from app.database import init_db

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = os.getenv("BOT_TOKEN") or "7669076544:AAF7D9FSNqzclEos9AP3NSDyZ0U3fjUsDbk"
OWNER_ID = int(os.getenv("OWNER_ID") or "6405212136")

logger.info(f"Bot token: {BOT_TOKEN[:10]}...")
logger.info(f"Owner ID: {OWNER_ID}")

# Initialize bot and dispatcher
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Database configuration
DATABASE_URL = "sqlite+aiosqlite:///./help_cleaners.db"
engine = create_async_engine(DATABASE_URL, echo=False)

# Include routers
dp.include_router(commands_router)
dp.include_router(photos_router)

async def on_startup(bot: Bot, engine: AsyncEngine):
    """Bot startup handler"""
    logger.info("Bot starting up...")
    await init_db(engine)
    logger.info("Database initialized")
    logger.info("Bot startup completed!")

async def on_shutdown(bot: Bot, engine: AsyncEngine):
    """Bot shutdown handler"""
    logger.info("Bot shutting down...")
    await engine.dispose()
    logger.info("Bot shutdown completed!")

async def main():
    """Main function"""
    logger.info("Starting bot...")
    
    try:
        # Startup
        await on_startup(bot, engine)
        
        # Start polling
        logger.info("Starting polling...")
        await dp.start_polling(bot)
        
    except Exception as e:
        logger.error(f"Error in main: {e}")
        raise
    finally:
        # Shutdown
        await on_shutdown(bot, engine)

if __name__ == "__main__":
    try:
        logger.info("Bot main function started")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        raise