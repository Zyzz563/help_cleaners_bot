"""
Database initialization module for Railway deployment
"""
from sqlalchemy.ext.asyncio import AsyncEngine
from app.db.session import run_startup_migrations

async def init_db(engine: AsyncEngine) -> None:
    """Initialize database with migrations"""
    await run_startup_migrations(engine)
