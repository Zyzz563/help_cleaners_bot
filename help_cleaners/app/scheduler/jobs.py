from __future__ import annotations

from datetime import datetime, date
from typing import List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, Shift, Member, ReminderJob, PhotoDuplicate
from app.utils.time import build_slots

scheduler = AsyncIOScheduler()


async def ensure_today_jobs(session: AsyncSession, bot: Bot, chat_id: int) -> None:
	group = await session.get(Group, chat_id)
	if not group:
		return
	# For MVP, only day slots for today if any shifts exist
	today = datetime.now().date()
	await schedule_jobs_for(session, bot, chat_id, today, "day")


async def schedule_jobs_for(session: AsyncSession, bot: Bot, chat_id: int, day: date, shift_type: str) -> None:
	group = await session.get(Group, chat_id)
	if not group:
		return

	window_start = group.day_start if shift_type == "day" else group.night_start
	window_end = group.day_end if shift_type == "day" else group.night_end
	slots = build_slots(
		tz_name=group.tz,
		day=day,
		window_start=window_start,
		window_end=window_end,
		interval_min=group.reminder_interval_min,
		lunch_start=group.lunch_start,
		lunch_end=group.lunch_end,
	)

	for slot_dt in slots:
		job_id = f"remind:{chat_id}:{day.isoformat()}:{shift_type}:{int(slot_dt.timestamp())}"
		if scheduler.get_job(job_id):
			continue
		scheduler.add_job(
			send_reminder,
			trigger="date",
			run_date=slot_dt,
			args=[bot, session.bind.url.render_as_string(hide_password=False), chat_id, day, shift_type, slot_dt],
			id=job_id,
			misfire_grace_time=60,
			coalesce=True,
		)


async def send_reminder(bot: Bot, database_url: str, chat_id: int, day: date, shift_type: str, slot_dt: datetime) -> None:
	# Lazy import to create a new session because jobs run outside request context
	from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
	engine = create_async_engine(database_url)
	SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
	async with SessionLocal() as session:
		crew = await _get_crew(session, chat_id, day, shift_type)
		if not crew:
			return
		text = f"Обход зала, отметить фото в чат. Сейчас: {slot_dt.strftime('%H:%M')}\n" + ", ".join(crew[:3]) + (" и команда" if len(crew) > 3 else "")
		await bot.send_message(chat_id, text)


async def _get_crew(session: AsyncSession, chat_id: int, day: date, shift_type: str) -> List[str]:
	q = (
		select(Member.display_name)
		.join(Shift, (Shift.chat_id == Member.chat_id) & (Shift.user_id == Member.user_id))
		.where(Shift.chat_id == chat_id, Shift.date == day, Shift.type == shift_type)
	)
	rows = (await session.execute(q)).scalars().all()
	return rows


async def cleanup_old_duplicates() -> None:
	"""Очистка дубликатов старше 30 дней"""
	from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
	from app.config import Settings
	
	settings = Settings.from_env()
	engine = create_async_engine(settings.database_url)
	SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
	
	async with SessionLocal() as session:
		try:
			from datetime import timedelta
			thirty_days_ago = (datetime.now() - timedelta(days=30)).date()
			
			# Удаляем дубликаты старше 30 дней
			result = await session.execute(
				select(PhotoDuplicate).where(PhotoDuplicate.duplicate_date < thirty_days_ago)
			)
			old_duplicates = result.scalars().all()
			
			if old_duplicates:
				for dup in old_duplicates:
					await session.delete(dup)
				await session.commit()
				print(f"Очищено {len(old_duplicates)} старых дубликатов")
			else:
				print("Старых дубликатов для очистки не найдено")
				
		except Exception as e:
			print(f"Ошибка при очистке дубликатов: {e}")
		finally:
			await engine.dispose()


# Добавляем задачу очистки старых дубликатов каждый день в 3:00
scheduler.add_job(
	cleanup_old_duplicates,
	trigger="cron",
	hour=3,
	minute=0,
	id="cleanup_duplicates_daily",
	misfire_grace_time=3600,  # 1 час grace time
	coalesce=True,
) 