import asyncio
from typing import Any, Awaitable, Callable, Dict
from datetime import datetime, timedelta

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage

from app.handlers.commands import RegStates


class FSMTimerMiddleware(BaseMiddleware):
	"""Middleware для автоматической очистки устаревших FSM-состояний"""
	
	async def __call__(
		self,
		handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
		event: TelegramObject,
		data: Dict[str, Any]
	) -> Any:
		# Проверяем FSM состояние и очищаем если устарело
		if isinstance(event, (Message, CallbackQuery)):
			state: FSMContext = data.get("state")
			if state:
				await self._check_fsm_timeout(state, event)
		
		return await handler(event, data)
	
	async def _check_fsm_timeout(self, state: FSMContext, event: TelegramObject) -> None:
		"""Проверяет таймаут FSM состояния и очищает если нужно"""
		try:
			current_state = await state.get_state()
			if not current_state:
				return
			
			# Получаем время последнего обновления состояния
			storage: BaseStorage = state.storage
			key = state.key
			
			# Если состояние слишком старое - очищаем
			if current_state == RegStates.waiting_name.state:
				# Проверяем таймаут для ввода имени
				await self._cleanup_if_expired(state, RegStates.waiting_name_timeout, "ввода имени")
			elif current_state == RegStates.waiting_gender.state:
				# Проверяем таймаут для выбора пола
				await self._cleanup_if_expired(state, RegStates.waiting_gender_timeout, "выбора пола")
				
		except Exception:
			# В случае ошибки - просто очищаем состояние
			await state.clear()
	
	async def _cleanup_if_expired(self, state: FSMContext, timeout_seconds: int, action_name: str) -> None:
		"""Очищает состояние если оно устарело"""
		try:
			# Получаем данные состояния
			data = await state.get_data()
			last_activity = data.get("_last_activity")
			
			if last_activity:
				last_time = datetime.fromisoformat(last_activity)
				if datetime.now() - last_time > timedelta(seconds=timeout_seconds):
					await state.clear()
					return
			
			# Обновляем время последней активности
			await state.update_data(_last_activity=datetime.now().isoformat())
			
		except Exception:
			# В случае ошибки - очищаем состояние
			await state.clear() 