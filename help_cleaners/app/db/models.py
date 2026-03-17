from __future__ import annotations

from datetime import date, time, datetime
from typing import Optional

from sqlalchemy import (
	BigInteger,
	Boolean,
	Column,
	Date,
	DateTime,
	Enum,
	ForeignKey,
	Index,
	Integer,
	String,
	Time,
	UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, Mapped, mapped_column

Base = declarative_base()


class Group(Base):
	__tablename__ = "groups"

	chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
	title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
	tz: Mapped[str] = mapped_column(String(64), default="Europe/Moscow", nullable=False)
	day_start: Mapped[time] = mapped_column(Time, nullable=False)
	day_end: Mapped[time] = mapped_column(Time, nullable=False)
	night_start: Mapped[time] = mapped_column(Time, nullable=False)
	night_end: Mapped[time] = mapped_column(Time, nullable=False)
	reminder_interval_min: Mapped[int] = mapped_column(Integer, default=45, nullable=False)
	lunch_start: Mapped[time] = mapped_column(Time, nullable=False)
	lunch_end: Mapped[time] = mapped_column(Time, nullable=False)
	updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
	welcomed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Member(Base):
	__tablename__ = "members"
	__table_args__ = (
		UniqueConstraint("chat_id", "user_id", name="uq_members_chat_user"),
	)

	chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.chat_id"), primary_key=True)
	user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
	display_name: Mapped[str] = mapped_column(String(10), nullable=False)
	role: Mapped[str] = mapped_column(String(16), default="cleaner", nullable=False)  # cleaner/admin
	registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
	gender: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)  # 'm'|'f' or None

	# Best-effort case-insensitive uniqueness within group via index; enforced strictly in application logic
	__table_args__ = (
		UniqueConstraint("chat_id", "user_id", name="uq_members_chat_user"),
		Index("ix_members_chat_display_lower", "chat_id", "display_name"),
	)


class Shift(Base):
	__tablename__ = "shifts"

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
	user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
	date: Mapped[date] = mapped_column(Date, nullable=False)
	type: Mapped[str] = mapped_column(String(10), nullable=False)  # day/night
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

	__table_args__ = (
		UniqueConstraint("chat_id", "user_id", "date", name="uq_shift_unique_per_day"),
		Index("ix_shift_chat_date_type", "chat_id", "date", "type"),
	)


class Photo(Base):
	__tablename__ = "photos"

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
	user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
	date: Mapped[date] = mapped_column(Date, nullable=False)
	time: Mapped[time] = mapped_column(Time, nullable=False)
	file_id: Mapped[str] = mapped_column(String(256), nullable=False)
	file_unique_id: Mapped[str] = mapped_column(String(128), nullable=False)
	phash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	caption: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
	is_forwarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

	__table_args__ = (
		Index("ix_photos_chat_date", "chat_id", "date"),
	)


class ReminderJob(Base):
	__tablename__ = "reminder_jobs"

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
	date: Mapped[date] = mapped_column(Date, nullable=False)
	type: Mapped[str] = mapped_column(String(8), nullable=False)  # day/night
	status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")  # scheduled/paused/done
	last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

	__table_args__ = (
		UniqueConstraint("chat_id", "date", "type", name="uq_reminder_unique"),
	) 


class PhotoDuplicate(Base):
	__tablename__ = "photo_duplicates"

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
	original_photo_id: Mapped[int] = mapped_column(Integer, ForeignKey("photos.id"), nullable=False)
	duplicate_photo_id: Mapped[int] = mapped_column(Integer, ForeignKey("photos.id"), nullable=False)
	original_date: Mapped[date] = mapped_column(Date, nullable=False)
	original_time: Mapped[time] = mapped_column(Time, nullable=False)
	duplicate_date: Mapped[date] = mapped_column(Date, nullable=False)
	duplicate_time: Mapped[time] = mapped_column(Time, nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

	__table_args__ = (
		Index("ix_photo_dupes_chat_created", "chat_id", "created_at"),
	) 


class ProcessedCallback(Base):
	__tablename__ = "processed_callbacks"

	id: Mapped[str] = mapped_column(String(64), primary_key=True)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False) 


class Timesheet(Base):
	"""
	Табель - подтвержденные рабочие дни.
	Запись создается только когда клинер:
	1. Выбрал смену (day/night)
	2. Отправил минимум 10 фото за рабочий день
	"""
	__tablename__ = "timesheets"

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
	user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
	date: Mapped[date] = mapped_column(Date, nullable=False)
	shift_type: Mapped[str] = mapped_column(String(10), nullable=False)  # day/night
	photo_count: Mapped[int] = mapped_column(Integer, nullable=False)  # количество фото за день
	confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

	__table_args__ = (
		UniqueConstraint("chat_id", "user_id", "date", "shift_type", name="uq_timesheet_unique_per_day_shift"),
		Index("ix_timesheet_chat_date", "chat_id", "date"),
		Index("ix_timesheet_user_date", "user_id", "date"),
	)


class Manager(Base):
	"""
	Менеджеры бота (глобально, не привязаны к группе).
	Могут управлять табелем, дубликатами, списком клинеров.
	Статус: pending → approved / rejected (владелец подтверждает).
	"""
	__tablename__ = "managers"

	user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
	display_name: Mapped[str] = mapped_column(String(64), nullable=False)
	status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending/approved/rejected
	requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
	approved_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
	approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class VpnOrder(Base):
	"""
	Заказы VPN — отслеживание покупок.
	Статус: pending → paid → active / rejected / error / expired
	"""
	__tablename__ = "vpn_orders"

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
	username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	aaio_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
	uuid: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
	status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
	traffic_limit_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
	duration_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
	vless_link: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
	paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
	activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

	__table_args__ = (
		Index("ix_vpn_orders_user", "user_id"),
		Index("ix_vpn_orders_status", "status"),
	)


class AllowedChat(Base):
	"""
	Разрешенные чаты для работы бота.
	Только владелец может добавлять новые группы.
	"""
	__tablename__ = "allowed_chats"

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
	chat_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
	added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)  # OWNER_ID
	added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
	is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

	__table_args__ = (
		Index("ix_allowed_chat_active", "is_active"),
	) 