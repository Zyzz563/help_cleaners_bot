import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine, async_sessionmaker, AsyncSession

from app.db.models import Base
from app.config import Settings


def ensure_data_dir() -> None:
	Path("data").mkdir(parents=True, exist_ok=True)


def create_engine(settings: Settings) -> AsyncEngine:
	ensure_data_dir()
	engine = create_async_engine(settings.database_url, echo=(settings.env == "development"))
	return engine


def create_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
	return async_sessionmaker(engine, expire_on_commit=False)


async def run_startup_migrations(engine: AsyncEngine) -> None:
	# Minimal pragmatic migrations for SQLite to add new columns if missing
	async with engine.begin() as conn:
		# SQLite performance PRAGMAs if driver is sqlite
		url = str(conn.engine.url)
		if url.startswith("sqlite+") or url.startswith("sqlite:"):
			await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
			await conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
		# members.gender
		res = await conn.exec_driver_sql("PRAGMA table_info('members')")
		cols = [row[1] for row in res.fetchall()]
		if "gender" not in cols:
			await conn.exec_driver_sql("ALTER TABLE members ADD COLUMN gender VARCHAR(1) NULL")
		# groups.welcomed_at
		res = await conn.exec_driver_sql("PRAGMA table_info('groups')")
		gcols = [row[1] for row in res.fetchall()]
		if "welcomed_at" not in gcols:
			await conn.exec_driver_sql("ALTER TABLE groups ADD COLUMN welcomed_at TIMESTAMP NULL")
		# processed_callbacks
		await conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS processed_callbacks (id VARCHAR(64) PRIMARY KEY, created_at TIMESTAMP NOT NULL)")
		
		# photos.is_forwarded
		res = await conn.exec_driver_sql("PRAGMA table_info('photos')")
		pcols = [row[1] for row in res.fetchall()]
		if "is_forwarded" not in pcols:
			await conn.exec_driver_sql("ALTER TABLE photos ADD COLUMN is_forwarded BOOLEAN DEFAULT 0 NOT NULL")
		
		# Очистка старых дубликатов (старше 30 дней)
		from datetime import datetime, timedelta
		thirty_days_ago = (datetime.now() - timedelta(days=30)).date()
		try:
			await conn.exec_driver_sql("DELETE FROM photo_duplicates WHERE duplicate_date < ?", (thirty_days_ago,))
		except Exception:
			pass  # Таблица может не существовать при первом запуске
		
		# timesheets table for tracking confirmed work days
		await conn.exec_driver_sql("""
			CREATE TABLE IF NOT EXISTS timesheets (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				chat_id BIGINT NOT NULL,
				user_id BIGINT NOT NULL,
				date DATE NOT NULL,
				shift_type VARCHAR(10) NOT NULL,
				photo_count INTEGER NOT NULL,
				confirmed_at TIMESTAMP NOT NULL,
				UNIQUE(chat_id, user_id, date, shift_type)
			)
		""")
		
		await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_timesheet_chat_date ON timesheets (chat_id, date)")
		await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_timesheet_user_date ON timesheets (user_id, date)")
		
		# allowed_chats table for bot security
		await conn.exec_driver_sql("""
			CREATE TABLE IF NOT EXISTS allowed_chats (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				chat_id BIGINT UNIQUE NOT NULL,
				chat_title VARCHAR(255) NULL,
				added_by BIGINT NOT NULL,
				added_at TIMESTAMP NOT NULL,
				is_active BOOLEAN DEFAULT 1 NOT NULL
			)
		""")
		
		await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_allowed_chat_active ON allowed_chats (is_active)")
		
		# НЕ добавляем автоматически группы - только владелец может добавлять!
		# await conn.exec_driver_sql("""
		# 	INSERT INTO allowed_chats (chat_id, chat_title, added_by, added_at, is_active)
		# 	VALUES (?, ?, ?, ?, ?)
		# """, (chat_id, "Initial Group", 6405212136, datetime.utcnow(), True))
		
		# Migrate existing timesheets table to support separate day/night entries
		res = await conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='timesheets'")
		table_sql = res.fetchone()
		if table_sql and 'UNIQUE(chat_id, user_id, date)' in table_sql[0]:
			# Need to recreate table with new unique constraint including shift_type
			await conn.exec_driver_sql("ALTER TABLE timesheets RENAME TO timesheets_old")
			await conn.exec_driver_sql("""
				CREATE TABLE timesheets (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					chat_id BIGINT NOT NULL,
					user_id BIGINT NOT NULL,
					date DATE NOT NULL,
					shift_type VARCHAR(10) NOT NULL,
					photo_count INTEGER NOT NULL,
					confirmed_at TIMESTAMP NOT NULL,
					UNIQUE(chat_id, user_id, date, shift_type)
				)
			""")
			await conn.exec_driver_sql("INSERT INTO timesheets SELECT * FROM timesheets_old")
			await conn.exec_driver_sql("DROP TABLE timesheets_old")
			await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_timesheet_chat_date ON timesheets (chat_id, date)")
			await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_timesheet_user_date ON timesheets (user_id, date)")
		
		# managers table
		await conn.exec_driver_sql("""
			CREATE TABLE IF NOT EXISTS managers (
				user_id BIGINT PRIMARY KEY,
				display_name VARCHAR(64) NOT NULL,
				status VARCHAR(16) NOT NULL DEFAULT 'pending',
				requested_at TIMESTAMP NOT NULL,
				approved_by BIGINT NULL,
				approved_at TIMESTAMP NULL
			)
		""")
		# Pre-seed Наталья as approved manager
		await conn.exec_driver_sql("""
			INSERT OR IGNORE INTO managers (user_id, display_name, status, requested_at, approved_by, approved_at)
			VALUES (1974868265, 'Наталья', 'approved', CURRENT_TIMESTAMP, 6405212136, CURRENT_TIMESTAMP)
		""")

		# Update shifts.type column size to support "day_night"
		# SQLite doesn't support ALTER COLUMN, so we check if recreation is needed
		res = await conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='shifts'")
		table_sql = res.fetchone()
		if table_sql and 'VARCHAR(8)' in table_sql[0]:
			# Need to recreate table with larger VARCHAR
			await conn.exec_driver_sql("ALTER TABLE shifts RENAME TO shifts_old")
			await conn.exec_driver_sql("""
				CREATE TABLE shifts (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					chat_id BIGINT NOT NULL,
					user_id BIGINT NOT NULL,
					date DATE NOT NULL,
					type VARCHAR(10) NOT NULL,
					created_at TIMESTAMP NOT NULL,
					UNIQUE(chat_id, user_id, date)
				)
			""")
			await conn.exec_driver_sql("INSERT INTO shifts SELECT * FROM shifts_old")
			await conn.exec_driver_sql("DROP TABLE shifts_old")
			await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_shift_chat_date_type ON shifts (chat_id, date, type)")

		# vpn_orders table
		await conn.exec_driver_sql("""
			CREATE TABLE IF NOT EXISTS vpn_orders (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				user_id BIGINT NOT NULL,
				username VARCHAR(64) NULL,
				aaio_order_id VARCHAR(64) NULL UNIQUE,
				uuid VARCHAR(36) NULL,
				status VARCHAR(16) NOT NULL DEFAULT 'pending',
				traffic_limit_gb INTEGER NOT NULL DEFAULT 50,
				duration_days INTEGER NOT NULL DEFAULT 30,
				vless_link VARCHAR(1024) NULL,
				created_at TIMESTAMP NOT NULL,
				paid_at TIMESTAMP NULL,
				activated_at TIMESTAMP NULL
			)
		""")

		# Миграция: добавить aaio_order_id если таблица уже существует без него
		res = await conn.exec_driver_sql("PRAGMA table_info('vpn_orders')")
		vpn_cols = [row[1] for row in res.fetchall()]
		if "aaio_order_id" not in vpn_cols:
			await conn.exec_driver_sql("ALTER TABLE vpn_orders ADD COLUMN aaio_order_id VARCHAR(64) NULL") 