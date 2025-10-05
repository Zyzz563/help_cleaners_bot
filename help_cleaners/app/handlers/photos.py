from __future__ import annotations

from datetime import datetime, date
from typing import List, Dict, Set
import asyncio

from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OWNER_ID, ALLOWED_CHATS
from app.db.models import Group, Member, Photo, PhotoDuplicate, Shift, Timesheet, AllowedChat
from app.utils.time import local_now

router = Router()

# Словарь для отслеживания медиа-групп в процессе обработки
media_groups_processing: Dict[str, Dict] = {}


async def check_user_allowed(message: Message) -> bool:
	"""
	🔒 Проверяет разрешен ли пользователь для работы с ботом.
	Только владелец (OWNER_ID) может использовать бота.
	"""
	if not message.from_user:
		return False
	
	# Только владелец может использовать бота
	return message.from_user.id == OWNER_ID


async def check_chat_allowed(message: Message, session: AsyncSession) -> bool:
	"""
	🔒 Проверяет разрешен ли чат для работы бота.
	Возвращает True если чат разрешен, False если нет.
	"""
	chat_id = message.chat.id
	
	# Сначала проверяем в конфигурации (для быстрого доступа)
	if chat_id in ALLOWED_CHATS:
		return True
	
	# Затем проверяем в БД
	allowed_chat = await session.execute(
		select(AllowedChat).where(
			AllowedChat.chat_id == chat_id,
			AllowedChat.is_active == True
		)
	)
	
	if allowed_chat.scalar_one_or_none():
		return True
	
	# Если чат не разрешен - бот уходит
	await message.reply("❌ Этот бот не разрешён для использования в данной группе.")
	try:
		await message.chat.leave()
	except Exception:
		pass  # Игнорируем ошибки при выходе
	
	return False


async def check_and_save_photo(session: AsyncSession, message: Message, photo_size, chat_id: int, user_id: int, group, is_forwarded: bool = False) -> tuple[bool, str]:
	"""
	Проверяет фото на дубликат и сохраняет в БД.
	Возвращает (is_duplicate, response_message)
	"""
	file_unique_id = photo_size.file_unique_id
	file_id = photo_size.file_id
	now = local_now(group.tz)
	today = now.date()

	# Ищем самое раннее фото с таким же unique_id (оригинал)
	existing = await session.execute(
		select(Photo)
		.where(Photo.chat_id == chat_id, Photo.file_unique_id == file_unique_id)
		.order_by(Photo.id.asc())  # Самое раннее = оригинал
	)
	original = existing.scalars().first()
	
	if original:
		# Это дубликат - создаем запись
		dup_photo = Photo(
			chat_id=chat_id,
			user_id=user_id,
			date=today,
			time=now.timetz(),
			file_id=file_id,
			file_unique_id=file_unique_id,
			phash=None,
			caption=message.caption or None,
			is_forwarded=is_forwarded,
		)
		session.add(dup_photo)
		await session.flush()
		
		# Создаем запись о дубликате
		dup_record = PhotoDuplicate(
			chat_id=chat_id,
			original_photo_id=original.id,
			duplicate_photo_id=dup_photo.id,
			original_date=original.date,
			original_time=original.time,
			duplicate_date=today,
			duplicate_time=now.timetz(),
			created_at=datetime.utcnow(),
		)
		session.add(dup_record)
		await session.commit()
		
		# Получаем информацию об оригинальном пользователе
		original_member = await session.get(Member, {"chat_id": chat_id, "user_id": original.user_id})
		if original_member:
			original_user_name = original_member.display_name
		else:
			# Если пользователь не зарегистрирован, получаем информацию из Telegram
			try:
				orig_chat_member = await message.bot.get_chat_member(chat_id, original.user_id)
				original_user_name = orig_chat_member.user.username or f"ID:{original.user_id}"
			except:
				original_user_name = f"ID:{original.user_id}"
		
		# Формируем сообщение о дубликате с информацией о том, кто отправил оригинал
		if user_id != original.user_id:
			response = f"⚠️ Дубликат фото обнаружен!\nОригинал от {original_user_name}: {original.date.isoformat()} в {str(original.time)[:8]}\nВаш дубликат: {dup_record.duplicate_date.isoformat()} {str(dup_record.duplicate_time)[:8]}"
		else:
			response = f"⚠️ Дубликат фото обнаружен!\nОригинал: {original.date.isoformat()} в {str(original.time)[:8]}\nДубликат: {dup_record.duplicate_date.isoformat()} {str(dup_record.duplicate_time)[:8]}"
		
		# НЕ ПРОВЕРЯЕМ ТАБЕЛЬ для дубликатов!
		return True, response

	# Store as original (первое появление этого фото)
	rec = Photo(
		chat_id=chat_id,
		user_id=user_id,
		date=today,
		time=now.timetz(),
		file_id=file_id,
		file_unique_id=file_unique_id,
		phash=None,
		caption=message.caption or None,
		is_forwarded=is_forwarded,
	)
	session.add(rec)
	await session.commit() 
	
	# ТАБЕЛЬ: Теперь управляется вручную через /add_shift
	# await check_and_update_timesheet(session, chat_id, user_id, today, group)
	
	return False, ""


async def check_and_update_timesheet(session: AsyncSession, chat_id: int, user_id: int, check_date: date, group) -> None:
	"""
	Проверяет и обновляет табель - умная логика с учетом времени смен
	"""
	from datetime import timedelta, time as dt_time
	
	# Получаем все смены: на текущую дату И на предыдущую (для ночных смен)
	prev_date = check_date - timedelta(days=1)
	
	shifts_result = await session.execute(
		select(Shift).where(
			Shift.chat_id == chat_id,
			Shift.user_id == user_id,
			Shift.date.in_([prev_date, check_date])
		)
	)
	shifts = shifts_result.scalars().all()
	
	if not shifts:
		print(f"DEBUG: Нет смен для {check_date}")
		return
	
	print(f"DEBUG: Найдено смен: {len(shifts)}")
	
	# Обрабатываем каждую смену
	for shift in shifts:
		print(f"DEBUG: Обрабатываем смену: date={shift.date}, type={shift.type}")
		
		if shift.type == "day":
			# Дневная смена (09:00-21:00) - считаем фото за день
			if shift.date == check_date:
				photo_count = await _count_photos_for_shift(session, chat_id, user_id, check_date, "day")
				print(f"DEBUG: Дневная смена {check_date}, фото: {photo_count}")
				await _update_timesheet_entry(session, chat_id, user_id, check_date, "day", photo_count)
			
		elif shift.type == "night":
			# Ночная смена (21:00-09:00)
			# Смена shift.date означает что работа НАЧИНАЕТСЯ вечером shift.date
			# Считаем фото: с 21:00 shift.date до 09:00 (shift.date + 1)
			shift_start_date = shift.date
			shift_end_date = shift.date + timedelta(days=1)
			
			# Считаем фото для ночной смены
			photo_count = await _count_photos_for_night_shift(
				session, chat_id, user_id, shift_start_date, shift_end_date
			)
			print(f"DEBUG: Ночная смена {shift_start_date} 21:00 - {shift_end_date} 09:00, фото: {photo_count}")
			
			# Записываем в табель на дату ОКОНЧАНИЯ смены (следующий день)
			await _update_timesheet_entry(session, chat_id, user_id, shift_end_date, "night", photo_count)


async def _count_photos_for_shift(session: AsyncSession, chat_id: int, user_id: int, shift_date: date, shift_type: str) -> int:
	"""Считает количество фото для дневной смены (09:00-21:00)"""
	from datetime import time as dt_time
	
	photo_count_result = await session.execute(
		select(func.count(Photo.id)).where(
			Photo.chat_id == chat_id,
			Photo.user_id == user_id,
			Photo.date == shift_date,
			Photo.is_forwarded == False,
			# Исключаем дубликаты
			~Photo.id.in_(
				select(PhotoDuplicate.duplicate_photo_id).where(
					PhotoDuplicate.chat_id == chat_id
				)
			)
		)
	)
	return photo_count_result.scalar() or 0


async def _count_photos_for_night_shift(session: AsyncSession, chat_id: int, user_id: int, start_date: date, end_date: date) -> int:
	"""
	Считает количество фото для ночной смены (21:00-09:00)
	start_date - дата начала смены (когда 21:00)
	end_date - дата окончания смены (когда 09:00)
	"""
	from datetime import time as dt_time
	
	# Считаем фото за start_date (с 21:00 до 23:59) + end_date (с 00:00 до 09:00)
	# Упрощенно: все фото за start_date с 21:00 и все фото за end_date до 09:00
	# Но у нас в БД только дата, без фильтра по времени, поэтому считаем ВСЕ фото за обе даты
	
	# Более простой подход: считаем все фото за start_date и end_date вместе
	photo_count_result = await session.execute(
		select(func.count(Photo.id)).where(
			Photo.chat_id == chat_id,
			Photo.user_id == user_id,
			Photo.date.in_([start_date, end_date]),
			Photo.is_forwarded == False,
			# Исключаем дубликаты
			~Photo.id.in_(
				select(PhotoDuplicate.duplicate_photo_id).where(
					PhotoDuplicate.chat_id == chat_id
				)
			)
		)
	)
	return photo_count_result.scalar() or 0


async def _update_timesheet_entry(session: AsyncSession, chat_id: int, user_id: int, work_date: date, shift_type: str, photo_count: int) -> None:
	"""Создает/обновляет/удаляет запись табеля"""
	
	print(f"DEBUG: _update_timesheet_entry - chat_id: {chat_id}, user_id: {user_id}, date: {work_date}, type: {shift_type}, photos: {photo_count}")
	
	# Проверяем есть ли запись в табеле с таким же chat_id, user_id, date (независимо от shift_type)
	timesheet_result = await session.execute(
		select(Timesheet).where(
			Timesheet.chat_id == chat_id,
			Timesheet.user_id == user_id,
			Timesheet.date == work_date
		)
	)
	existing_entries = timesheet_result.scalars().all()
	
	if photo_count >= 10:
		# Ищем запись с нужным shift_type
		target_entry = None
		for entry in existing_entries:
			if entry.shift_type == shift_type:
				target_entry = entry
				break
		
		if target_entry:
			# Обновляем существующую запись
			print(f"DEBUG: Обновляем существующую запись табеля")
			target_entry.photo_count = photo_count
		else:
			# Создаем новую запись
			print(f"DEBUG: Создаем новую запись табеля")
			new_entry = Timesheet(
				chat_id=chat_id,
				user_id=user_id,
				date=work_date,
				shift_type=shift_type,
				photo_count=photo_count,
				confirmed_at=datetime.utcnow()
			)
			session.add(new_entry)
		
		await session.commit()
		print(f"DEBUG: Табель обновлен успешно")
	else:
		# Если фото < 10, удаляем записи с таким shift_type
		for entry in existing_entries:
			if entry.shift_type == shift_type:
				print(f"DEBUG: Удаляем запись табеля (фото < 10)")
				await session.delete(entry)
		
		if existing_entries:  # Если были записи для удаления
			await session.commit()
		else:
			print(f"DEBUG: Фото < 10, запись не создается")


@router.message(F.photo)
async def on_photo(message: Message, session: AsyncSession):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		return

	chat_id = message.chat.id
	user_id = message.from_user.id if message.from_user else 0

	# Убираем проверку на регистрацию - бот должен работать для ВСЕХ участников
	# member = await session.get(Member, {"chat_id": chat_id, "user_id": user_id})
	# if not member:
	# 	return

	group = await session.get(Group, chat_id)
	if not group:
		# Создаем группу если не существует
		from app.handlers.commands import ensure_group
		group = await ensure_group(session, chat_id, message.chat.title)

	# Проверяем, является ли сообщение пересланным
	is_forwarded = bool(message.forward_from or message.forward_from_chat)
	
	# Получаем самое качественное фото
	photo_size = message.photo[-1]
	
	# Проверяем на дубликат и сохраняем
	is_duplicate, response_msg = await check_and_save_photo(
		session, message, photo_size, chat_id, user_id, group, is_forwarded
	)
	
	# Если это дубликат, отвечаем сразу
	if is_duplicate and not is_forwarded:
		await message.reply(response_msg)


@router.message(F.media_group_id)
async def on_media_group(message: Message, session: AsyncSession):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	"""
	Обработчик для медиа-групп (альбомы с несколькими фото).
	Собирает все фото из группы, проверяет каждое с БД,
	находит дубликаты внутри группы, реагирует на все дубликаты.
	"""
	if message.chat.type not in {"group", "supergroup"}:
		return

	chat_id = message.chat.id
	user_id = message.from_user.id if message.from_user else 0
	media_group_id = message.media_group_id

	# Убираем проверку на регистрацию - бот должен работать для ВСЕХ участников
	# member = await session.get(Member, {"chat_id": chat_id, "user_id": user_id})
	# if not member:
	# 	return

	group = await session.get(Group, chat_id)
	if not group:
		# Создаем группу если не существует
		from app.handlers.commands import ensure_group
		group = await ensure_group(session, chat_id, message.chat.title)

	# Проверяем, является ли сообщение пересланным
	is_forwarded = bool(message.forward_from or message.forward_from_chat)
	
	# Если это фото в медиа-группе
	if message.photo:
		photo_size = message.photo[-1]
		file_unique_id = photo_size.file_unique_id
		
		# Инициализируем обработку медиа-группы
		if media_group_id not in media_groups_processing:
			media_groups_processing[media_group_id] = {
				'photos': [],
				'seen_unique_ids': set(),
				'duplicates_in_group': [],
				'processed': False
			}
		
		group_data = media_groups_processing[media_group_id]
		
		# Добавляем фото в группу
		group_data['photos'].append({
			'message': message,
			'photo_size': photo_size,
			'file_unique_id': file_unique_id
		})
		
		# Ждем немного, чтобы собрать все фото из группы
		await asyncio.sleep(1)
		
		# Если группа еще не обработана, запускаем обработку
		if not group_data['processed']:
			group_data['processed'] = True
			
			# Сначала проверяем ВСЕ фото с БД
			db_duplicates = []
			unique_photos = []
			
			for photo_data in group_data['photos']:
				msg = photo_data['message']
				photo_sz = photo_data['photo_size']
				file_uid = photo_data['file_unique_id']
				
				# Проверяем с БД - ищем самый ранний оригинал
				existing = await session.execute(
					select(Photo)
					.where(Photo.chat_id == chat_id, Photo.file_unique_id == file_uid)
					.order_by(Photo.id.asc())  # Самое раннее = оригинал
				)
				original = existing.scalars().first()
				
				if original:
					# Это дубликат из БД
					db_duplicates.append({
						'message': msg,
						'photo_size': photo_sz,
						'file_unique_id': file_uid,
						'original': original
					})
				else:
					# Это уникальное фото - добавляем в список для проверки дубликатов внутри группы
					unique_photos.append({
						'message': msg,
						'photo_size': photo_sz,
						'file_unique_id': file_uid
					})
			
			# Теперь находим дубликаты внутри группы среди уникальных фото
			seen_in_group = set()
			group_duplicates = []
			truly_unique = []
			
			for photo_data in unique_photos:
				file_uid = photo_data['file_unique_id']
				
				if file_uid in seen_in_group:
					# Дубликат внутри группы
					group_duplicates.append(photo_data)
				else:
					# Первое появление в группе
					seen_in_group.add(file_uid)
					truly_unique.append(photo_data)
			
			# Реагируем на дубликаты из БД
			for dup_data in db_duplicates:
				msg = dup_data['message']
				original = dup_data['original']
				now = local_now(group.tz)
				
				# Сохраняем дубликат в БД
				dup_photo = Photo(
					chat_id=chat_id,
					user_id=user_id,
					date=now.date(),
					time=now.timetz(),
					file_id=dup_data['photo_size'].file_id,
					file_unique_id=dup_data['file_unique_id'],
					phash=None,
					caption=msg.caption or None,
					is_forwarded=is_forwarded,
				)
				session.add(dup_photo)
				await session.flush()
				
				# Создаем запись о дубликате
				dup_record = PhotoDuplicate(
					chat_id=chat_id,
					original_photo_id=original.id,
					duplicate_photo_id=dup_photo.id,
					original_date=original.date,
					original_time=original.time,
					duplicate_date=now.date(),
					duplicate_time=now.timetz(),
					created_at=datetime.utcnow(),
				)
				session.add(dup_record)
				await session.commit()
				
				# Отвечаем на дубликат из БД - показываем кто отправил оригинал
				if not is_forwarded:
					# Получаем информацию об оригинальном пользователе
					original_member = await session.get(Member, {"chat_id": chat_id, "user_id": original.user_id})
					if original_member:
						original_user_name = original_member.display_name
					else:
						# Если пользователь не зарегистрирован, получаем информацию из Telegram
						try:
							orig_chat_member = await msg.bot.get_chat_member(chat_id, original.user_id)
							original_user_name = orig_chat_member.user.username or f"ID:{original.user_id}"
						except:
							original_user_name = f"ID:{original.user_id}"
					
					if user_id != original.user_id:
						response = f"⚠️ Дубликат фото обнаружен!\nОригинал от {original_user_name}: {original.date.isoformat()} в {str(original.time)[:8]}\nВаш дубликат: {dup_record.duplicate_date.isoformat()} {str(dup_record.duplicate_time)[:8]}"
					else:
						response = f"⚠️ Дубликат фото обнаружен!\nОригинал: {original.date.isoformat()} в {str(original.time)[:8]}\nДубликат: {dup_record.duplicate_date.isoformat()} {str(dup_record.duplicate_time)[:8]}"
					
					await msg.reply(response)
			
			# Реагируем на дубликаты внутри группы
			for dup_data in group_duplicates:
				msg = dup_data['message']
				
				# Сохраняем дубликат в БД
				now = local_now(group.tz)
				dup_photo = Photo(
					chat_id=chat_id,
					user_id=user_id,
					date=now.date(),
					time=now.timetz(),
					file_id=dup_data['photo_size'].file_id,
					file_unique_id=dup_data['file_unique_id'],
					phash=None,
					caption=msg.caption or None,
					is_forwarded=is_forwarded,
				)
				session.add(dup_photo)
				await session.commit()
				
				# Отвечаем на дубликат внутри группы
				if not is_forwarded:
					await msg.reply("⚠️ Дубликат фото обнаружен в альбоме!")
			
			# Сохраняем только уникальные фото в БД
			for photo_data in truly_unique:
				msg = photo_data['message']
				photo_sz = photo_data['photo_size']
				
				now = local_now(group.tz)
				rec = Photo(
					chat_id=chat_id,
					user_id=user_id,
					date=now.date(),
					time=now.timetz(),
					file_id=photo_sz.file_id,
					file_unique_id=photo_data['file_unique_id'],
					phash=None,
					caption=msg.caption or None,
					is_forwarded=is_forwarded,
				)
				session.add(rec)
				await session.commit()
			
			# ТАБЕЛЬ: Теперь управляется вручную через /add_shift
			# if truly_unique:  # Только если есть уникальные фото
			# 	now = local_now(group.tz)
			# 	await check_and_update_timesheet(session, chat_id, user_id, now.date(), group)
			
			# Очищаем данные группы через некоторое время
			asyncio.create_task(cleanup_media_group(media_group_id))


async def cleanup_media_group(media_group_id: str):
	"""Очищает данные медиа-группы через 30 секунд"""
	await asyncio.sleep(30)
	if media_group_id in media_groups_processing:
		del media_groups_processing[media_group_id] 