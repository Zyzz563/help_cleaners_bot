from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def day_night_kb() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="🌅 Дневная", callback_data="shift:day"),
		 InlineKeyboardButton(text="🌙 Ночная", callback_data="shift:night")]
	])


def register_name_kb(username: str | None) -> InlineKeyboardMarkup:
	buttons = []
	if username:
		buttons.append([InlineKeyboardButton(text=f"Взять @{username}", callback_data="regname:use_username")])
	buttons.append([InlineKeyboardButton(text="Ввести вручную", callback_data="regname:manual")])
	return InlineKeyboardMarkup(inline_keyboard=buttons)


def gender_kb() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="М", callback_data="gender:m"),
		 InlineKeyboardButton(text="Ж", callback_data="gender:f")]
	])


def start_kb(username: str | None) -> InlineKeyboardMarkup:
	buttons = [[InlineKeyboardButton(text="Зарегистрироваться", callback_data="start:register")]]
	return InlineKeyboardMarkup(inline_keyboard=buttons)


def remove_menu_kb(is_admin: bool) -> InlineKeyboardMarkup:
	rows = [[InlineKeyboardButton(text="Удалить себя", callback_data="rm:self")]]
	if is_admin:
		rows.append([InlineKeyboardButton(text="Удалить клинера", callback_data="rm:other")])
	return InlineKeyboardMarkup(inline_keyboard=rows)


def members_choice_kb(options: list[tuple[str, int]]) -> InlineKeyboardMarkup:
	keyboard = []
	for name, uid in options:
		keyboard.append([InlineKeyboardButton(text=name, callback_data=f"rmuser:{uid}")])
	return InlineKeyboardMarkup(inline_keyboard=keyboard)


__all__ = [
	"day_night_kb",
	"register_name_kb",
	"gender_kb",
	"start_kb",
	"remove_menu_kb",
	"members_choice_kb",
]
