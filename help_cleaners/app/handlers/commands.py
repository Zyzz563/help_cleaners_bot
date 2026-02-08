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
from app.db.models import Group, Member, Shift, Photo, PhotoDuplicate, ProcessedCallback, Timesheet, AllowedChat, Manager
# from app.db.session import create_session_maker  # unused after middleware refactor
from app.keyboards import day_night_kb, register_name_kb, gender_kb, start_kb, remove_menu_kb, members_choice_kb
from app.utils.text import is_valid_display_name
from app.utils.time import parse_hhmm, local_today, local_now

router = Router()


async def check_user_allowed(message: Message) -> bool:
	"""
	🔒 Проверяет разрешен ли пользователь для работы с ботом.у
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
	
	# Если это личное сообщение - разрешаем только владельцу
	if message.chat.type == "private":
		if message.from_user and message.from_user.id == OWNER_ID:
			return True
		else:
			await message.reply("Бот работает только в разрешённых группах.\n/help — справка")
			return False
	
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
	waiting_shift_date = State()
	waiting_shift_type = State()
	waiting_confirm = State()
	waiting_remove_date = State()


class ManagerRequestStates(StatesGroup):
	"""Состояния для запроса роли менеджера"""
	waiting_name = State()


# ============ /manager_request — запрос роли менеджера ============

@router.message(Command("manager_request"))
async def cmd_manager_request(message: Message, session: AsyncSession, state: FSMContext):
	"""Запрос на получение роли менеджера."""
	user_id = message.from_user.id if message.from_user else 0
	if user_id == OWNER_ID:
		await message.reply("Вы владелец — у вас уже все права.")
		return

	# Уже менеджер?
	existing = (await session.execute(
		select(Manager).where(Manager.user_id == user_id)
	)).scalar_one_or_none()

	if existing:
		if existing.status == "approved":
			await message.reply("✅ Вы уже менеджер.")
			return
		elif existing.status == "pending":
			await message.reply("⏳ Ваш запрос уже на рассмотрении у владельца.")
			return
		elif existing.status == "rejected":
			# Разрешаем повторную заявку
			await session.delete(existing)
			await session.commit()

	await message.reply("Введите ваше имя (для менеджерского профиля):")
	await state.set_state(ManagerRequestStates.waiting_name)
	asyncio.create_task(auto_delete_message(message, 3))


@router.message(ManagerRequestStates.waiting_name)
async def mgr_request_name(message: Message, session: AsyncSession, state: FSMContext):
	"""Получили имя — сохраняем заявку, уведомляем владельца."""
	name = (message.text or "").strip()
	if not name or len(name) > 64:
		await message.reply("Имя от 1 до 64 символов. Попробуйте ещё раз:")
		return

	user_id = message.from_user.id
	mgr = Manager(
		user_id=user_id,
		display_name=name,
		status="pending",
		requested_at=datetime.utcnow(),
	)
	session.add(mgr)
	await session.commit()
	await state.clear()

	await message.reply("⏳ Запрос отправлен владельцу. Ожидайте подтверждения.")

	# Уведомляем владельца в ЛС
	keyboard = InlineKeyboardBuilder()
	keyboard.button(text="✅ Одобрить", callback_data=f"mgr_approve:{user_id}")
	keyboard.button(text="❌ Отклонить", callback_data=f"mgr_reject:{user_id}")
	keyboard.adjust(2)

	try:
		username = f"@{message.from_user.username}" if message.from_user.username else ""
		await message.bot.send_message(
			OWNER_ID,
			f"📋 <b>Запрос на менеджера</b>\n\n"
			f"👤 {name} {username}\n"
			f"🆔 <code>{user_id}</code>\n\n"
			f"Выдать права менеджера?",
			parse_mode="HTML",
			reply_markup=keyboard.as_markup(),
		)
	except Exception:
		pass  # Владелец мог не начать диалог с ботом


@router.callback_query(F.data.startswith("mgr_approve:"))
async def cb_mgr_approve(callback: CallbackQuery, session: AsyncSession):
	"""Владелец одобряет менеджера."""
	if callback.from_user.id != OWNER_ID:
		await callback.answer("❌ Только владелец", show_alert=True)
		return

	target_id = int(callback.data.split(":", 1)[1])
	mgr = (await session.execute(
		select(Manager).where(Manager.user_id == target_id)
	)).scalar_one_or_none()

	if not mgr:
		await callback.answer("Заявка не найдена", show_alert=True)
		return

	mgr.status = "approved"
	mgr.approved_by = OWNER_ID
	mgr.approved_at = datetime.utcnow()
	await session.commit()

	await callback.message.edit_text(f"✅ {mgr.display_name} — менеджер!")
	await callback.answer("Одобрено!")

	# Уведомляем нового менеджера
	try:
		await callback.bot.send_message(
			target_id,
			"✅ Ваш запрос одобрен! Теперь вы менеджер.\n"
			"Доступные команды: /add_shift, /remove_shift, /manager_list"
		)
	except Exception:
		pass


@router.callback_query(F.data.startswith("mgr_reject:"))
async def cb_mgr_reject(callback: CallbackQuery, session: AsyncSession):
	"""Владелец отклоняет заявку."""
	if callback.from_user.id != OWNER_ID:
		await callback.answer("❌ Только владелец", show_alert=True)
		return

	target_id = int(callback.data.split(":", 1)[1])
	mgr = (await session.execute(
		select(Manager).where(Manager.user_id == target_id)
	)).scalar_one_or_none()

	if not mgr:
		await callback.answer("Заявка не найдена", show_alert=True)
		return

	mgr.status = "rejected"
	await session.commit()

	await callback.message.edit_text(f"❌ {mgr.display_name} — отклонено.")
	await callback.answer("Отклонено")

	try:
		await callback.bot.send_message(target_id, "❌ Ваш запрос на роль менеджера отклонён.")
	except Exception:
		pass


@router.message(Command("manager_list"))
async def cmd_manager_list(message: Message, session: AsyncSession):
	"""Список менеджеров — доступно менеджерам и владельцу."""
	caller_id = message.from_user.id if message.from_user else 0
	if not await is_manager_db(caller_id, session):
		await message.reply("❌ Нет доступа.")
		asyncio.create_task(auto_delete_message(message, 3))
		return

	managers = (await session.execute(
		select(Manager).where(Manager.status == "approved").order_by(Manager.display_name)
	)).scalars().all()

	if not managers:
		text = "Менеджеров нет (кроме владельца)."
	else:
		lines = [f"👤 {m.display_name} (ID: {m.user_id})" for m in managers]
		text = "<b>Менеджеры:</b>\n" + "\n".join(lines)

	# Добавляем владельца
	text = f"👑 Владелец: {OWNER_ID}\n\n" + text

	await message.reply(text, parse_mode="HTML")
	asyncio.create_task(auto_delete_message(message, 3))


@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext):
	"""
	Команда /start - приветствие и информация о боте.
	"""
	# Если это личное сообщение
	if message.chat.type == "private":
		await message.reply(
			"👋 Привет! Я бот для клинеров.\n\n"
			"Добавьте меня в группу и выполните /addchat.\n"
			"Подробнее: /help",
			parse_mode="HTML"
		)
		return
	
	# Если это группа - проверяем разрешения
	if not await check_chat_allowed(message, session):
		return
	
	await message.reply(
		"Я бот для клинеров. Для начала — зарегистрируйтесь:", reply_markup=start_kb(message.from_user.username if message.from_user else None))


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
	caller_id = message.from_user.id
	caller_is_admin = await is_admin(chat_id, caller_id, message.bot) or caller_id == OWNER_ID

	args = (message.text or "").split(maxsplit=1)
	target_user_id: Optional[int] = None
	admin_mode = False

	# ──────────────────────────────────────────────────
	# 1. REPLY-TO-MESSAGE: ответ на сообщение пользователя
	#    Самый надёжный способ зарегистрировать другого
	# ──────────────────────────────────────────────────
	if message.reply_to_message and message.reply_to_message.from_user:
		replied_user = message.reply_to_message.from_user
		if replied_user.is_bot:
			await message.reply("❌ Нельзя зарегистрировать бота.")
			asyncio.create_task(auto_delete_message(message, 3))
			return
		if replied_user.id != caller_id:
			if not caller_is_admin:
				await message.reply("❌ Регистрировать других может только админ.")
				asyncio.create_task(auto_delete_message(message, 3))
				return
			target_user_id = replied_user.id
			admin_mode = True

	# ──────────────────────────────────────────────────
	# 2. АРГУМЕНТ: /register <число_ID>
	# ──────────────────────────────────────────────────
	if target_user_id is None and len(args) >= 2:
		arg = args[1].strip()

		if not caller_is_admin:
			await message.reply("❌ Регистрировать других может только админ.")
			asyncio.create_task(auto_delete_message(message, 3))
			return

		# Пробуем числовой ID
		try:
			parsed_id = int(arg)
		except ValueError:
			parsed_id = None

		if parsed_id is not None:
			# Проверяем — это свой ID?
			if parsed_id == caller_id:
				await message.reply(
					f"⚠️ ID {parsed_id} — это ваш собственный аккаунт!\n\n"
					f"Ваш Telegram ID: {caller_id}\n\n"
					f"💡 Чтобы зарегистрировать другого участника:\n"
					f"→ Ответьте на любое сообщение этого участника командой /register"
				)
				asyncio.create_task(auto_delete_message(message, 3))
				return
			target_user_id = parsed_id
			admin_mode = True
		else:
			# Не число — @username или что-то другое
			await message.reply(
				f"❌ «{arg}» — не числовой ID.\n\n"
				f"💡 Самый простой способ зарегистрировать другого участника:\n"
				f"→ Ответьте на любое его сообщение командой /register\n\n"
				f"Или узнайте его числовой ID через @userinfobot"
			)
			asyncio.create_task(auto_delete_message(message, 3))
			return

	# ──────────────────────────────────────────────────
	# 3. ADMIN MODE: валидация и запуск регистрации
	# ──────────────────────────────────────────────────
	if admin_mode and target_user_id is not None:
		# Проверяем что пользователь в группе
		try:
			chat_member = await message.bot.get_chat_member(chat_id, target_user_id)
			if chat_member.status in ['left', 'kicked']:
				await message.reply("❌ Этот пользователь не находится в группе.")
				asyncio.create_task(auto_delete_message(message, 3))
				return
			# Имя для отображения
			if chat_member.user.username:
				target_name = f"@{chat_member.user.username}"
			elif chat_member.user.first_name:
				target_name = chat_member.user.first_name
			else:
				target_name = f"ID:{target_user_id}"
		except Exception:
			await message.reply("❌ Не удалось найти пользователя. Проверьте ID.")
			asyncio.create_task(auto_delete_message(message, 3))
			return

		# Уже зарегистрирован?
		existing_target = await session.get(Member, {"chat_id": chat_id, "user_id": target_user_id})
		if existing_target:
			await message.reply(f"ℹ️ {target_name} уже зарегистрирован как «{existing_target.display_name}».")
			asyncio.create_task(auto_delete_message(message, 3))
			return

		# Начинаем регистрацию
		await state.clear()  # Сброс любого предыдущего состояния
		await state.update_data(target_user_id=target_user_id)
		reply_msg = await message.reply(
			f"👤 Регистрирую: {target_name} (ID: {target_user_id})\n"
			f"Введите имя для этого пользователя (1..{MAX_NAME_LEN} символов):"
		)
		await state.set_state(RegStates.waiting_name)
		await state.update_data(messages_to_delete=[message, reply_msg])
		return

	# ──────────────────────────────────────────────────
	# 4. /register без аргументов
	# ──────────────────────────────────────────────────

	# Если уже в процессе
	st = await state.get_state()
	if st == RegStates.waiting_gender.state:
		await message.reply("Вы уже в процессе: выберите пол выше или /reset")
		return
	if st == RegStates.waiting_name.state:
		await message.reply(f"Регистрация идёт: введите имя (1..{MAX_NAME_LEN}) или /reset")
		return

	# Уже зарегистрирован?
	existing_self = await session.get(Member, {"chat_id": chat_id, "user_id": caller_id})
	if existing_self:
		if caller_is_admin:
			# Админ уже зарегистрирован — показываем подсказку
			await message.reply(
				f"✅ Вы зарегистрированы как «{existing_self.display_name}».\n\n"
				f"👑 Чтобы зарегистрировать другого участника:\n"
				f"→ Ответьте на его сообщение командой /register"
			)
		else:
			await message.reply(f"✅ Вы уже зарегистрированы как «{existing_self.display_name}».")
		asyncio.create_task(auto_delete_message(message, 3))
		return

	# Саморегистрация
	await state.clear()
	if message.from_user.username:
		kb = register_name_kb(message.from_user.username)
		reply = await message.reply(f"Отправьте имя/ник (1..{MAX_NAME_LEN} символов) или нажмите кнопку", reply_markup=kb)
	else:
		reply = await message.reply(f"Отправьте имя/ник (1..{MAX_NAME_LEN} символов)")
	await state.set_state(RegStates.waiting_name)
	await state.update_data(target_user_id=caller_id, messages_to_delete=[message, reply])


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
		return

	chat_id = message.chat.id
	user_id = message.from_user.id if message.from_user else 0
	group = await ensure_group(session, chat_id, message.chat.title)
	today = local_today(group.tz)

	# Проверяем есть ли уже смена сегодня
	existing_shift = await session.execute(
		select(Shift).where(Shift.chat_id == chat_id, Shift.user_id == user_id, Shift.date == today)
	)
	current_shift = existing_shift.scalar_one_or_none()

	if current_shift:
		shift_names = {"day": "🌅 дневная", "night": "🌙 ночная"}
		name = shift_names.get(current_shift.type, current_shift.type)
		await message.reply(f"У вас уже есть {name} смена на {today.strftime('%d.%m')}.")
		asyncio.create_task(auto_delete_message(message, 3))
		return

	await message.reply("Выберите смену:", reply_markup=day_night_kb())
	asyncio.create_task(auto_delete_message(message, 3))


@router.callback_query(F.data.startswith("shift:"))
async def cb_shift(callback: CallbackQuery, session: AsyncSession):
	shift_type = callback.data.split(":", 1)[1]
	if shift_type not in ("day", "night"):
		await callback.answer("❌ Неизвестный тип смены", show_alert=True)
		return

	chat_id = callback.message.chat.id
	user_id = callback.from_user.id

	# Idempotency
	cb_id = f"{callback.id}"
	existing = await session.get(ProcessedCallback, cb_id)
	if existing:
		await callback.answer()
		return

	group = await ensure_group(session, chat_id, callback.message.chat.title)
	shift_date = local_today(group.tz)

	# Уже есть смена?
	existing_shift = await session.execute(
		select(Shift).where(Shift.chat_id == chat_id, Shift.user_id == user_id, Shift.date == shift_date)
	)
	if existing_shift.scalar_one_or_none():
		await callback.answer("⚠️ У вас уже есть смена на сегодня!", show_alert=True)
		return

	# Записываем смену — ВСЕГДА на сегодняшнюю дату
	# Дневная: 09:00-21:00 того же дня
	# Ночная:  21:00-09:00 (начало в этот день, конец на следующий)
	# В табеле и БД — дата НАЧАЛА смены
	await _upsert_shift(session, chat_id, user_id, shift_date, shift_type)

	if shift_type == "day":
		await callback.message.edit_text(
			f"✅ Дневная смена: {shift_date.strftime('%d.%m')} (09:00–21:00)"
		)
	else:
		await callback.message.edit_text(
			f"✅ Ночная смена: {shift_date.strftime('%d.%m')} (21:00–09:00)"
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
		return

	chat_id = message.chat.id
	group = await ensure_group(session, chat_id, message.chat.title)
	today = local_today(group.tz)

	# Все смены на сегодня (и день, и ночь записаны на одну дату)
	q = (
		select(Shift.type, Member.display_name, Member.user_id)
		.join(Member, (Member.chat_id == Shift.chat_id) & (Member.user_id == Shift.user_id))
		.where(Shift.chat_id == chat_id, Shift.date == today)
		.order_by(Shift.type, Member.display_name)
	)
	rows = (await session.execute(q)).all()

	if not rows:
		reply = await message.reply("Сегодня смен нет.")
		asyncio.create_task(auto_delete_message(message, 3))
		asyncio.create_task(auto_delete_message(reply, 15))
		return

	def mention(uid: int, name: str) -> str:
		return f"<a href=\"tg://user?id={uid}\">{name}</a>"

	day_names = []
	night_names = []
	for shift_type, name, uid in rows:
		m = mention(uid, name)
		if shift_type == "day":
			day_names.append(m)
		elif shift_type == "night":
			night_names.append(m)

	parts = []
	if day_names:
		parts.append("🌅 День: " + ", ".join(day_names))
	if night_names:
		parts.append("🌙 Ночь: " + ", ".join(night_names))

	await message.reply("\n".join(parts), parse_mode="HTML")
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


## Second /start handler removed — merged into the first cmd_start above


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
	caller_id = message.from_user.id if message.from_user else 0
	caller_is_manager = await is_manager_db(caller_id, session)
	caller_is_owner = caller_id == OWNER_ID

	# Базовые команды — для всех
	text = (
		"🤖 <b>CleaningBot for the Spirit</b>\n\n"
		"<b>Клинер:</b>\n"
		"/register — регистрация\n"
		"/shift — выбрать смену\n"
		"/today — кто на смене\n"
		"/myshifts — мои смены\n"
		"/tabel — мой табель\n"
		"/help — справка\n"
	)

	# Менеджерские команды
	if caller_is_manager:
		text += (
			"\n<b>Менеджер:</b>\n"
			"/add_shift — добавить смену в табель\n"
			"/remove_shift — удалить смену\n"
			"/cleaners — список клинеров\n"
			"/duplicates — дубликаты фото\n"
			"/manager_list — список менеджеров\n"
		)

	# Команды владельца
	if caller_is_owner:
		text += (
			"\n<b>Владелец:</b>\n"
			"/addchat — разрешить группу\n"
			"/removechat — убрать группу\n"
			"/listchats — список групп\n"
		)

	# Для не-менеджеров — как стать менеджером
	if not caller_is_manager:
		text += "\n/manager_request — запрос роли менеджера\n"

	text += (
		"\n<b>Смены:</b>\n"
		"🌅 Дневная: 09:00–21:00\n"
		"🌙 Ночная: 21:00–09:00\n\n"
		"<i>Разработчик | Aziz [Devzis]</i>"
	)

	if message.chat.type == "private":
		await message.reply(text, parse_mode="HTML")
		return

	if not await check_chat_allowed(message, session):
		return

	await message.reply(text, parse_mode="HTML")
	asyncio.create_task(auto_delete_message(message, 3))


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
			
			reply = await message.reply(f"✅ Группа {chat_title} снова активирована!")
			# Удаляем сообщение бота через 30 секунд
			asyncio.create_task(auto_delete_message(reply, 30))
			# Удаляем команду пользователя через 3 секунды
			asyncio.create_task(auto_delete_message(message, 3))
		else:
			# Группа уже активна
			reply = await message.reply(f"✅ Группа {chat_title} уже в списке разрешенных.")
			# Удаляем сообщение бота через 30 секунд
			asyncio.create_task(auto_delete_message(reply, 30))
			# Удаляем команду пользователя через 3 секунды
			asyncio.create_task(auto_delete_message(message, 3))
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
	
	reply = await message.reply(f"✅ Группа {chat_title} добавлена в список разрешенных!")
	# Удаляем сообщение бота через 30 секунд
	asyncio.create_task(auto_delete_message(reply, 30))
	# Удаляем команду пользователя через 3 секунды
	asyncio.create_task(auto_delete_message(message, 3))


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
	
	from app.config import ALLOWED_CHATS
	# Используем бота из контекста сообщения
	bot = message.bot
	
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
	
	# Создаём клавиатуру для кнопок удаления
	from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
	from aiogram.utils.keyboard import InlineKeyboardBuilder
	keyboard = InlineKeyboardBuilder()
	
	if all_active:
		text += "🟢 АКТИВНЫЕ ГРУППЫ:\n\n"
		for i, chat in enumerate(all_active):
			text += f"{chat['status']} {chat['chat_title']}\n"
			text += f"   ID: {chat['chat_id']}\n"
			if chat.get('added_at'):
				text += f"   Добавлена: {chat['added_at'].strftime('%d.%m.%Y %H:%M')}\n"
			text += f"   └─ Нажмите кнопку ниже для удаления\n\n"
			
			# Добавляем кнопку удаления для этой группы
			keyboard.button(
				text=f"🗑️ Удалить: {chat['chat_title'][:20]}...",
				callback_data=f"remove_chat:{chat['chat_id']}"
			)
	
	if inactive_chats:
		text += "\n🔴 НЕАКТИВНЫЕ (бот не состоит в группе):\n\n"
		for chat in inactive_chats:
			text += f"{chat['status']} {chat['chat_title']}\n"
			text += f"   ID: {chat['chat_id']}\n"
			if chat.get('added_at'):
				text += f"   Была добавлена: {chat['added_at'].strftime('%d.%m.%Y %H:%M')}\n"
			text += "\n"
		text += "💡 Неактивные группы были автоматически удалены из списка разрешённых.\n"
	
	# Настраиваем клавиатуру - по одной кнопке в ряд
	keyboard.adjust(1)
	
	if all_active or inactive_chats:
		reply = await message.reply(text, reply_markup=keyboard.as_markup())
		# Удаляем сообщение бота через 30 секунд
		asyncio.create_task(auto_delete_message(reply, 30))
	else:
		reply = await message.reply(text)
		# Удаляем сообщение бота через 30 секунд
		asyncio.create_task(auto_delete_message(reply, 30))
	
	# Удаляем команду пользователя через 3 секунды
	asyncio.create_task(auto_delete_message(message, 3))


@router.callback_query(F.data.startswith("remove_chat:"))
async def cb_remove_chat(callback: CallbackQuery, session: AsyncSession):
	"""
	🔒 Callback для удаления группы из списка разрешённых.
	"""
	# Проверяем права владельца
	if callback.from_user.id != OWNER_ID:
		await callback.answer("❌ У вас нет прав для этого действия.", show_alert=True)
		return
	
	# Извлекаем chat_id из callback_data
	chat_id = int(callback.data.split(":")[1])
	
	from app.config import ALLOWED_CHATS
	
	# Убираем группу из БД
	result = await session.execute(
		update(AllowedChat)
		.where(AllowedChat.chat_id == chat_id)
		.values(is_active=False)
	)
	await session.commit()
	
	# Убираем группу из конфига ALLOWED_CHATS
	if chat_id in ALLOWED_CHATS:
		ALLOWED_CHATS.remove(chat_id)
	
	if result.rowcount > 0:
		await callback.answer("✅ Группа удалена из списка разрешённых!", show_alert=True)
		# Обновляем сообщение, убирая кнопку удалённой группы
		updated_message = await callback.message.edit_text(
			f"{callback.message.text}\n\n🗑️ Группа (ID: {chat_id}) удалена из списка."
		)
		# Удаляем обновлённое сообщение через 30 секунд
		asyncio.create_task(auto_delete_message(updated_message, 30))
		
		# Пытаемся выйти из группы
		try:
			await callback.bot.leave_chat(chat_id)
		except Exception:
			pass  # Группа может быть уже недоступна
	else:
		await callback.answer("❌ Группа не найдена в списке.", show_alert=True)


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
		asyncio.create_task(auto_delete_message(message, 3))
		asyncio.create_task(auto_delete_message(reply, 10))
		return

	shift_names = {"day": "🌅 день", "night": "🌙 ночь"}
	text = "Ваши смены (посл. 30):\n" + "\n".join(
		f"{d.strftime('%d.%m')} — {shift_names.get(t, t)}" for d, t in rows
	)
	await message.reply(text)
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
	await message.reply(text, parse_mode="HTML")
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
	caller_id = message.from_user.id if message.from_user else 0
	group = await ensure_group(session, chat_id, message.chat.title)
	today = local_today(group.tz)

	# ──────────────────────────────────────────────────
	# Определяем целевого пользователя (если указан)
	# ──────────────────────────────────────────────────
	target_user_id: Optional[int] = None
	target_name: Optional[str] = None

	# 1. Reply-to-message
	if message.reply_to_message and message.reply_to_message.from_user:
		if await is_admin(chat_id, caller_id, message.bot) or caller_id == OWNER_ID:
			replied = message.reply_to_message.from_user
			if not replied.is_bot:
				target_user_id = replied.id
				target_name = replied.username or replied.first_name or f"ID:{replied.id}"

	# 2. Аргумент: /duplicates <user_id>
	args = (message.text or "").split(maxsplit=1)
	if target_user_id is None and len(args) >= 2:
		if await is_admin(chat_id, caller_id, message.bot) or caller_id == OWNER_ID:
			arg = args[1].strip()
			try:
				target_user_id = int(arg)
				try:
					cm = await message.bot.get_chat_member(chat_id, target_user_id)
					target_name = cm.user.username or cm.user.first_name or f"ID:{target_user_id}"
				except Exception:
					target_name = f"ID:{target_user_id}"
			except ValueError:
				# Может быть @username — пробуем найти в нашей БД
				clean = arg.lstrip("@").lower()
				# Ищем среди зарегистрированных участников
				members_result = await session.execute(
					select(Member).where(Member.chat_id == chat_id)
				)
				for m in members_result.scalars().all():
					try:
						cm = await message.bot.get_chat_member(chat_id, m.user_id)
						if cm.user.username and cm.user.username.lower() == clean:
							target_user_id = m.user_id
							target_name = f"@{cm.user.username}"
							break
					except Exception:
						continue
				if target_user_id is None:
					await message.reply(
						"❌ Не удалось найти пользователя.\n"
						"Используйте: /duplicates <числовой_ID>\n"
						"Или ответьте на сообщение пользователя командой /duplicates"
					)
					asyncio.create_task(auto_delete_message(message, 3))
					return

	# ──────────────────────────────────────────────────
	# РЕЖИМ: Анализ конкретного пользователя
	# ──────────────────────────────────────────────────
	if target_user_id is not None:
		await _duplicates_for_user(message, session, chat_id, target_user_id, target_name or f"ID:{target_user_id}", group)
		return

	# ──────────────────────────────────────────────────
	# РЕЖИМ: Общий список дубликатов группы (30 дней)
	# ──────────────────────────────────────────────────
	thirty_days_ago = today - timedelta(days=30)
	
	rows = await session.execute(
		select(PhotoDuplicate, Photo)
		.join(Photo, PhotoDuplicate.duplicate_photo_id == Photo.id)
		.where(PhotoDuplicate.chat_id == chat_id, PhotoDuplicate.duplicate_date >= thirty_days_ago)
		.order_by(PhotoDuplicate.created_at.desc())
		.limit(50)
	)
	rows = rows.all()

	if not rows:
		reply = await message.reply("✅ Дубликатов не найдено за последние 30 дней.")
		asyncio.create_task(auto_delete_message(message, 3))
		return
	
	lines = []
	lines.append("📊 <b>Дубликаты за 30 дней</b>\n")
	
	for i, (dup, dup_photo) in enumerate(rows, 1):
		dup_user = await session.get(Member, {"chat_id": chat_id, "user_id": dup_photo.user_id})
		
		orig_photo_result = await session.execute(
			select(Photo).where(Photo.id == dup.original_photo_id)
		)
		orig_photo = orig_photo_result.scalar_one_or_none()
		
		if orig_photo:
			orig_user = await session.get(Member, {"chat_id": chat_id, "user_id": orig_photo.user_id})
			orig_name_str = orig_user.display_name if orig_user else f"ID:{orig_photo.user_id}"
		else:
			orig_name_str = "Неизвестно"
		
		dup_name_str = dup_user.display_name if dup_user else f"ID:{dup_photo.user_id}"
		
		orig_link = f"<a href=\"tg://user?id={orig_photo.user_id}\">{orig_name_str}</a>" if orig_photo else orig_name_str
		dup_link = f"<a href=\"tg://user?id={dup_photo.user_id}\">{dup_name_str}</a>"
		
		lines.append(f"<b>{i}.</b> Оригинал: {orig_link}")
		lines.append(f"   📅 {dup.original_date.strftime('%d.%m.%Y')} ⏰ {str(dup.original_time)[:5]}")
		lines.append(f"   Дубликат: {dup_link}")
		lines.append(f"   📅 {dup.duplicate_date.strftime('%d.%m.%Y')} ⏰ {str(dup.duplicate_time)[:5]}")
		lines.append("")
	
	response_text = "\n".join(lines)
	if len(response_text) > 4096:
		parts = [response_text[i:i+4096] for i in range(0, len(response_text), 4096)]
		for i, part in enumerate(parts):
			await message.reply(f"Часть {i+1}/{len(parts)}:\n{part}", parse_mode="HTML")
	else:
		await message.reply(response_text, parse_mode="HTML")
	
	asyncio.create_task(auto_delete_message(message, 3))


async def _duplicates_for_user(
	message: Message, session: AsyncSession,
	chat_id: int, user_id: int, user_display: str, group
):
	"""
	Полный анализ дубликатов конкретного пользователя.
	Проверяет ВСЕ фото пользователя за всё время.
	"""
	processing = await message.reply(f"⏳ Запускаю полный анализ дубликатов для {user_display}...")

	# 1. Все фото этого пользователя в группе
	user_photos_result = await session.execute(
		select(Photo)
		.where(Photo.chat_id == chat_id, Photo.user_id == user_id)
		.order_by(Photo.date.asc(), Photo.time.asc())
	)
	user_photos = user_photos_result.scalars().all()

	if not user_photos:
		await processing.edit_text(f"📊 Анализ для {user_display}: фотографий не найдено.")
		asyncio.create_task(auto_delete_message(message, 3))
		return

	# 2. Получаем ВСЕ уникальные file_unique_id этого пользователя
	user_unique_ids = {p.file_unique_id for p in user_photos}

	# 3. Ищем ВСЕ фото в группе с такими же file_unique_id (включая чужие)
	all_matching_result = await session.execute(
		select(Photo)
		.where(Photo.chat_id == chat_id, Photo.file_unique_id.in_(user_unique_ids))
		.order_by(Photo.date.asc(), Photo.time.asc())
	)
	all_matching = all_matching_result.scalars().all()

	# 4. Группируем по file_unique_id
	groups_by_uid: dict[str, list] = {}
	for photo in all_matching:
		fuid = photo.file_unique_id
		if fuid not in groups_by_uid:
			groups_by_uid[fuid] = []
		groups_by_uid[fuid].append(photo)

	# 5. Находим дубликаты — группы с >1 фото
	duplicates_found = []
	for fuid, photos in groups_by_uid.items():
		if len(photos) > 1:
			# Сортируем: первое = оригинал, остальные = дубликаты
			photos.sort(key=lambda p: (p.date, p.time))
			original = photos[0]
			for dup in photos[1:]:
				# Нас интересуют только случаи, где ЭТОТ пользователь замешан
				if dup.user_id == user_id or original.user_id == user_id:
					duplicates_found.append((original, dup))

	# 6. Также проверяем записи в таблице photo_duplicates
	db_dups_result = await session.execute(
		select(PhotoDuplicate)
		.where(
			PhotoDuplicate.chat_id == chat_id,
			(PhotoDuplicate.duplicate_photo_id.in_(
				select(Photo.id).where(Photo.chat_id == chat_id, Photo.user_id == user_id)
			)) | (PhotoDuplicate.original_photo_id.in_(
				select(Photo.id).where(Photo.chat_id == chat_id, Photo.user_id == user_id)
			))
		)
		.order_by(PhotoDuplicate.created_at.desc())
	)
	db_dups = db_dups_result.scalars().all()

	# 7. Формируем отчёт
	total_photos = len(user_photos)
	total_unique = len(user_unique_ids)
	total_duplicates = len(duplicates_found)

	lines = []
	lines.append(f"📊 <b>АНАЛИЗ ДУБЛИКАТОВ</b>")
	lines.append(f"👤 Пользователь: <b>{user_display}</b>")
	lines.append(f"")
	lines.append(f"📈 <b>Статистика:</b>")
	lines.append(f"• Всего фото отправлено: {total_photos}")
	lines.append(f"• Уникальных фото: {total_unique}")
	lines.append(f"• Дубликатов найдено: {total_duplicates}")
	if total_photos > 0:
		dup_percent = round(total_duplicates / total_photos * 100, 1)
		lines.append(f"• Процент дубликатов: {dup_percent}%")
	lines.append("")

	if not duplicates_found:
		lines.append("✅ <b>Дубликатов не обнаружено!</b>")
	else:
		lines.append("─────────────────────")
		lines.append("")

		# Группируем по датам для наглядности
		by_date: dict[str, list] = {}
		for orig, dup in duplicates_found:
			date_key = dup.date.strftime('%d.%m.%Y')
			if date_key not in by_date:
				by_date[date_key] = []
			by_date[date_key].append((orig, dup))

		for date_str in sorted(by_date.keys(), reverse=True):
			items = by_date[date_str]
			lines.append(f"📅 <b>{date_str}</b> — {len(items)} дубликат(ов)")
			for idx, (orig, dup) in enumerate(items, 1):
				# Информация об оригинале
				orig_member = await session.get(Member, {"chat_id": chat_id, "user_id": orig.user_id})
				orig_name = orig_member.display_name if orig_member else f"ID:{orig.user_id}"
				
				dup_member = await session.get(Member, {"chat_id": chat_id, "user_id": dup.user_id})
				dup_name = dup_member.display_name if dup_member else f"ID:{dup.user_id}"

				# Время между оригиналом и дубликатом
				orig_dt = datetime.combine(orig.date, orig.time.replace(tzinfo=None) if hasattr(orig.time, 'replace') else orig.time)
				dup_dt = datetime.combine(dup.date, dup.time.replace(tzinfo=None) if hasattr(dup.time, 'replace') else dup.time)
				diff = abs(dup_dt - orig_dt)
				if diff.days > 0:
					diff_str = f"{diff.days} дн."
				elif diff.seconds >= 3600:
					diff_str = f"{diff.seconds // 3600} ч."
				elif diff.seconds >= 60:
					diff_str = f"{diff.seconds // 60} мин."
				else:
					diff_str = f"{diff.seconds} сек."

				is_forwarded = "📤 Пересланное" if dup.is_forwarded else ""
				same_user = orig.user_id == dup.user_id

				if same_user:
					lines.append(
						f"  {idx}. Свой дубликат {is_forwarded}"
					)
					lines.append(
						f"     Оригинал: {orig.date.strftime('%d.%m')} {str(orig.time)[:5]}"
					)
					lines.append(
						f"     Повтор:   {dup.date.strftime('%d.%m')} {str(dup.time)[:5]} (через {diff_str})"
					)
				else:
					orig_link = f"<a href=\"tg://user?id={orig.user_id}\">{orig_name}</a>"
					dup_link = f"<a href=\"tg://user?id={dup.user_id}\">{dup_name}</a>"
					lines.append(
						f"  {idx}. Чужое фото скопировано {is_forwarded}"
					)
					lines.append(
						f"     Оригинал ({orig_link}): {orig.date.strftime('%d.%m')} {str(orig.time)[:5]}"
					)
					lines.append(
						f"     Копия ({dup_link}): {dup.date.strftime('%d.%m')} {str(dup.time)[:5]} (через {diff_str})"
					)
			lines.append("")

	response_text = "\n".join(lines)

	# Отправляем результат
	try:
		await processing.delete()
	except Exception:
		pass

	if len(response_text) > 4096:
		parts = [response_text[i:i+4096] for i in range(0, len(response_text), 4096)]
		for i, part in enumerate(parts):
			await message.reply(f"Часть {i+1}/{len(parts)}:\n{part}", parse_mode="HTML")
	else:
		await message.reply(response_text, parse_mode="HTML")

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




# ==================== УПРАВЛЕНИЕ ТАБЕЛЕМ (ТОЛЬКО МЕНЕДЖЕРЫ) ====================


async def is_manager_db(user_id: int, session: AsyncSession) -> bool:
	"""Проверяет, является ли пользователь менеджером (из БД) или владельцем."""
	if user_id == OWNER_ID:
		return True
	mgr = (await session.execute(
		select(Manager).where(Manager.user_id == user_id, Manager.status == "approved")
	)).scalar_one_or_none()
	return mgr is not None


@router.message(Command("add_shift"))
async def cmd_add_shift(message: Message, session: AsyncSession, state: FSMContext):
	"""
	Добавление смены в табель — только для менеджеров.
	Менеджер выбирает сотрудника → дату → тип смены.
	"""
	if not await check_chat_allowed(message, session):
		return
	if message.chat.type not in {"group", "supergroup"}:
		return

	caller_id = message.from_user.id if message.from_user else 0
	if not await is_manager_db(caller_id, session):
		reply = await message.reply("❌ Только менеджеры могут заполнять табель.")
		asyncio.create_task(auto_delete_message(message, 3))
		asyncio.create_task(auto_delete_message(reply, 10))
		return

	chat_id = message.chat.id

	# Показываем список зарегистрированных сотрудников
	members = (await session.execute(
		select(Member).where(Member.chat_id == chat_id).order_by(Member.display_name)
	)).scalars().all()

	if not members:
		reply = await message.reply("❌ Нет зарегистрированных клинеров.")
		asyncio.create_task(auto_delete_message(message, 3))
		asyncio.create_task(auto_delete_message(reply, 10))
		return

	keyboard = InlineKeyboardBuilder()
	for m in members:
		keyboard.button(text=m.display_name, callback_data=f"addshift_user:{m.user_id}")
	keyboard.adjust(2)

	await message.reply("📋 Выберите сотрудника:", reply_markup=keyboard.as_markup())
	await state.set_state(TimesheetStates.waiting_shift_date)
	asyncio.create_task(auto_delete_message(message, 3))


@router.callback_query(F.data.startswith("addshift_user:"))
async def cb_addshift_user(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Менеджер выбрал сотрудника → показываем календарь дат."""
	if not await is_manager_db(callback.from_user.id, session):
		await callback.answer("❌ Нет доступа", show_alert=True)
		return

	target_user_id = int(callback.data.split(":", 1)[1])
	chat_id = callback.message.chat.id

	# Запоминаем ID сотрудника
	await state.update_data(target_user_id=target_user_id)

	# Имя сотрудника
	member = await session.get(Member, {"chat_id": chat_id, "user_id": target_user_id})
	member_name = member.display_name if member else str(target_user_id)

	group = await ensure_group(session, chat_id, callback.message.chat.title)
	today = local_today(group.tz)

	# Кнопки для дней текущего месяца
	first_day = today.replace(day=1)
	if today.month == 12:
		last_day = today.replace(day=31)
	else:
		next_month = today.replace(month=today.month + 1, day=1)
		last_day = next_month - timedelta(days=1)

	keyboard = InlineKeyboardBuilder()
	current_date = first_day
	while current_date <= last_day:
		label = "📅 Сегодня" if current_date == today else current_date.strftime("%d.%m")
		keyboard.button(text=label, callback_data=f"addshift_date:{current_date.isoformat()}")
		current_date += timedelta(days=1)
	keyboard.adjust(4)

	await callback.message.edit_text(
		f"📋 Табель: {member_name}\nВыберите дату:",
		reply_markup=keyboard.as_markup()
	)
	await callback.answer()


@router.callback_query(F.data.startswith("addshift_date:"))
async def cb_addshift_date(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Менеджер выбрал дату → выбор типа смены."""
	if not await is_manager_db(callback.from_user.id, session):
		await callback.answer("❌ Нет доступа", show_alert=True)
		return

	shift_date = date.fromisoformat(callback.data.split(":", 1)[1])
	chat_id = callback.message.chat.id
	data = await state.get_data()
	target_user_id = data.get("target_user_id")

	if not target_user_id:
		await callback.answer("❌ Сотрудник не выбран", show_alert=True)
		await state.clear()
		return

	await state.update_data(shift_date=shift_date)

	# Проверяем, есть ли уже запись в табеле
	existing = (await session.execute(
		select(Timesheet).where(
			Timesheet.chat_id == chat_id,
			Timesheet.user_id == target_user_id,
			Timesheet.date == shift_date
		)
	)).scalars().all()

	if existing:
		names = [("🌅 день" if s.shift_type == "day" else "🌙 ночь") for s in existing]
		await callback.answer(f"⚠️ Уже есть: {', '.join(names)} на {shift_date.strftime('%d.%m')}", show_alert=True)
		return

	keyboard = InlineKeyboardBuilder()
	keyboard.button(text="🌅 Дневная", callback_data="addshift_type:day")
	keyboard.button(text="🌙 Ночная", callback_data="addshift_type:night")
	keyboard.adjust(2)

	await callback.message.edit_text(
		f"📅 {shift_date.strftime('%d.%m.%Y')}\nВыберите тип смены:",
		reply_markup=keyboard.as_markup()
	)
	await state.set_state(TimesheetStates.waiting_shift_type)
	await callback.answer()


@router.callback_query(F.data.startswith("addshift_type:"))
async def cb_addshift_type(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Менеджер выбрал тип → подтверждение."""
	if not await is_manager_db(callback.from_user.id, session):
		await callback.answer("❌ Нет доступа", show_alert=True)
		return

	shift_type = callback.data.split(":", 1)[1]
	data = await state.get_data()
	shift_date = data.get("shift_date")
	target_user_id = data.get("target_user_id")

	if not shift_date or not target_user_id:
		await callback.answer("❌ Данные потеряны", show_alert=True)
		await state.clear()
		return

	await state.update_data(shift_type=shift_type)

	member = await session.get(Member, {"chat_id": callback.message.chat.id, "user_id": target_user_id})
	member_name = member.display_name if member else str(target_user_id)
	shift_label = "🌅 дневная" if shift_type == "day" else "🌙 ночная"

	keyboard = InlineKeyboardBuilder()
	keyboard.button(text="✅ Добавить", callback_data="addshift_confirm:yes")
	keyboard.button(text="❌ Отмена", callback_data="addshift_confirm:no")
	keyboard.adjust(2)

	await callback.message.edit_text(
		f"📋 Подтверждение\n\n"
		f"👤 {member_name}\n"
		f"📅 {shift_date.strftime('%d.%m.%Y')}\n"
		f"⏰ {shift_label}\n\n"
		f"Добавить в табель?",
		reply_markup=keyboard.as_markup()
	)
	await state.set_state(TimesheetStates.waiting_confirm)
	await callback.answer()


@router.callback_query(F.data.startswith("addshift_confirm:"))
async def cb_addshift_confirm(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Подтверждение добавления смены в табель."""
	confirm = callback.data.split(":", 1)[1]

	if confirm == "no":
		await callback.message.edit_text("❌ Отменено.")
		await state.clear()
		await callback.answer()
		return

	data = await state.get_data()
	shift_date = data.get("shift_date")
	shift_type = data.get("shift_type")
	target_user_id = data.get("target_user_id")
	chat_id = callback.message.chat.id

	if not shift_date or not shift_type or not target_user_id:
		await callback.answer("❌ Данные потеряны", show_alert=True)
		await state.clear()
		return

	new_ts = Timesheet(
		chat_id=chat_id,
		user_id=target_user_id,
		date=shift_date,
		shift_type=shift_type,
		photo_count=0,
		confirmed_at=datetime.utcnow()
	)
	session.add(new_ts)
	await session.commit()

	member = await session.get(Member, {"chat_id": chat_id, "user_id": target_user_id})
	member_name = member.display_name if member else str(target_user_id)
	shift_label = "🌅 день" if shift_type == "day" else "🌙 ночь"

	await callback.message.edit_text(f"✅ {member_name} — {shift_date.strftime('%d.%m')} {shift_label}")
	await state.clear()
	await callback.answer("✅ Добавлено!")


@router.message(Command("remove_shift"))
async def cmd_remove_shift(message: Message, session: AsyncSession, state: FSMContext):
	"""Удаление смены из табеля — только для менеджеров."""
	if not await check_chat_allowed(message, session):
		return
	if message.chat.type not in {"group", "supergroup"}:
		return

	caller_id = message.from_user.id if message.from_user else 0
	if not await is_manager_db(caller_id, session):
		reply = await message.reply("❌ Только менеджеры могут удалять смены из табеля.")
		asyncio.create_task(auto_delete_message(message, 3))
		asyncio.create_task(auto_delete_message(reply, 10))
		return

	chat_id = message.chat.id

	# Список сотрудников
	members = (await session.execute(
		select(Member).where(Member.chat_id == chat_id).order_by(Member.display_name)
	)).scalars().all()

	if not members:
		reply = await message.reply("❌ Нет зарегистрированных клинеров.")
		asyncio.create_task(auto_delete_message(message, 3))
		asyncio.create_task(auto_delete_message(reply, 10))
		return

	keyboard = InlineKeyboardBuilder()
	for m in members:
		keyboard.button(text=m.display_name, callback_data=f"rmshift_user:{m.user_id}")
	keyboard.adjust(2)

	await message.reply("🗑 Выберите сотрудника:", reply_markup=keyboard.as_markup())
	await state.set_state(TimesheetStates.waiting_remove_date)
	asyncio.create_task(auto_delete_message(message, 3))


@router.callback_query(F.data.startswith("rmshift_user:"))
async def cb_rmshift_user(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Менеджер выбрал сотрудника для удаления смены."""
	if not await is_manager_db(callback.from_user.id, session):
		await callback.answer("❌ Нет доступа", show_alert=True)
		return

	target_user_id = int(callback.data.split(":", 1)[1])
	chat_id = callback.message.chat.id

	# Получаем табель сотрудника
	timesheets = (await session.execute(
		select(Timesheet).where(
			Timesheet.chat_id == chat_id,
			Timesheet.user_id == target_user_id
		).order_by(Timesheet.date.desc()).limit(30)
	)).scalars().all()

	member = await session.get(Member, {"chat_id": chat_id, "user_id": target_user_id})
	member_name = member.display_name if member else str(target_user_id)

	if not timesheets:
		await callback.message.edit_text(f"❌ У {member_name} нет записей в табеле.")
		await state.clear()
		await callback.answer()
		return

	keyboard = InlineKeyboardBuilder()
	for ts in timesheets:
		label = f"{ts.date.strftime('%d.%m')} {'🌅' if ts.shift_type == 'day' else '🌙'}"
		keyboard.button(text=label, callback_data=f"removeshift:{ts.id}")
	keyboard.adjust(3)

	await callback.message.edit_text(
		f"🗑 {member_name} — выберите смену для удаления:",
		reply_markup=keyboard.as_markup()
	)
	await callback.answer()


@router.callback_query(F.data.startswith("removeshift:"))
async def cb_removeshift(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Удаление записи из табеля."""
	if not await is_manager_db(callback.from_user.id, session):
		await callback.answer("❌ Нет доступа", show_alert=True)
		return

	timesheet_id = int(callback.data.split(":", 1)[1])
	timesheet = await session.get(Timesheet, timesheet_id)

	if not timesheet:
		await callback.answer("❌ Запись не найдена", show_alert=True)
		await state.clear()
		return

	shift_label = "🌅 день" if timesheet.shift_type == "day" else "🌙 ночь"
	date_str = timesheet.date.strftime("%d.%m")

	keyboard = InlineKeyboardBuilder()
	keyboard.button(text="✅ Удалить", callback_data=f"removeshift_confirm:{timesheet_id}:yes")
	keyboard.button(text="❌ Отмена", callback_data=f"removeshift_confirm:{timesheet_id}:no")
	keyboard.adjust(2)

	await callback.message.edit_text(
		f"Удалить {date_str} {shift_label}?",
		reply_markup=keyboard.as_markup()
	)
	await callback.answer()


@router.callback_query(F.data.startswith("removeshift_confirm:"))
async def cb_removeshift_confirm(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
	"""Подтверждение удаления."""
	if not await is_manager_db(callback.from_user.id, session):
		await callback.answer("❌ Нет доступа", show_alert=True)
		return

	parts = callback.data.split(":")
	timesheet_id = int(parts[1])
	confirm = parts[2]

	if confirm == "no":
		await callback.message.edit_text("❌ Отменено.")
		await state.clear()
		await callback.answer()
		return

	timesheet = await session.get(Timesheet, timesheet_id)
	if not timesheet:
		await callback.answer("❌ Запись не найдена", show_alert=True)
		await state.clear()
		return

	date_str = timesheet.date.strftime("%d.%m")
	shift_label = "🌅" if timesheet.shift_type == "day" else "🌙"

	await session.delete(timesheet)
	await session.commit()

	await callback.message.edit_text(f"✅ Удалено: {date_str} {shift_label}")
	await state.clear()
	await callback.answer("✅ Удалено!")