from __future__ import annotations

import asyncio
from datetime import datetime, date, time, timedelta
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func, insert, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, KICKED, MEMBER
from aiogram.types import ChatMemberUpdated
from aiogram.types import InputMediaPhoto
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.config import MAX_NAME_LEN, default_group_config, OWNER_ID, ALLOWED_CHATS
from app.db.models import Group, Member, Shift, Photo, PhotoDuplicate, ProcessedCallback, Timesheet, AllowedChat
from app.db.session import create_session_maker
from app.keyboards import day_night_kb, register_name_kb, gender_kb, start_kb, remove_menu_kb, members_choice_kb
from app.utils.text import is_valid_display_name
from app.utils.time import parse_hhmm, local_today, local_now

router = Router()


async def check_user_allowed(message: Message) -> bool:
	"""
	🔒 Проверяет разрешен ли пользователь для работы с ботом.
	Только владелец (OWNER_ID) может использовать команды.
	"""
	if not message.from_user:
		return False
	
	# Только владелец может использовать команды
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


async def check_owner_permission(message: Message) -> bool:
	"""
	🔒 Проверяет является ли пользователь владельцем бота.
	"""
	return message.from_user and message.from_user.id == OWNER_ID


async def auto_delete_message(message: Message, delay_seconds: int = 60):
	"""Автоматически удаляет сообщение через указанное время"""
	await asyncio.sleep(delay_seconds)
	try:
		await message.delete()
	except Exception:
		pass  # Игнорируем ошибки удаления


async def cleanup_registration_messages(messages_to_delete: list, delay_seconds: int = 60):
	"""Удаляет все сообщения процесса регистрации через указанное время"""
	await asyncio.sleep(delay_seconds)
	for msg in messages_to_delete:
		try:
			await msg.delete()
		except Exception:
			pass  # Игнорируем ошибки удаления


async def ensure_group(session: AsyncSession, chat_id: int, title: Optional[str]) -> Group:
	existing = await session.get(Group, chat_id)
	if existing:
		return existing
	cfg = default_group_config()
	group = Group(
		chat_id=chat_id,
		title=title,
		tz=cfg["tz"],
		day_start=parse_hhmm(cfg["day_start"]),
		day_end=parse_hhmm(cfg["day_end"]),
		night_start=parse_hhmm(cfg["night_start"]),
		night_end=parse_hhmm(cfg["night_end"]),
		reminder_interval_min=cfg["reminder_interval_min"],
		lunch_start=parse_hhmm(cfg["lunch_start"]),
		lunch_end=parse_hhmm(cfg["lunch_end"]),
		updated_at=datetime.utcnow(),
	)
	session.add(group)
	await session.commit()
	return group


async def is_admin(chat_id: int, user_id: int, bot) -> bool:
	try:
		cm = await bot.get_chat_member(chat_id, user_id)
		return cm.status in {"administrator", "creator", "owner"}
	except Exception:
		return False


async def schedule_reminders_for_shift(chat_id: int, shift_date: date, shift_type: str, session: AsyncSession) -> None:
	# TODO: integrate real scheduler. Placeholder for MVP.
	return None


class RegStates(StatesGroup):
	waiting_name = State()
	waiting_gender = State()
	
	# Таймауты для состояний (в секундах)
	waiting_name_timeout = 300  # 5 минут на ввод имени
	waiting_gender_timeout = 300  # 5 минут на выбор пола


class TimesheetStates(StatesGroup):
	"""Состояния для управления табелем"""
	waiting_shift_date = State()  # Ожидание выбора даты смены
	waiting_shift_type = State()  # Ожидание выбора типа смены
	waiting_confirm = State()  # Ожидание подтверждения добавления
	
	# Для удаления смены
	waiting_remove_date = State()  # Ожидание выбора даты для удаления


@router.message(Command("register"))
async def cmd_register(message: Message, session: AsyncSession, state: FSMContext):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return

	chat_id = message.chat.id
	group = await ensure_group(session, chat_id, message.chat.title)

	# Check if user is trying to register someone else (admin only)
	args = message.text.split(maxsplit=1)
	target_user_id = message.from_user.id
	
	if len(args) >= 2:
		# Admin pattern: /register 123456789 — допускаем независимо от текущего состояния
		if not await is_admin(chat_id, message.from_user.id, message.bot):
			reply_msg = await message.reply("❌ Регистрировать других может только админ.")
			data = await state.get_data()
			messages_to_delete = data.get("messages_to_delete", [])
			messages_to_delete.append(reply_msg)
			await state.update_data(messages_to_delete=messages_to_delete)
			return
		try:
			target_user_id = int(args[1])
			# Check if user exists in chat
			chat_member = await message.bot.get_chat_member(chat_id, target_user_id)
			if chat_member.status in ['left', 'kicked']:
				reply_msg = await message.reply("❌ Указанный пользователь не находится в группе.")
				data = await state.get_data()
				messages_to_delete = data.get("messages_to_delete", [])
				messages_to_delete.append(reply_msg)
				await state.update_data(messages_to_delete=messages_to_delete)
				return
		except (ValueError, Exception):
			reply_msg = await message.reply("❌ Неверный формат ID. Используйте: /register 123456789")
			data = await state.get_data()
			messages_to_delete = data.get("messages_to_delete", [])
			messages_to_delete.append(reply_msg)
			await state.update_data(messages_to_delete=messages_to_delete)
			return
		# Check if target user is already registered
		existing_target = await session.get(Member, {"chat_id": chat_id, "user_id": target_user_id})
		if existing_target:
			reply_msg = await message.reply(f"Пользователь {existing_target.display_name} уже зарегистрирован в этой группе.")
			data = await state.get_data()
			messages_to_delete = data.get("messages_to_delete", [])
			messages_to_delete.append(reply_msg)
			await state.update_data(messages_to_delete=messages_to_delete)
			return
		# Start registration for target user
		await state.update_data(target_user_id=target_user_id)
		reply_msg = await message.reply(f"Регистрирую пользователя ID:{target_user_id}. Введите его имя (1..{MAX_NAME_LEN} символов):")
		await state.set_state(RegStates.waiting_name)
		await state.update_data(messages_to_delete=[message, reply_msg])
		return

	# Anti-duplication for self-registration only
	st = await state.get_state()
	if st == RegStates.waiting_gender.state:
		reply_msg = await message.reply("Вы уже в процессе регистрации: выберите пол на предыдущем сообщении или отправьте /reset, чтобы начать заново.")
		data = await state.get_data()
		messages_to_delete = data.get("messages_to_delete", [])
		messages_to_delete.append(reply_msg)
		await state.update_data(messages_to_delete=messages_to_delete)
		return
	if st == RegStates.waiting_name.state:
		reply_msg = await message.reply(f"Регистрация уже начата: введите имя (1..{MAX_NAME_LEN} символов) или отправьте /reset, чтобы начать заново.")
		data = await state.get_data()
		messages_to_delete = data.get("messages_to_delete", [])
		messages_to_delete.append(reply_msg)
		await state.update_data(messages_to_delete=messages_to_delete)
		return

	# Self-registration flow
	if message.from_user.username:
		kb = register_name_kb(message.from_user.username if message.from_user else None)
		reply = await message.reply(f"Отправьте имя/ник (1..{MAX_NAME_LEN} символов) или нажмите кнопку", reply_markup=kb)
		await state.set_state(RegStates.waiting_name)
		await state.update_data(messages_to_delete=[message, reply])
		return

	# Без username предлагаем вручную ввести имя, не завершаем регистрацию автоматически
	reply = await message.reply(f"Отправьте имя/ник (1..{MAX_NAME_LEN} символов)")
	await state.set_state(RegStates.waiting_name)
	await state.update_data(messages_to_delete=[message, reply])


@router.message(StateFilter(RegStates.waiting_name), F.text)
async def on_manual_name(message: Message, session: AsyncSession, state: FSMContext):
	# Сразу отвечаем - "обрабатываю"
	processing_msg = await message.reply("⏳ Обрабатываю...")
	
	try:
		text = (message.text or "").strip()
		# If user typed a command instead of a name, cancel registration step and dispatch the command
		if text.startswith('/'):
			await state.clear()
			await processing_msg.delete()
			cmd = text.split()[0].split('@')[0]
			if cmd == '/shift':
				await cmd_shift(message, session)
				return
			if cmd == '/today':
				await cmd_today(message, session)
				return
			if cmd == '/cleaners':
				await cmd_cleaners(message, session)
				return
			if cmd == '/register':
				await cmd_register(message, session, state)
				return
			if cmd == '/analyze_duplicates':
				await cmd_analyze_duplicates(message, session)
				return
			if cmd == '/tabel':
				await cmd_tabel(message, session)
				return
		
		# Get target_user_id from state (for admin registration) or use current user
		data = await state.get_data()
		target_user_id = data.get("target_user_id", message.from_user.id)
		
		# Validate the name
		if not is_valid_display_name(text):
			reply_msg = await processing_msg.edit_text(f"❌ Имя должно быть 1..{MAX_NAME_LEN} символов, без пробелов/эмодзи. Введите снова.")
			# Добавляем сообщение к списку для удаления
			data = await state.get_data()
			messages_to_delete = data.get("messages_to_delete", [])
			messages_to_delete.append(processing_msg)
			await state.update_data(messages_to_delete=messages_to_delete)
			return
		
		# Remove "processing" message and continue
		await processing_msg.delete()
		await _complete_registration(message, session, state, message.chat.id, text, target_user_id=target_user_id)
		
	except Exception as e:
		# В случае ошибки - очищаем состояние и сообщаем
		await processing_msg.edit_text(f"❌ Ошибка: {str(e)}. Попробуйте /register снова.")
		
		# Добавляем сообщение к списку для удаления
		data = await state.get_data()
		messages_to_delete = data.get("messages_to_delete", [])
		messages_to_delete.append(processing_msg)
		await state.update_data(messages_to_delete=messages_to_delete)
		
		await state.clear()


async def _complete_registration(message: Message, session: AsyncSession, state: FSMContext, chat_id: int, candidate: str, *, target_user_id: int) -> None:
	try:
		# Проверяем существующую регистрацию
		existing_self = await session.get(Member, {"chat_id": chat_id, "user_id": target_user_id})
		if existing_self:
			if target_user_id == message.from_user.id:
				reply_msg = await message.reply(f"✅ Вы уже зарегистрированы как {existing_self.display_name}.")
			else:
				reply_msg = await message.reply(f"✅ Пользователь {existing_self.display_name} уже зарегистрирован в этой группе.")
			
			# Добавляем сообщение к списку для удаления
			data = await state.get_data()
			messages_to_delete = data.get("messages_to_delete", [])
			messages_to_delete.append(reply_msg)
			await state.update_data(messages_to_delete=messages_to_delete)
			
			await state.clear()
			return
		
		# Проверяем уникальность имени в группе (исключая того же пользователя)
		q = select(Member).where(
			Member.chat_id == chat_id,
			func.lower(Member.display_name) == func.lower(candidate),
		)
		exists = (await session.execute(q)).scalar_one_or_none()
		if exists and exists.user_id != target_user_id:
			reply = await message.reply("❌ Такое имя уже занято в этой группе. Введите другое или используйте формат Имя|user_id")
			
			# Добавляем сообщение к списку для удаления
			data = await state.get_data()
			messages_to_delete = data.get("messages_to_delete", [])
			messages_to_delete.append(reply)
			await state.update_data(messages_to_delete=messages_to_delete)
			
			await state.set_state(RegStates.waiting_name)
			return

		await state.update_data(display_name=candidate, target_user_id=target_user_id)
		
		# Разные сообщения для саморегистрации и регистрации других
		if target_user_id == message.from_user.id:
			reply = await message.reply("✅ Имя принято! Теперь выберите пол:", reply_markup=gender_kb())
		else:
			# Получаем информацию о регистрируемом пользователе
			try:
				chat_member = await message.bot.get_chat_member(chat_id, target_user_id)
				user_info = f"@{chat_member.user.username}" if chat_member.user.username else f"ID:{target_user_id}"
				reply = await message.reply(f"✅ Регистрирую {user_info} как '{candidate}'. Теперь выберите пол:", reply_markup=gender_kb())
			except Exception:
				reply = await message.reply(f"✅ Регистрирую пользователя ID:{target_user_id} как '{candidate}'. Теперь выберите пол:", reply_markup=gender_kb())
		
		await state.set_state(RegStates.waiting_gender)
		
		# Добавляем сообщение к списку для удаления
		data = await state.get_data()
		messages_to_delete = data.get("messages_to_delete", [])
		messages_to_delete.append(message)  # Сообщение пользователя с именем
		messages_to_delete.append(reply)    # Сообщение бота с кнопками
		await state.update_data(messages_to_delete=messages_to_delete)
		
	except Exception as e:
		reply_msg = await message.reply(f"❌ Ошибка при проверке имени: {str(e)}. Попробуйте /register снова.")
		
		# Добавляем сообщение к списку для удаления
		data = await state.get_data()
		messages_to_delete = data.get("messages_to_delete", [])
		messages_to_delete.append(reply_msg)
		await state.update_data(messages_to_delete=messages_to_delete)
		
		await state.clear()


@router.callback_query(StateFilter(RegStates.waiting_gender), F.data.startswith("gender:"))
async def on_gender(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	try:
		# Сразу отвечаем на callback
		await callback.answer("⏳ Сохраняю...")
		
		gender = callback.data.split(":", 1)[1]
		if gender not in {"m", "f"}:
			await callback.answer("❌ Неверный пол", show_alert=True)
			return
		
		data = await state.get_data()
		display_name = data.get("display_name")
		target_user_id = data.get("target_user_id")
		
		if not display_name or not target_user_id:
			await callback.answer("❌ Ошибка состояния. Повторите /register", show_alert=True)
			await state.clear()
			return
		
		# Upsert: if member exists, update name/gender; else create
		member = await session.get(Member, {"chat_id": callback.message.chat.id, "user_id": int(target_user_id)})
		if member:
			member.display_name = display_name
			member.gender = gender
		else:
			member = Member(
				chat_id=callback.message.chat.id,
				user_id=int(target_user_id),
				display_name=display_name,
				role="cleaner",
				registered_at=datetime.utcnow(),
				gender=gender,
			)
			session.add(member)
		
		await session.commit()
		
		# Успешная регистрация
		gender_text = "мужской" if gender == "m" else "женский"
		
		# Разные сообщения для саморегистрации и регистрации других
		if int(target_user_id) == callback.from_user.id:
			await callback.message.edit_text(f"✅ Регистрация завершена!\n\n👤 Имя: {display_name}\n🚹 Пол: {gender_text}\n\nТеперь вы можете использовать команды /shift, /today, /myshifts")
		else:
			# Получаем информацию о зарегистрированном пользователе
			try:
				chat_member = await callback.bot.get_chat_member(callback.message.chat.id, int(target_user_id))
				user_info = f"@{chat_member.user.username}" if chat_member.user.username else f"ID:{target_user_id}"
				await callback.message.edit_text(f"✅ Регистрация завершена!\n\n👤 Пользователь: {user_info}\n📝 Имя: {display_name}\n🚹 Пол: {gender_text}\n\nПользователь может использовать команды /shift, /today, /myshifts")
			except Exception:
				await callback.message.edit_text(f"✅ Регистрация завершена!\n\n👤 Пользователь: ID:{target_user_id}\n📝 Имя: {display_name}\n🚹 Пол: {gender_text}\n\nПользователь может использовать команды /shift, /today, /myshifts")
		
		# Удаляем все предыдущие сообщения процесса регистрации
		data = await state.get_data()
		messages_to_delete = data.get("messages_to_delete", [])
		if messages_to_delete:
			asyncio.create_task(cleanup_registration_messages(messages_to_delete, 2))
		
		await state.clear()
		
	except Exception as e:
		await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)
		await state.clear()


@router.callback_query(F.data == "regname:use_username")
async def cb_use_username(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	username = callback.from_user.username
	if not username:
		await callback.answer("У вас нет username. Введите вручную.", show_alert=True)
		return
	chat_id = callback.message.chat.id
	await ensure_group(session, chat_id, callback.message.chat.title)
	# If already registered, just inform
	existing = await session.get(Member, {"chat_id": chat_id, "user_id": callback.from_user.id})
	if existing:
		await callback.message.answer(f"Вы уже зарегистрированы как {existing.display_name}.")
		await callback.answer()
		return
	# If name taken by other user, ask manual
	q = select(Member).where(Member.chat_id == chat_id, func.lower(Member.display_name) == func.lower(username))
	taken = (await session.execute(q)).scalar_one_or_none()
	if taken and taken.user_id != callback.from_user.id:
		await callback.message.answer("Это имя занято в группе. Нажмите 'Ввести вручную' и укажите Имя или Имя|user_id")
		await callback.answer()
		await state.set_state(RegStates.waiting_name)
		return
	# proceed to gender
	await state.update_data(display_name=username, target_user_id=callback.from_user.id)
	await callback.message.answer("Выберите пол:", reply_markup=gender_kb())
	await state.set_state(RegStates.waiting_gender)
	await callback.answer()


@router.callback_query(F.data == "regname:manual")
async def cb_manual(callback: CallbackQuery, state: FSMContext):
	st = await state.get_state()
	if st == RegStates.waiting_gender.state:
		await callback.answer()
		return
	reply_msg = await callback.message.answer("Введите имя (1..10 символов)")
	await state.set_state(RegStates.waiting_name)
	await callback.answer()
	
	# Добавляем сообщение к списку для удаления
	data = await state.get_data()
	messages_to_delete = data.get("messages_to_delete", [])
	messages_to_delete.append(reply_msg)
	await state.update_data(messages_to_delete=messages_to_delete)


@router.message(Command("shift"))
async def cmd_shift(message: Message, session: AsyncSession):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return

	chat_id = message.chat.id
	user_id = message.from_user.id if message.from_user else 0
	group = await ensure_group(session, chat_id, message.chat.title)
	today = local_today(group.tz)

	args = (message.text or "").split()
	# Usage: /shift [YYYY-MM-DD] [day|night|day_night]
	shift_date = today
	shift_type: Optional[str] = None
	if len(args) >= 2:
		try:
			shift_date = date.fromisoformat(args[1])
		except Exception:
			shift_date = today
	if len(args) >= 3 and args[2] in {"day", "night", "day_night"}:
		shift_type = args[2]

	if shift_type is None:
		# Проверяем есть ли уже смена сегодня для умной клавиатуры
		existing_shift = await session.execute(
			select(Shift).where(Shift.chat_id == chat_id, Shift.user_id == user_id, Shift.date == shift_date)
		)
		current_shift = existing_shift.scalar_one_or_none()
		
		if current_shift:
			if current_shift.type == "day":
				# Уже есть дневная - предлагаем ночную или комбо
				await message.reply(f"У вас уже есть дневная смена на {shift_date.strftime('%d.%m')}.\nВыберите продолжение:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
					[InlineKeyboardButton(text="Только ночная", callback_data="shift:night")],
					[InlineKeyboardButton(text="День+Ночь", callback_data="shift:day_night")]
				]))
			elif current_shift.type == "night":
				# Уже есть ночная - предлагаем дневную или комбо  
				# Ночная смена записана на следующий день, показываем правильную дату
				from datetime import timedelta
				night_shift_date = shift_date + timedelta(days=1)
				await message.reply(f"У вас уже есть ночная смена на {night_shift_date.strftime('%d.%m')}.\nВыберите продолжение:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
					[InlineKeyboardButton(text="Только дневная", callback_data="shift:day")],
					[InlineKeyboardButton(text="День+Ночь", callback_data="shift:day_night")]
				]))
			else:
				# Уже комбо - просто стандартная клавиатура
				await message.reply("Выберите смену:", reply_markup=day_night_kb())
		else:
			# Нет смены - стандартная клавиатура
			await message.reply("Выберите смену:", reply_markup=day_night_kb())
	else:
		# shift_type задан явно через аргумент команды
		if shift_type == "day_night":
			# Создаем дневную смену на сегодня
			await _upsert_shift(session, chat_id, user_id, shift_date, "day")
			await schedule_reminders_for_shift(chat_id, shift_date, "day", session)
			
			# Создаем ночную смену на СЕГОДНЯ (дата начала)
			from datetime import timedelta
			next_day = shift_date + timedelta(days=1)
			await _upsert_shift(session, chat_id, user_id, shift_date, "night")
			await schedule_reminders_for_shift(chat_id, shift_date, "night", session)
			
			reply = await message.reply(f"✅ Комбо смена записана:\n🌅 Дневная: {shift_date.strftime('%d.%m')}\n🌙 Ночная: {next_day.strftime('%d.%m')}")
		elif shift_type == "night":
			# 🌙 Ночная смена: записываем на дату НАЧАЛА (сегодня)
			from datetime import timedelta
			next_day = shift_date + timedelta(days=1)
			await _upsert_shift(session, chat_id, user_id, shift_date, "night")
			await schedule_reminders_for_shift(chat_id, shift_date, "night", session)
			
			reply = await message.reply(f"✅ Ночная смена записана: {next_day.strftime('%d.%m')}")
		else:
			# 🌅 Дневная смена: с 09:00 до 21:00 - записываем на текущий день
			await _upsert_shift(session, chat_id, user_id, shift_date, shift_type)
			await schedule_reminders_for_shift(chat_id, shift_date, shift_type, session)
			
			shift_name = {"day": "дневная"}[shift_type]
			reply = await message.reply(f"✅ Смена записана: {shift_name} {shift_date.strftime('%d.%m')}")
		
		asyncio.create_task(_autodelete(message, reply))


@router.callback_query(F.data.startswith("shift:"))
async def cb_shift(callback: CallbackQuery, session: AsyncSession):
	shift_type = callback.data.split(":", 1)[1]
	chat_id = callback.message.chat.id
	user_id = callback.from_user.id

	# Idempotency: skip if this callback id was already processed
	cb_id = f"{callback.id}"
	existing = await session.get(ProcessedCallback, cb_id)
	if existing:
		await callback.answer()
		return

	group = await ensure_group(session, chat_id, callback.message.chat.title)
	shift_date = local_today(group.tz)
	now = local_now(group.tz)
	current_time = now.time()
	
	# Проверяем есть ли уже смена у пользователя на эту дату
	existing_shift = await session.execute(
		select(Shift).where(
			Shift.chat_id == chat_id,
			Shift.user_id == user_id,
			Shift.date == shift_date
		)
	)
	existing_shift = existing_shift.scalar_one_or_none()
	
	# Если уже есть смена, предупреждаем
	if existing_shift:
		shift_names = {
			"day": "дневная",
			"night": "ночная", 
			"day_night": "комбо (день+ночь)"
		}
		shift_name = shift_names.get(existing_shift.type, existing_shift.type)
		await callback.answer(f"⚠️ У вас уже выбрана {shift_name} смена на {shift_date.strftime('%d.%m')}!", show_alert=True)
		return
	
	# Проверяем время для выбора смены
	from datetime import time as dt_time
	
	if shift_type == "day":
		# Дневная смена: можно выбрать только до 21:00
		if current_time >= dt_time(21, 0):
			await callback.answer("❌ Дневную смену нельзя выбрать после 21:00! Дневная смена работает с 09:00 до 21:00.", show_alert=True)
			return
			
	elif shift_type == "night":
		# Ночная смена: можно выбрать с 21:00 до 09:00 следующего дня
		if current_time < dt_time(21, 0) and current_time >= dt_time(9, 0):
			await callback.answer("❌ Ночную смену нельзя выбрать с 09:00 до 21:00! Ночная смена работает с 21:00 до 09:00.", show_alert=True)
			return
			
	elif shift_type == "day_night":
		# Комбо смена: можно выбрать с 09:00 до 21:00 (чтобы успеть дневную часть)
		if current_time < dt_time(9, 0) or current_time >= dt_time(21, 0):
			await callback.answer("❌ Комбо смену нельзя выбрать с 21:00 до 09:00! Дневная часть работает с 09:00 до 21:00.", show_alert=True)
			return

	if shift_type == "day_night":
		# Создаем дневную смену на сегодня
		await _upsert_shift(session, chat_id, user_id, shift_date, "day")
		await schedule_reminders_for_shift(chat_id, shift_date, "day", session)
		
		# Создаем ночную смену на СЕГОДНЯ (дата начала), а не на следующий день
		from datetime import timedelta
		next_day = shift_date + timedelta(days=1)
		await _upsert_shift(session, chat_id, user_id, shift_date, "night")  # <- дата НАЧАЛА
		await schedule_reminders_for_shift(chat_id, shift_date, "night", session)
		
		# Отправляем подтверждение и удаляем кнопки
		await callback.message.edit_text(
			f"✅ **Комбо смена записана!**\n\n"
			f"🌅 **Дневная:** {shift_date.strftime('%d.%m')} (09:00-21:00)\n"
			f"🌙 **Ночная:** {next_day.strftime('%d.%m')} (21:00-09:00)\n\n"
			f"📸 Отправляйте фото во время смены для записи в табель!"
		)
	elif shift_type == "night":
		# 🌙 Ночная смена: с 21:00 до 09:00 - записываем на дату НАЧАЛА (сегодня)
		from datetime import timedelta
		next_day = shift_date + timedelta(days=1)
		
		# ВАЖНО: записываем смену на дату НАЧАЛА (shift_date), а не конца!
		await _upsert_shift(session, chat_id, user_id, shift_date, "night")
		await schedule_reminders_for_shift(chat_id, shift_date, "night", session)
		
		# Отправляем подтверждение и удаляем кнопки (показываем дату ОКОНЧАНИЯ для пользователя)
		await callback.message.edit_text(
			f"✅ **Ночная смена записана!**\n\n"
			f"🌙 **Дата в табеле:** {next_day.strftime('%d.%m')}\n"
			f"⏰ **Время:** {shift_date.strftime('%d.%m')} 21:00 - {next_day.strftime('%d.%m')} 09:00\n\n"
			f"📸 Отправляйте фото во время смены для записи в табель!"
		)
	else:
		# 🌅 Дневная смена: с 09:00 до 21:00 - записываем на текущий день
		await _upsert_shift(session, chat_id, user_id, shift_date, shift_type)
		await schedule_reminders_for_shift(chat_id, shift_date, shift_type, session)
		
		# Отправляем подтверждение и удаляем кнопки
		await callback.message.edit_text(
			f"✅ **Дневная смена записана!**\n\n"
			f"🌅 **Дата:** {shift_date.strftime('%d.%m')}\n"
			f"⏰ **Время:** 09:00 - 21:00\n\n"
			f"📸 Отправляйте фото во время смены для записи в табель!"
		)
	
	# mark processed
	session.add(ProcessedCallback(id=cb_id, created_at=datetime.utcnow()))
	await session.commit()
	await callback.answer()


@router.message(Command("today"))
async def cmd_today(message: Message, session: AsyncSession):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return

	chat_id = message.chat.id
	group = await ensure_group(session, chat_id, message.chat.title)
	today = local_today(group.tz)

	# Получаем смены на сегодня
	q_today = (
		select(Shift.type, Member.display_name, Member.user_id)
		.join(Member, (Member.chat_id == Shift.chat_id) & (Member.user_id == Shift.user_id))
		.where(Shift.chat_id == chat_id, Shift.date == today)
		.order_by(Shift.type, Member.display_name)
	)
	rows_today = (await session.execute(q_today)).all()
	
	# Получаем ночные смены на завтра (чтобы найти комбо смены)
	from datetime import timedelta
	tomorrow = today + timedelta(days=1)
	q_tomorrow = (
		select(Shift.type, Member.display_name, Member.user_id)
		.join(Member, (Member.chat_id == Shift.chat_id) & (Member.user_id == Shift.user_id))
		.where(Shift.chat_id == chat_id, Shift.date == tomorrow, Shift.type == "night")
		.order_by(Member.display_name)
	)
	rows_tomorrow = (await session.execute(q_tomorrow)).all()
	
	if not rows_today and not rows_tomorrow:
		reply = await message.reply("Сегодня смен нет.")
		asyncio.create_task(_autodelete(message, reply))
		return
	
	def mention(uid: int, name: str) -> str:
		return f"<a href=\"tg://user?id={uid}\">{name}</a>"
	
	# Группируем по пользователям для определения комбо смен
	user_shifts = {}
	
	# Обрабатываем сегодняшние смены
	for shift_type, name, uid in rows_today:
		if uid not in user_shifts:
			user_shifts[uid] = {"name": name, "types": []}
		user_shifts[uid]["types"].append(shift_type)
	
	# Обрабатываем завтрашние ночные смены (для комбо)
	for shift_type, name, uid in rows_tomorrow:
		if uid not in user_shifts:
			user_shifts[uid] = {"name": name, "types": []}
		user_shifts[uid]["types"].append("night_tomorrow")
	
	# Формируем вывод
	day_names = []
	night_names = []
	combo_names = []
	
	for uid, data in user_shifts.items():
		types = data["types"]
		name = data["name"]
		mention_text = mention(uid, name)
		
		if "day" in types and "night_tomorrow" in types:
			# Комбо смена: день сегодня + ночь завтра
			combo_names.append(mention_text)
		elif "day" in types:
			# Только дневная
			day_names.append(mention_text)
		elif "night" in types:
			# Только ночная сегодня
			night_names.append(mention_text)
		elif "night_tomorrow" in types:
			# Только ночная завтра (начинается сегодня в 21:00)
			night_names.append(mention_text)
	
	text = ""
	if day_names:
		text += "🌅 День: " + ", ".join(day_names) + "\n"
	if night_names:
		text += "🌙 Ночь: " + ", ".join(night_names) + "\n"
	if combo_names:
		text += "🌅 День/🌙 Ночь: " + ", ".join(combo_names)
		
	reply = await message.reply(text.strip(), parse_mode="HTML")
	asyncio.create_task(auto_delete_message(message, 3))


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_bot_added(event: ChatMemberUpdated, session: AsyncSession):
	if event.chat.type not in {"group", "supergroup"}:
		return
	if not event.new_chat_member or not event.new_chat_member.user.is_bot:
		return
	# Guard: send welcome only once
	group = await ensure_group(session, event.chat.id, event.chat.title)
	if group.welcomed_at:
		return
	await event.bot.send_message(event.chat.id, "Привет! Я бот для клинеров. Нажмите, чтобы начать регистрацию.", reply_markup=start_kb(event.from_user.username if event.from_user else None))
	group.welcomed_at = datetime.utcnow()
	session.add(group)
	await session.commit()


@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type in {"group", "supergroup"}:
		reply = await message.reply(
			"Я бот для клинеров. Для начала — зарегистрируйтесь:", reply_markup=start_kb(message.from_user.username if message.from_user else None))
		asyncio.create_task(_autodelete(message, reply))
	else:
		await message.answer("Добавьте меня в рабочую группу и используйте команды там: /register, /shift, /today.")


@router.callback_query(F.data == "start:register", StateFilter(None))
async def cb_start_register(callback: CallbackQuery, state: FSMContext):
	st = await state.get_state()
	if st in (RegStates.waiting_name.state, RegStates.waiting_gender.state):
		await callback.answer()
		return
	await callback.message.answer(f"Отправьте имя/ник (1..{MAX_NAME_LEN} символов) или нажмите кнопку", reply_markup=register_name_kb(callback.from_user.username if callback.from_user else None))
	await state.set_state(RegStates.waiting_name)
	await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	text = (
		"🤖 **HELP CLEANERS | KZN**\n\n"
		"**О боте:**\n"
		"Этот бот создан для автоматизации работы клининговых бригад. "
		"Он помогает вести учет смен, отслеживать дубликаты фотографий, "
		"вести табель и управлять персоналом.\n\n"
		
		"**📋 Основные функции:**\n"
		"• Регистрация клинеров в группе\n"
		"• Управление сменами (дневные/ночные/комбо)\n"
		"• Автоматическое ведение табеля\n"
		"• Детекция дубликатов фотографий\n"
		"• Система напоминаний о сменах\n\n"
		
		"**🔧 Доступные команды:**\n\n"
		"**👤 Регистрация и управление:**\n"
		"• 📝 /register — регистрация в группе\n"
		"• 👥 /cleaners — список всех клинеров\n\n"
		
		"**⏰ Смены и табель:**\n"
		"• 🕐 /shift — установить смену (день/ночь/комбо)\n"
		"• 📅 /today — кто сегодня на смене\n"
		"• 📊 /tabel — мой табель за 30 дней\n"
		"• 📋 /myshifts — мои смены\n\n"
		
		"**📸 Фотографии:**\n"
		"• Отправляйте фото для подтверждения работы\n"
		"• Автоматическое обнаружение дубликатов\n"
		"• 🔍 /duplicates — список дубликатов за 30 дней\n\n"
		
		"**🔒 Команды владельца:**\n"
		"• ➕ /addchat — добавить группу в разрешенные\n"
		"• ➖ /removechat — убрать группу из разрешенных\n"
		"• 📋 /listchats — список разрешенных групп\n\n"
		
		"**ℹ️ Дополнительно:**\n"
		"• ❓ /help — эта справка\n"
		"• 🔄 /reset — сброс состояния регистрации\n\n"
		
		"**💡 Как это работает:**\n"
		"1. Зарегистрируйтесь командой 📝 /register\n"
		"2. Установите смену командой 🕐 /shift\n"
		"3. Отправляйте фото во время работы\n"
		"4. Бот автоматически ведет табель\n"
		"5. Проверяйте прогресс командой 📊 /tabel\n\n"
		
		"**👨‍💻 Создатель:** Азиз\n"
		"**🔐 Версия:** 2.0\n"
		"**📅 Обновлено:** 2025\n\n"
		
		"*Нажмите на любую команду выше, чтобы выполнить её!*"
	)
	
	if message.chat.type in {"group", "supergroup"}:
		reply = await message.reply(text, parse_mode="Markdown")
		# Удаляем только команду пользователя, справку оставляем
		asyncio.create_task(auto_delete_message(message, 3))
	else:
		await message.answer(text, parse_mode="Markdown")


@router.message(Command("addchat"))
async def cmd_addchat(message: Message, session: AsyncSession):
	"""
	🔒 Команда владельца: добавить новую группу в список разрешенных.
	"""
	# Проверяем права владельца
	if not await check_owner_permission(message):
		await message.reply("❌ У вас нет прав для этой команды.")
		return
	
	# Проверяем что команда вызвана в группе
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("❌ Команда доступна только в группе.")
		return
	
	chat_id = message.chat.id
	chat_title = message.chat.title
	
	# Проверяем существующую запись
	existing_result = await session.execute(
		select(AllowedChat).where(AllowedChat.chat_id == chat_id)
	)
	existing = existing_result.scalar_one_or_none()
	
	if existing:
		# Если запись существует, но неактивна - активируем
		if not existing.is_active:
			existing.is_active = True
			existing.added_at = datetime.utcnow()
			existing.chat_title = chat_title
			await session.commit()
			
			# Добавляем в конфиг ALLOWED_CHATS
			from app.config import ALLOWED_CHATS
			if chat_id not in ALLOWED_CHATS:
				ALLOWED_CHATS.append(chat_id)
			
			await message.reply(f"✅ Группа {chat_title} снова активирована!")
		else:
			# Группа уже активна
			await message.reply(f"✅ Группа {chat_title} уже в списке разрешенных.")
		return
	
	# Создаем новую запись
	new_allowed = AllowedChat(
		chat_id=chat_id,
		chat_title=chat_title,
		added_by=OWNER_ID,
		added_at=datetime.utcnow(),
		is_active=True
	)
	session.add(new_allowed)
	await session.commit()
	
	# Добавляем группу в конфиг ALLOWED_CHATS
	from app.config import ALLOWED_CHATS
	if chat_id not in ALLOWED_CHATS:
		ALLOWED_CHATS.append(chat_id)
	
	await message.reply(f"✅ Группа {chat_title} добавлена в список разрешенных!")


@router.message(Command("removechat"))
async def cmd_removechat(message: Message, session: AsyncSession):
	"""
	🔒 Команда владельца: убрать группу из списка разрешенных.
	"""
	# Проверяем права владельца
	if not await check_owner_permission(message):
		await message.reply("❌ У вас нет прав для этой команды.")
		return
	
	# Проверяем что команда вызвана в группе
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("❌ Команда доступна только в группе.")
		return
	
	chat_id = message.chat.id
	chat_title = message.chat.title
	
	# Убираем группу из БД
	result = await session.execute(
		update(AllowedChat)
		.where(AllowedChat.chat_id == chat_id)
		.values(is_active=False)
	)
	await session.commit()
	
	# Убираем группу из конфига ALLOWED_CHATS
	from app.config import ALLOWED_CHATS
	if chat_id in ALLOWED_CHATS:
		ALLOWED_CHATS.remove(chat_id)
	
	if result.rowcount > 0:
		await message.reply(f"✅ Группа {chat_title} убрана из списка разрешенных. Бот покинет группу.")
		# Бот уходит из группы
		try:
			await message.chat.leave()
		except Exception:
			pass
	else:
		await message.reply(f"❌ Группа {chat_title} не найдена в списке разрешенных.")


@router.message(Command("listchats"))
async def cmd_listchats(message: Message, session: AsyncSession):
	"""
	🔒 Команда владельца: показать список разрешенных групп.
	Автоматически проверяет статус бота в каждой группе.
	"""
	# Проверяем права владельца
	if not await check_owner_permission(message):
		await message.reply("❌ У вас нет прав для этой команды.")
		return
	
	from aiogram import Bot
	from app.config import BOT_TOKEN, ALLOWED_CHATS
	bot = Bot(token=BOT_TOKEN)
	
	# Получаем список разрешенных групп из БД
	allowed_chats = await session.execute(
		select(AllowedChat).where(AllowedChat.is_active == True).order_by(AllowedChat.added_at.desc())
	)
	
	chats = allowed_chats.scalars().all()
	
	active_chats = []
	inactive_chats = []
	
	# Проверяем каждую группу - состоит ли бот в ней
	for chat in chats:
		try:
			# Пытаемся получить информацию о группе
			chat_info = await bot.get_chat(chat.chat_id)
			# Если бот получил информацию, значит он состоит в группе
			active_chats.append({
				"chat_id": chat.chat_id,
				"chat_title": chat_info.title or chat.chat_title,
				"added_at": chat.added_at,
				"status": "✅ Активна"
			})
			# Обновляем название группы в БД, если изменилось
			if chat_info.title and chat_info.title != chat.chat_title:
				chat.chat_title = chat_info.title
				await session.commit()
		except Exception:
			# Если ошибка - бот не состоит в группе, помечаем как неактивную
			inactive_chats.append({
				"chat_id": chat.chat_id,
				"chat_title": chat.chat_title,
				"added_at": chat.added_at,
				"status": "❌ Неактивна"
			})
			# Автоматически деактивируем группу в БД
			chat.is_active = False
			# Убираем из конфига ALLOWED_CHATS
			if chat.chat_id in ALLOWED_CHATS:
				ALLOWED_CHATS.remove(chat.chat_id)
	
	# Сохраняем изменения
	await session.commit()
	
	# Проверяем группы из конфига
	config_chats = []
	for chat_id in ALLOWED_CHATS:
		# Проверяем есть ли уже в БД
		existing = await session.execute(
			select(AllowedChat).where(AllowedChat.chat_id == chat_id)
		)
		if not existing.scalar_one_or_none():
			try:
				chat_info = await bot.get_chat(chat_id)
				config_chats.append({
					"chat_id": chat_id,
					"chat_title": chat_info.title or f"Группа (ID: {chat_id})",
					"added_at": None,
					"status": "✅ Активна (из конфига)"
				})
			except Exception:
				# Группа из конфига недоступна
				ALLOWED_CHATS.remove(chat_id)
	
	# Объединяем списки
	all_active = active_chats + config_chats
	
	if not all_active and not inactive_chats:
		await message.reply("📋 Список разрешенных групп пуст.")
		return
	
	text = "📋 РАЗРЕШЁННЫЕ ГРУППЫ\n\n"
	
	if all_active:
		text += "🟢 АКТИВНЫЕ ГРУППЫ:\n\n"
		for chat in all_active:
			text += f"{chat['status']} {chat['chat_title']}\n"
			text += f"   ID: {chat['chat_id']}\n"
			if chat.get('added_at'):
				text += f"   Добавлена: {chat['added_at'].strftime('%d.%m.%Y %H:%M')}\n"
			text += "\n"
	
	if inactive_chats:
		text += "\n🔴 НЕАКТИВНЫЕ (бот не состоит в группе):\n\n"
		for chat in inactive_chats:
			text += f"{chat['status']} {chat['chat_title']}\n"
			text += f"   ID: {chat['chat_id']}\n"
			if chat.get('added_at'):
				text += f"   Была добавлена: {chat['added_at'].strftime('%d.%m.%Y %H:%M')}\n"
			text += "\n"
		text += "💡 Неактивные группы были автоматически удалены из списка разрешённых.\n"
	
	await message.reply(text)


@router.message(Command("myshifts"))
async def cmd_myshifts(message: Message, session: AsyncSession):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return
	chat_id = message.chat.id
	q = (
		select(Shift.date, Shift.type)
		.where(Shift.chat_id == chat_id, Shift.user_id == message.from_user.id)
		.order_by(Shift.date.desc())
		.limit(30)
	)
	rows = (await session.execute(q)).all()
	if not rows:
		reply = await message.reply("Смен не найдено.")
		asyncio.create_task(_autodelete(message, reply))
		return
	
	# Формируем текст с человеческими названиями смен
	shift_names = {"day": "🌅 дневная", "night": "🌙 ночная"}
	text = "Ваши смены (посл. 30):\n" + "\n".join(
		f"{d.strftime('%d.%m.%Y')} — {shift_names.get(t, t)}" for d, t in rows
	)
	reply = await message.reply(text)
	asyncio.create_task(_autodelete(message, reply))
	# Удаляем команду пользователя
	asyncio.create_task(auto_delete_message(message, 3))


@router.message(Command("cleaners"))
async def cmd_cleaners(message: Message, session: AsyncSession):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return
	chat_id = message.chat.id
	# Ensure group record exists (in case /cleaners вызывают первым)
	await ensure_group(session, chat_id, message.chat.title)
	rows = (await session.execute(
		select(Member.display_name, Member.gender, Member.user_id)
		.where(Member.chat_id == chat_id)
		.order_by(Member.display_name)
	)).all()
	if not rows:
		reply = await message.reply("Нет зарегистрированных клинеров. Используйте /register, чтобы добавиться.")
		asyncio.create_task(auto_delete_message(message, 3))
		return
	def gender_mark(g: str | None) -> str:
		return "М" if g == "m" else ("Ж" if g == "f" else "?")
	def mention(uid: int, name: str) -> str:
		return f"<a href=\"tg://user?id={uid}\">{name}</a>"
	text = "Клинеры:\n" + "\n".join(f"{gender_mark(g)} — {mention(uid, name)}" for name, g, uid in rows)
	reply = await message.reply(text, parse_mode="HTML")
	asyncio.create_task(_autodelete(message, reply))
	# Удаляем команду пользователя
	asyncio.create_task(auto_delete_message(message, 3))


@router.message(Command("duplicates"))
async def cmd_duplicates(message: Message, session: AsyncSession):
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return

	chat_id = message.chat.id
	group = await ensure_group(session, chat_id, message.chat.title)
	today = local_today(group.tz)

	# Get duplicates from last 30 days
	from datetime import timedelta
	thirty_days_ago = today - timedelta(days=30)
	
	# Получаем дубликаты с информацией об оригинале и дубликате
	rows = await session.execute(
		select(PhotoDuplicate, Photo)
		.join(Photo, PhotoDuplicate.duplicate_photo_id == Photo.id)
		.where(PhotoDuplicate.chat_id == chat_id, PhotoDuplicate.duplicate_date >= thirty_days_ago)
		.order_by(PhotoDuplicate.created_at.desc())
		.limit(50)  # Limit to prevent spam
	)
	rows = rows.all()

	if not rows:
		reply = await message.reply("Дубликатов не найдено.")
		asyncio.create_task(auto_delete_message(message, 3))
		return
	
	# Формируем читаемый список дубликатов
	lines = []
	lines.append("Дубликаты за 30 дней\n")
	
	for i, (dup, dup_photo) in enumerate(rows, 1):
		# Получаем информацию о пользователях
		dup_user = await session.get(Member, {"chat_id": chat_id, "user_id": dup_photo.user_id})
		
		# Получаем оригинальное фото отдельно
		orig_photo_result = await session.execute(
			select(Photo).where(Photo.id == dup.original_photo_id)
		)
		orig_photo = orig_photo_result.scalar_one_or_none()
		
		if orig_photo:
			orig_user = await session.get(Member, {"chat_id": chat_id, "user_id": orig_photo.user_id})
			orig_name = orig_user.display_name if orig_user else f"ID:{orig_photo.user_id}"
		else:
			orig_name = "Неизвестно"
		
		dup_name = dup_user.display_name if dup_user else f"ID:{dup_photo.user_id}"
		
		# Создаем ссылки на пользователей
		orig_link = f"[{orig_name}](tg://user?id={orig_photo.user_id})" if orig_photo else orig_name
		dup_link = f"[{dup_name}](tg://user?id={dup_photo.user_id})"
		
		# Сначала оригинал, потом дубликат
		lines.append(f"{i}. Оригинал: {orig_link}")
		lines.append(f"   {dup.original_date.strftime('%d.%m')} {str(dup.original_time)[:8]}")
		lines.append(f"   Дубликат: {dup_link}")
		lines.append(f"   {dup.duplicate_date.strftime('%d.%m')} {str(dup.duplicate_time)[:8]}")
		lines.append("")
	
	# Отправляем сообщение с форматированием
	response_text = "\n".join(lines)
	if len(response_text) > 4096:
		# Разбиваем на части если слишком длинное
		parts = [response_text[i:i+4096] for i in range(0, len(response_text), 4096)]
		for i, part in enumerate(parts):
			await message.reply(f"Часть {i+1}/{len(parts)}:\n{part}", parse_mode="Markdown")
	else:
		await message.reply(response_text, parse_mode="Markdown")
	
	# Удаляем команду пользователя
	asyncio.create_task(auto_delete_message(message, 3))


@router.message(Command("remove"))
async def cmd_remove(message: Message, session: AsyncSession):
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return
	chat_id = message.chat.id
	await ensure_group(session, chat_id, message.chat.title)
	admin = await is_admin(chat_id, message.from_user.id, message.bot)
	reply_msg = await message.reply("Выберите действие:", reply_markup=remove_menu_kb(admin))
	# Удаляем команду пользователя
	asyncio.create_task(auto_delete_message(message, 3))


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext):
	"""Сброс FSM состояния - если бот 'завис'"""
	current_state = await state.get_state()
	if current_state:
		await state.clear()
		reply = await message.reply("✅ Состояние сброшено. Можете начать заново с /register")
	else:
		reply = await message.reply("ℹ️ Нет активного состояния для сброса")
	# Удаляем команду пользователя
	asyncio.create_task(auto_delete_message(message, 3))


@router.callback_query(F.data == "rm:self")
async def cb_rm_self(callback: CallbackQuery, session: AsyncSession):
	chat_id = callback.message.chat.id
	user_id = callback.from_user.id
	member = await session.get(Member, {"chat_id": chat_id, "user_id": user_id})
	if not member:
		reply_msg = await callback.message.answer("Вы не зарегистрированы в этой группе.")
		await callback.answer()
		return
	# Также удаляем все смены участника, чтобы он не отображался в /today после повторной регистрации
	await session.execute(delete(Shift).where(Shift.chat_id == chat_id, Shift.user_id == user_id))
	await session.delete(member)
	await session.commit()
	reply_msg = await callback.message.answer("Ваш профиль удалён из этой группы.")
	await callback.answer()
	
	# Удаляем сообщение через 5 секунд
	asyncio.create_task(auto_delete_message(reply_msg, 5))


@router.callback_query(F.data == "rm:other")
async def cb_rm_other(callback: CallbackQuery, session: AsyncSession):
	chat_id = callback.message.chat.id
	if not await is_admin(chat_id, callback.from_user.id, callback.bot):
		await callback.answer("Только администратор может удалять других.", show_alert=True)
		return
	rows = (await session.execute(select(Member.display_name, Member.user_id).where(Member.chat_id == chat_id).order_by(Member.display_name))).all()
	options = [(name, uid) for name, uid in rows if uid != callback.from_user.id]
	if not options:
		reply_msg = await callback.message.answer("В этой группе пока нет других клинеров для удаления.")
		await callback.answer()
		return
	reply_msg = await callback.message.answer("Кого удалить?", reply_markup=members_choice_kb(options))
	await callback.answer()
	
	# Удаляем сообщение через 1 минуту
	asyncio.create_task(auto_delete_message(reply_msg, 60))


@router.callback_query(F.data.startswith("rmuser:"))
async def cb_rm_user(callback: CallbackQuery, session: AsyncSession):
	chat_id = callback.message.chat.id
	if not await is_admin(chat_id, callback.from_user.id, callback.bot):
		await callback.answer("Только администратор может удалять других.", show_alert=True)
		return
	target_user_id = int(callback.data.split(":", 1)[1])
	member = await session.get(Member, {"chat_id": chat_id, "user_id": target_user_id})
	if not member:
		reply_msg = await callback.message.answer("Участник уже не найден в базе.")
		await callback.answer()
		return
	# Также удаляем все смены участника, чтобы он не отображался в /today после повторной регистрации
	await session.execute(delete(Shift).where(Shift.chat_id == chat_id, Shift.user_id == target_user_id))
	await session.delete(member)
	await session.commit()
	reply_msg = await callback.message.answer(f"Удалён клинер: {member.display_name}.")
	await callback.answer()
	
	# Удаляем сообщение через 5 секунд
	asyncio.create_task(auto_delete_message(reply_msg, 5))


@router.message(Command("tabel"))
async def cmd_tabel(message: Message, session: AsyncSession):
	"""
	Показать красивый табель за 30 дней для текущего пользователя.
	"""
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return

	chat_id = message.chat.id
	user_id = message.from_user.id if message.from_user else 0
	
	# Проверяем регистрацию клинера
	member = await session.get(Member, {"chat_id": chat_id, "user_id": user_id})
	if not member:
		reply = await message.reply("❌ Зарегистрируйтесь: /register")
		asyncio.create_task(auto_delete_message(message, 5))
		asyncio.create_task(auto_delete_message(reply, 10))
		return

	group = await session.get(Group, chat_id)
	if not group:
		reply = await message.reply("❌ Группа не найдена.")
		asyncio.create_task(auto_delete_message(message, 5))
		asyncio.create_task(auto_delete_message(reply, 10))
		return

	now = local_now(group.tz)
	today = now.date()
	
	# Получаем ВСЕ смены за текущий месяц (с 1 числа до конца месяца)
	first_day_of_month = today.replace(day=1)
	
	# Последний день текущего месяца
	if today.month == 12:
		last_day_of_month = today.replace(day=31)
	else:
		next_month = today.replace(month=today.month + 1, day=1)
		last_day_of_month = next_month - timedelta(days=1)
	
	# Получаем табель за текущий месяц
	timesheet_result = await session.execute(
		select(Timesheet).where(
			Timesheet.chat_id == chat_id,
			Timesheet.user_id == user_id,
			Timesheet.date >= first_day_of_month,
			Timesheet.date <= last_day_of_month
		).order_by(Timesheet.date.asc())
	)
	timesheet_entries = timesheet_result.scalars().all()
	
	if not timesheet_entries:
		reply = await message.reply(
			f"📋 ТАБЕЛЬ: {member.display_name}\n\n"
			f"❌ Смен не найдено.\n\n"
			f"💡 /add_shift — добавить смену"
		)
		# Удаляем и команду, и ответ через 10 секунд
		asyncio.create_task(auto_delete_message(message, 10))
		asyncio.create_task(auto_delete_message(reply, 10))
		return
	
	# Группируем по дате
	entries_by_date = {}
	for entry in timesheet_entries:
		date_key = entry.date
		if date_key not in entries_by_date:
			entries_by_date[date_key] = []
		entries_by_date[date_key].append(entry)
	
	# Подсчитываем статистику
	total_shifts = len(timesheet_entries)
	day_shifts = sum(1 for e in timesheet_entries if e.shift_type == "day")
	night_shifts = sum(1 for e in timesheet_entries if e.shift_type == "night")
	total_days = len(entries_by_date)
	
	# Формируем профессиональный отчет
	response_lines = []
	response_lines.append(f"📋 ТАБЕЛЬ: {member.display_name.upper()}")
	response_lines.append(f"📅 {first_day_of_month.strftime('%B %Y').capitalize()}")
	response_lines.append("")
	response_lines.append(f"📊 Статистика:")
	response_lines.append(f"• Рабочих дней: {total_days}")
	response_lines.append(f"• Всего смен: {total_shifts}")
	response_lines.append(f"• 🌅 Дневных: {day_shifts}")
	response_lines.append(f"• 🌙 Ночных: {night_shifts}")
	response_lines.append("")
	response_lines.append("─────────────────────")
	response_lines.append("")
	
	# Отображаем записи по датам
	for work_date in sorted(entries_by_date.keys(), reverse=True):
		date_entries = entries_by_date[work_date]
		date_str = work_date.strftime('%d.%m.%Y')
		
		# День недели
		weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
		weekday = weekday_names[work_date.weekday()]
		
		# Собираем смены
		shift_icons = []
		for entry in date_entries:
			if entry.shift_type == "day":
				shift_icons.append("🌅")
			elif entry.shift_type == "night":
				shift_icons.append("🌙")
		
		shifts_str = " ".join(shift_icons)
		response_lines.append(f"{date_str} ({weekday}) {shifts_str}")
	
	response_lines.append("")
	response_lines.append("─────────────────────")
	response_lines.append("💡 /add_shift — добавить смену")
	response_lines.append("🗑 /remove_shift — удалить смену")
	
	response_text = "\n".join(response_lines)
	
	# Отправляем табель (НЕ удаляем - это важная информация)
	await message.reply(response_text)
	
	# Удаляем только команду пользователя
	asyncio.create_task(auto_delete_message(message, 3))


@router.callback_query(F.data.startswith("analyze:"))
async def cb_analyze_user(callback: CallbackQuery, session: AsyncSession):
	chat_id = callback.message.chat.id
	target_user_id = int(callback.data.split(":", 1)[1])
	
	await callback.answer("⏳ Анализирую дубликаты...")
	
	try:
		# Получаем информацию о пользователе
		chat_member = await callback.bot.get_chat_member(chat_id, target_user_id)
		user_name = chat_member.user.username or f"ID:{target_user_id}"
		
		# Анализируем дубликаты пользователя
		duplicates = await analyze_user_duplicates(session, chat_id, target_user_id)
		
		if not duplicates:
			await callback.message.edit_text(f"✅ У {user_name} дубликатов не найдено.")
			return
		
		# Формируем отчет
		report = f"📊 Анализ дубликатов для {user_name}:\n\n"
		total_duplicates = 0
		
		for dup in duplicates:
			total_duplicates += 1
			report += f"🔍 Дубликат #{total_duplicates}:\n"
			report += f"   📅 Оригинал: {dup.original_date.isoformat()} в {str(dup.original_time)[:8]}\n"
			report += f"   📅 Дубликат: {dup.duplicate_date.isoformat()} в {str(dup.duplicate_time)[:8]}\n"
			report += f"   ⏱️ Разница: {dup.time_diff}\n\n"
		
		report += f"📈 Итого найдено дубликатов: {total_duplicates}"
		
		await callback.message.edit_text(report)
		
	except Exception as e:
		await callback.message.edit_text(f"❌ Ошибка при анализе: {str(e)}")


async def analyze_user_duplicates(session: AsyncSession, chat_id: int, user_id: int) -> list:
	"""
	Анализирует дубликаты конкретного пользователя.
	Возвращает список дубликатов с дополнительной информацией.
	"""
	# Получаем все дубликаты пользователя
	q = (
		select(PhotoDuplicate)
		.where(PhotoDuplicate.chat_id == chat_id)
		.where(
			# Дубликат от этого пользователя
			(PhotoDuplicate.duplicate_photo_id.in_(
				select(Photo.id).where(Photo.chat_id == chat_id, Photo.user_id == user_id)
			)) |
			# Или оригинал от этого пользователя
			(PhotoDuplicate.original_photo_id.in_(
				select(Photo.id).where(Photo.chat_id == chat_id, Photo.user_id == user_id)
			))
		)
		.order_by(PhotoDuplicate.created_at.desc())
	)
	
	duplicates = (await session.execute(q)).scalars().all()
	
	# Добавляем информацию о времени между оригиналом и дубликатом
	for dup in duplicates:
		original_dt = datetime.combine(dup.original_date, dup.original_time.replace(tzinfo=None))
		duplicate_dt = datetime.combine(dup.duplicate_date, dup.duplicate_time.replace(tzinfo=None))
		time_diff = abs(duplicate_dt - original_dt)
		
		if time_diff.days > 0:
			dup.time_diff = f"{time_diff.days} дней"
		elif time_diff.seconds > 3600:
			dup.time_diff = f"{time_diff.seconds // 3600} часов"
		elif time_diff.seconds > 60:
			dup.time_diff = f"{time_diff.seconds // 60} минут"
		else:
			dup.time_diff = f"{time_diff.seconds} секунд"
	
	return duplicates


async def _upsert_shift(session: AsyncSession, chat_id: int, user_id: int, shift_date: date, shift_type: str) -> None:
	# Ensure member exists
	member = await session.get(Member, {"chat_id": chat_id, "user_id": user_id})
	if not member:
		raise ValueError("Сначала зарегистрируйтесь: /register")

	existing = await session.execute(
		select(Shift).where(Shift.chat_id == chat_id, Shift.user_id == user_id, Shift.date == shift_date)
	)
	shift = existing.scalar_one_or_none()
	if shift:
		shift.type = shift_type
	else:
		shift = Shift(
			chat_id=chat_id,
			user_id=user_id,
			date=shift_date,
			type=shift_type,
			created_at=datetime.utcnow(),
		)
		session.add(shift)
	await session.commit()


async def _autodelete(src: Message, reply: Message, delay: int = 15) -> None:
	try:
		await asyncio.sleep(delay)
		await reply.delete()
		await src.delete()
	except Exception:
		return


# ==================== НОВЫЕ КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ ТАБЕЛЕМ ====================

@router.message(Command("add_shift"))
async def cmd_add_shift(message: Message, session: AsyncSession, state: FSMContext):
	"""
	Команда для добавления смены в табель вручную.
	Клинер выбирает дату и тип смены через кнопки.
	"""
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return

	chat_id = message.chat.id
	user_id = message.from_user.id if message.from_user else 0
	
	# Проверяем регистрацию
	member = await session.get(Member, {"chat_id": chat_id, "user_id": user_id})
	if not member:
		reply = await message.reply("❌ Зарегистрируйтесь: /register")
		asyncio.create_task(auto_delete_message(message, 5))
		asyncio.create_task(auto_delete_message(reply, 10))
		return
	
	group = await ensure_group(session, chat_id, message.chat.title)
	today = local_today(group.tz)
	
	# Создаем кнопки для текущего месяца (с 1 числа до конца месяца)
	keyboard = InlineKeyboardBuilder()
	
	# Первый день текущего месяца
	first_day = today.replace(day=1)
	
	# Последний день текущего месяца
	if today.month == 12:
		last_day = today.replace(day=31)
	else:
		next_month = today.replace(month=today.month + 1, day=1)
		last_day = next_month - timedelta(days=1)
	
	# Генерируем кнопки для всех дней месяца
	current_date = first_day
	while current_date <= last_day:
		date_str = current_date.strftime("%d.%m")
		
		# Подсветка сегодняшнего дня
		if current_date == today:
			button_text = f"📅 Сегодня"
		else:
			button_text = f"📆 {date_str}"
		
		keyboard.button(text=button_text, callback_data=f"addshift_date:{current_date.isoformat()}")
		current_date += timedelta(days=1)
	
	keyboard.adjust(3)  # 3 кнопки в ряд
	
	reply = await message.reply(
		"📋 Добавление смены в табель\n\n"
		"Выберите дату смены:",
		reply_markup=keyboard.as_markup()
	)
	
	await state.set_state(TimesheetStates.waiting_shift_date)
	asyncio.create_task(auto_delete_message(message, 3))


@router.callback_query(F.data.startswith("addshift_date:"))
async def cb_addshift_date(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Обработка выбора даты для добавления смены"""
	shift_date_str = callback.data.split(":", 1)[1]
	shift_date = date.fromisoformat(shift_date_str)
	
	chat_id = callback.message.chat.id
	user_id = callback.from_user.id
	
	# Сохраняем дату в состояние
	await state.update_data(shift_date=shift_date)
	
	# Проверяем, есть ли уже смена на эту дату
	existing_timesheet = await session.execute(
		select(Timesheet).where(
			Timesheet.chat_id == chat_id,
			Timesheet.user_id == user_id,
			Timesheet.date == shift_date
		)
	)
	existing_shifts = existing_timesheet.scalars().all()
	
	if existing_shifts:
		shift_types = [s.shift_type for s in existing_shifts]
		shift_names = {
			"day": "дневная",
			"night": "ночная"
		}
		existing_names = [shift_names.get(t, t) for t in shift_types]
		
		await callback.answer(
			f"⚠️ У вас уже есть смена на {shift_date.strftime('%d.%m.%Y')}: {', '.join(existing_names)}",
			show_alert=True
		)
		return
	
	# Создаем кнопки для выбора типа смены
	keyboard = InlineKeyboardBuilder()
	keyboard.button(text="🌅 Дневная (09:00-21:00)", callback_data="addshift_type:day")
	keyboard.button(text="🌙 Ночная (21:00-09:00)", callback_data="addshift_type:night")
	keyboard.adjust(1)  # По одной кнопке в ряд
	
	await callback.message.edit_text(
		f"📋 Добавление смены\n\n"
		f"📅 Дата: {shift_date.strftime('%d.%m.%Y')}\n\n"
		f"Выберите тип смены:",
		reply_markup=keyboard.as_markup()
	)
	
	await state.set_state(TimesheetStates.waiting_shift_type)
	await callback.answer()


@router.callback_query(F.data.startswith("addshift_type:"))
async def cb_addshift_type(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Обработка выбора типа смены"""
	shift_type = callback.data.split(":", 1)[1]
	
	chat_id = callback.message.chat.id
	user_id = callback.from_user.id
	
	# Получаем дату из состояния
	data = await state.get_data()
	shift_date = data.get("shift_date")
	
	if not shift_date:
		await callback.answer("❌ Ошибка: дата не выбрана", show_alert=True)
		await state.clear()
		return
	
	shift_names = {
		"day": "дневная",
		"night": "ночная"
	}
	shift_name = shift_names.get(shift_type, shift_type)
	
	# Создаем кнопки подтверждения
	keyboard = InlineKeyboardBuilder()
	keyboard.button(text="✅ Да, добавить", callback_data="addshift_confirm:yes")
	keyboard.button(text="❌ Отмена", callback_data="addshift_confirm:no")
	keyboard.adjust(2)
	
	# Сохраняем тип смены в состояние
	await state.update_data(shift_type=shift_type)
	
	await callback.message.edit_text(
		f"📋 Подтверждение добавления\n\n"
		f"📅 Дата: {shift_date.strftime('%d.%m.%Y')}\n"
		f"⏰ Смена: {shift_name}\n\n"
		f"Добавить эту смену в табель?",
		reply_markup=keyboard.as_markup()
	)
	
	await state.set_state(TimesheetStates.waiting_confirm)
	await callback.answer()


@router.callback_query(F.data.startswith("addshift_confirm:"))
async def cb_addshift_confirm(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Подтверждение добавления смены в табель"""
	confirm = callback.data.split(":", 1)[1]
	
	if confirm == "no":
		await callback.message.edit_text("❌ Добавление смены отменено.")
		await state.clear()
		await callback.answer()
		return
	
	chat_id = callback.message.chat.id
	user_id = callback.from_user.id
	
	# Получаем данные из состояния
	data = await state.get_data()
	shift_date = data.get("shift_date")
	shift_type = data.get("shift_type")
	
	if not shift_date or not shift_type:
		await callback.answer("❌ Ошибка: данные не найдены", show_alert=True)
		await state.clear()
		return
	
	# Добавляем смену в табель
	new_timesheet = Timesheet(
		chat_id=chat_id,
		user_id=user_id,
		date=shift_date,
		shift_type=shift_type,
		photo_count=0,  # Фото не учитываются, так как управление ручное
		confirmed_at=datetime.utcnow()
	)
	session.add(new_timesheet)
	await session.commit()
	
	shift_names = {
		"day": "дневная",
		"night": "ночная"
	}
	shift_name = shift_names.get(shift_type, shift_type)
	
	await callback.message.edit_text(
		f"✅ Смена добавлена!\n\n"
		f"📅 {shift_date.strftime('%d.%m.%Y')} — {shift_name}"
	)
	
	# Удаляем сообщение через 10 секунд
	asyncio.create_task(auto_delete_message(callback.message, 10))
	
	await state.clear()
	await callback.answer("✅ Добавлено!")


@router.message(Command("remove_shift"))
async def cmd_remove_shift(message: Message, session: AsyncSession, state: FSMContext):
	"""
	Команда для удаления смены из табеля.
	"""
	# 🔒 Проверяем разрешен ли чат
	if not await check_chat_allowed(message, session):
		return
	
	if message.chat.type not in {"group", "supergroup"}:
		await message.reply("Команда доступна только в группе.")
		return

	chat_id = message.chat.id
	user_id = message.from_user.id if message.from_user else 0
	
	# Проверяем регистрацию
	member = await session.get(Member, {"chat_id": chat_id, "user_id": user_id})
	if not member:
		reply = await message.reply("❌ Зарегистрируйтесь: /register")
		asyncio.create_task(auto_delete_message(message, 5))
		asyncio.create_task(auto_delete_message(reply, 10))
		return
	
	# Получаем все смены пользователя
	timesheets_result = await session.execute(
		select(Timesheet).where(
			Timesheet.chat_id == chat_id,
			Timesheet.user_id == user_id
		).order_by(Timesheet.date.desc())
	)
	timesheets = timesheets_result.scalars().all()
	
	if not timesheets:
		reply = await message.reply("❌ Смен в табеле нет.")
		asyncio.create_task(auto_delete_message(message, 5))
		asyncio.create_task(auto_delete_message(reply, 10))
		return
	
	# Группируем смены по датам для красивого отображения
	shifts_by_date = {}
	for ts in timesheets:
		date_key = ts.date
		if date_key not in shifts_by_date:
			shifts_by_date[date_key] = []
		shifts_by_date[date_key].append(ts)
	
	# Создаем кнопки для каждой даты и типа смены
	keyboard = InlineKeyboardBuilder()
	
	shift_names = {
		"day": "🌅 Дневная",
		"night": "🌙 Ночная"
	}
	
	for work_date in sorted(shifts_by_date.keys(), reverse=True):
		for ts in shifts_by_date[work_date]:
			date_str = ts.date.strftime("%d.%m.%Y")
			shift_name = shift_names.get(ts.shift_type, ts.shift_type)
			button_text = f"{date_str} — {shift_name}"
			
			keyboard.button(
				text=button_text,
				callback_data=f"removeshift:{ts.id}"
			)
	
	keyboard.adjust(1)  # По одной кнопке в ряд
	
	reply = await message.reply(
		"🗑️ Удаление смены из табеля\n\n"
		"Выберите смену для удаления:",
		reply_markup=keyboard.as_markup()
	)
	
	await state.set_state(TimesheetStates.waiting_remove_date)
	asyncio.create_task(auto_delete_message(message, 3))


@router.callback_query(F.data.startswith("removeshift:"))
async def cb_removeshift(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Подтверждение удаления смены"""
	timesheet_id = int(callback.data.split(":", 1)[1])
	
	chat_id = callback.message.chat.id
	user_id = callback.from_user.id
	
	# Получаем смену из БД
	timesheet = await session.get(Timesheet, timesheet_id)
	
	if not timesheet:
		await callback.answer("❌ Смена не найдена", show_alert=True)
		await state.clear()
		return
	
	# Проверяем права доступа
	if timesheet.chat_id != chat_id or timesheet.user_id != user_id:
		await callback.answer("❌ У вас нет прав на удаление этой смены", show_alert=True)
		await state.clear()
		return
	
	shift_names = {
		"day": "дневная",
		"night": "ночная"
	}
	shift_name = shift_names.get(timesheet.shift_type, timesheet.shift_type)
	
	# Создаем кнопки подтверждения
	keyboard = InlineKeyboardBuilder()
	keyboard.button(text="✅ Да, удалить", callback_data=f"removeshift_confirm:{timesheet_id}:yes")
	keyboard.button(text="❌ Отмена", callback_data=f"removeshift_confirm:{timesheet_id}:no")
	keyboard.adjust(2)
	
	await callback.message.edit_text(
		f"🗑️ Подтверждение удаления\n\n"
		f"📅 Дата: {timesheet.date.strftime('%d.%m.%Y')}\n"
		f"⏰ Смена: {shift_name}\n\n"
		f"Удалить эту смену из табеля?",
		reply_markup=keyboard.as_markup()
	)
	
	await callback.answer()


@router.callback_query(F.data.startswith("removeshift_confirm:"))
async def cb_removeshift_confirm(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Окончательное удаление смены из табеля"""
	parts = callback.data.split(":")
	timesheet_id = int(parts[1])
	confirm = parts[2]
	
	if confirm == "no":
		await callback.message.edit_text("❌ Удаление смены отменено.")
		await state.clear()
		await callback.answer()
		return
	
	chat_id = callback.message.chat.id
	user_id = callback.from_user.id
	
	# Получаем смену из БД
	timesheet = await session.get(Timesheet, timesheet_id)
	
	if not timesheet:
		await callback.answer("❌ Смена не найдена", show_alert=True)
		await state.clear()
		return
	
	# Проверяем права доступа
	if timesheet.chat_id != chat_id or timesheet.user_id != user_id:
		await callback.answer("❌ У вас нет прав на удаление этой смены", show_alert=True)
		await state.clear()
		return
	
	shift_names = {
		"day": "дневная",
		"night": "ночная"
	}
	shift_name = shift_names.get(timesheet.shift_type, timesheet.shift_type)
	date_str = timesheet.date.strftime("%d.%m.%Y")
	
	# Удаляем смену
	await session.delete(timesheet)
	await session.commit()
	
	await callback.message.edit_text(
		f"✅ Смена удалена!\n\n"
		f"📅 {date_str} — {shift_name}"
	)
	
	# Удаляем сообщение через 10 секунд
	asyncio.create_task(auto_delete_message(callback.message, 10))
	
	await state.clear()
	await callback.answer("✅ Удалено!") 