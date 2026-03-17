"""
Обработчики модуля продажи VPN.
Поток: /vpn → Купить → Реквизиты → Я оплатил → Подтверждение админом → Выдача конфига.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    OWNER_ID,
    VPN_PRICE,
    VPN_PAYMENT_DETAILS,
    VPN_TRAFFIC_LIMIT_GB,
    VPN_DURATION_DAYS,
)
from app.db.models import VpnOrder
from app.vpn_manager import xui_client

router = Router()
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════
#  Пользовательский flow
# ═══════════════════════════════════════════════════

@router.message(Command("vpn"))
async def cmd_vpn(message: Message, session: AsyncSession):
    """Команда /vpn — показывает предложение VPN (только в ЛС)."""
    if message.chat.type != "private":
        await message.reply("💬 Напишите мне в личные сообщения для покупки VPN.")
        return
    await _send_vpn_offer(message)


async def _send_vpn_offer(target: Message):
    """Отправляет карточку VPN-сервиса с кнопкой покупки."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить VPN", callback_data="vpn:buy")

    text = (
        "🔐 <b>VPN-сервис | VLESS + Reality</b>\n\n"
        "🇩🇪 Сервер в Германии — быстрый и стабильный\n"
        "🛡 Протокол VLESS + Reality — не определяется DPI\n"
        f"📊 Трафик: {VPN_TRAFFIC_LIMIT_GB} ГБ\n"
        f"⏳ Срок: {VPN_DURATION_DAYS} дней\n"
        f"💰 Цена: <b>{VPN_PRICE}</b>\n\n"
        "Работает с приложениями:\n"
        "• Android — <b>v2rayNG</b>\n"
        "• iOS — <b>Streisand</b> / <b>Shadowrocket</b>\n"
        "• Windows — <b>Hiddify</b> / <b>v2rayN</b>\n"
        "• macOS — <b>V2Box</b> / <b>Hiddify</b>"
    )
    await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data == "vpn:show")
async def on_vpn_show(callback: CallbackQuery, session: AsyncSession):
    """Кнопка 'Купить VPN' из главного меню /start."""
    await callback.answer()

    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить VPN", callback_data="vpn:buy")

    text = (
        "🔐 <b>VPN-сервис | VLESS + Reality</b>\n\n"
        "🇩🇪 Сервер в Германии — быстрый и стабильный\n"
        "🛡 Протокол VLESS + Reality — не определяется DPI\n"
        f"📊 Трафик: {VPN_TRAFFIC_LIMIT_GB} ГБ\n"
        f"⏳ Срок: {VPN_DURATION_DAYS} дней\n"
        f"💰 Цена: <b>{VPN_PRICE}</b>\n\n"
        "Работает с приложениями:\n"
        "• Android — <b>v2rayNG</b>\n"
        "• iOS — <b>Streisand</b> / <b>Shadowrocket</b>\n"
        "• Windows — <b>Hiddify</b> / <b>v2rayN</b>\n"
        "• macOS — <b>V2Box</b> / <b>Hiddify</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data == "vpn:buy")
async def on_vpn_buy(callback: CallbackQuery, session: AsyncSession):
    """Пользователь нажал 'Купить VPN' — показываем реквизиты для перевода."""
    await callback.answer()

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я оплатил", callback_data="vpn:paid")
    kb.button(text="◀ Назад", callback_data="vpn:show")
    kb.adjust(1)

    text = (
        f"💳 <b>Оплата VPN</b>\n\n"
        f"{VPN_PAYMENT_DETAILS}\n\n"
        f"После перевода нажмите кнопку <b>«Я оплатил»</b>.\n"
        f"Конфигурация будет выдана после подтверждения администратором."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data == "vpn:cancel")
async def on_vpn_cancel(callback: CallbackQuery):
    """Пользователь отменил покупку."""
    await callback.answer("Покупка отменена.")
    await callback.message.edit_text(
        "❌ Покупка VPN отменена.\n\nНажмите /vpn чтобы начать заново."
    )


@router.callback_query(F.data == "vpn:paid")
async def on_vpn_paid(callback: CallbackQuery, session: AsyncSession):
    """Пользователь нажал 'Я оплатил' — создаём заказ, уведомляем владельца."""
    await callback.answer("⏳ Заявка отправлена на проверку...")

    user = callback.from_user
    username = user.username or f"id{user.id}"

    # Защита от повторных заявок
    existing = (await session.execute(
        select(VpnOrder).where(
            VpnOrder.user_id == user.id,
            VpnOrder.status == "pending",
        )
    )).scalar_one_or_none()

    if existing:
        await callback.message.edit_text(
            "⏳ У вас уже есть заявка на рассмотрении.\n"
            "Дождитесь подтверждения от администратора."
        )
        return

    order = VpnOrder(
        user_id=user.id,
        username=username,
        status="pending",
        traffic_limit_gb=VPN_TRAFFIC_LIMIT_GB,
        duration_days=VPN_DURATION_DAYS,
        created_at=datetime.utcnow(),
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)

    await callback.message.edit_text(
        "✅ <b>Заявка отправлена!</b>\n\n"
        "Администратор проверит оплату и подтвердит подключение.\n"
        "Вы получите конфигурацию VPN в этот чат.",
        parse_mode="HTML",
    )

    # ─── Уведомляем владельца ───
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить оплату", callback_data=f"vpn:confirm:{order.id}")
    kb.button(text="❌ Отклонить", callback_data=f"vpn:reject:{order.id}")
    kb.adjust(1)

    admin_text = (
        f"💰 <b>Новая заявка на VPN</b>\n\n"
        f"👤 Пользователь: @{username} (ID: <code>{user.id}</code>)\n"
        f"📦 Тариф: {VPN_TRAFFIC_LIMIT_GB} ГБ / {VPN_DURATION_DAYS} дней\n"
        f"💵 Сумма: {VPN_PRICE}\n"
        f"🕐 Время: {datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC"
    )
    try:
        await callback.bot.send_message(
            OWNER_ID, admin_text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить админа о VPN-заказе: {e}")


# ═══════════════════════════════════════════════════
#  Админ-flow: подтверждение / отклонение
# ═══════════════════════════════════════════════════

@router.callback_query(F.data.startswith("vpn:confirm:"))
async def on_vpn_confirm(callback: CallbackQuery, session: AsyncSession):
    """Владелец подтвердил оплату — создаём VPN-клиента через 3x-ui и шлём конфиг."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Только владелец может подтверждать.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[2])
    order = (await session.execute(
        select(VpnOrder).where(VpnOrder.id == order_id)
    )).scalar_one_or_none()

    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if order.status != "pending":
        await callback.answer(f"Заказ уже обработан ({order.status}).", show_alert=True)
        return

    await callback.answer("⏳ Создаю VPN-клиента...")

    # Запрос к API 3x-ui
    result = await xui_client.add_vpn_client(
        order.user_id, order.username or f"id{order.user_id}"
    )

    if not result:
        await callback.message.edit_text(
            callback.message.text + "\n\n❌ <b>Ошибка при создании VPN!</b>\n"
            "Проверьте доступность панели 3x-ui.",
            parse_mode="HTML",
        )
        return

    # Обновляем заказ в БД
    order.status = "active"
    order.uuid = result["uuid"]
    order.vless_link = result["vless_link"]
    order.paid_at = datetime.utcnow()
    order.activated_at = datetime.utcnow()
    await session.commit()

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ <b>Оплата подтверждена, VPN выдан!</b>",
        parse_mode="HTML",
    )

    # ─── Отправляем конфиг пользователю ───
    expires = datetime.utcnow() + timedelta(days=VPN_DURATION_DAYS)

    user_text = (
        "🎉 <b>VPN подключён!</b>\n\n"
        f"📊 Трафик: {VPN_TRAFFIC_LIMIT_GB} ГБ\n"
        f"⏳ Действует до: {expires.strftime('%d.%m.%Y')}\n\n"
        "📋 <b>Ваша ссылка для подключения:</b>\n"
        f"<code>{result['vless_link']}</code>\n\n"
        "👆 Нажмите на ссылку чтобы скопировать\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📱 <b>Инструкция по подключению</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Android (v2rayNG):</b>\n"
        "1. Скачайте v2rayNG из Google Play\n"
        "2. Нажмите + → Импорт из буфера обмена\n"
        "3. Скопируйте ссылку выше и вставьте\n"
        "4. Нажмите ▶ для подключения\n\n"
        "<b>iOS (Streisand):</b>\n"
        "1. Скачайте Streisand из App Store\n"
        "2. Скопируйте ссылку → откройте приложение\n"
        "3. Конфигурация добавится автоматически\n"
        "4. Включите VPN\n\n"
        "<b>Windows (Hiddify):</b>\n"
        "1. Скачайте Hiddify с hiddify.com\n"
        "2. Добавить профиль → Из буфера обмена\n"
        "3. Скопируйте ссылку и вставьте\n"
        "4. Нажмите Подключить"
    )
    try:
        await callback.bot.send_message(order.user_id, user_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Не удалось отправить VPN-конфиг пользователю {order.user_id}: {e}")
        await callback.message.answer(
            f"⚠️ Не удалось отправить конфиг пользователю {order.user_id}.\n"
            f"Ссылка:\n<code>{result['vless_link']}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("vpn:reject:"))
async def on_vpn_reject(callback: CallbackQuery, session: AsyncSession):
    """Владелец отклонил оплату."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Только владелец может это делать.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[2])
    order = (await session.execute(
        select(VpnOrder).where(VpnOrder.id == order_id)
    )).scalar_one_or_none()

    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if order.status != "pending":
        await callback.answer(f"Заказ уже обработан ({order.status}).", show_alert=True)
        return

    order.status = "rejected"
    await session.commit()

    await callback.answer("Заказ отклонён.")
    await callback.message.edit_text(
        callback.message.text + "\n\n❌ <b>Заказ отклонён.</b>",
        parse_mode="HTML",
    )

    try:
        await callback.bot.send_message(
            order.user_id,
            "❌ <b>Заявка на VPN отклонена.</b>\n\n"
            "Оплата не подтверждена. Если вы уже оплатили, "
            "свяжитесь с администратором.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить пользователя {order.user_id} об отклонении: {e}")


# ═══════════════════════════════════════════════════
#  Админ: список заказов
# ═══════════════════════════════════════════════════

@router.message(Command("vpn_orders"))
async def cmd_vpn_orders(message: Message, session: AsyncSession):
    """Показывает ожидающие VPN-заказы (только для владельца)."""
    if message.chat.type != "private":
        return
    if not message.from_user or message.from_user.id != OWNER_ID:
        return

    orders = (await session.execute(
        select(VpnOrder).where(VpnOrder.status == "pending").order_by(VpnOrder.created_at)
    )).scalars().all()

    if not orders:
        await message.reply("📋 Нет ожидающих VPN-заказов.")
        return

    lines = ["📋 <b>Ожидающие VPN-заказы:</b>\n"]
    for o in orders:
        lines.append(
            f"#{o.id} — @{o.username} (ID: {o.user_id})\n"
            f"   📅 {o.created_at.strftime('%d.%m.%Y %H:%M')} UTC"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")
