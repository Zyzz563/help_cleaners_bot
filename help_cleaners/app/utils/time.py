from __future__ import annotations

from datetime import datetime, date, time, timedelta
from typing import Iterable, List, Tuple

from zoneinfo import ZoneInfo


def parse_hhmm(value: str) -> time:
	h, m = value.split(":", 1)
	return time(hour=int(h), minute=int(m))


def local_now(tz_name: str) -> datetime:
	return datetime.now(ZoneInfo(tz_name))


def local_today(tz_name: str) -> date:
	return local_now(tz_name).date()


def build_slots(
	*,
	tz_name: str,
	day: date,
	window_start: time,
	window_end: time,
	interval_min: int,
	lunch_start: time,
	lunch_end: time,
) -> List[datetime]:
	# Build naive datetimes in local tz
	start_dt = datetime.combine(day, window_start, tzinfo=ZoneInfo(tz_name))
	end_dt = datetime.combine(day, window_end, tzinfo=ZoneInfo(tz_name))
	if end_dt <= start_dt:
		end_dt = end_dt + timedelta(days=1)

	current = start_dt
	slots: List[datetime] = []
	while current <= end_dt:
		if not is_in_lunch(current.timetz(), lunch_start, lunch_end):
			slots.append(current)
		current += timedelta(minutes=interval_min)
	return slots


def is_in_lunch(current_time: time, lunch_start: time, lunch_end: time) -> bool:
	return lunch_start <= current_time < lunch_end 