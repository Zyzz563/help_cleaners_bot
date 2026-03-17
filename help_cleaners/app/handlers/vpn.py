"""
Модуль продажи VPN.
Пользователь: /vpn → Купить → Lava оплата → автовыдача конфига.
Админ: /vpn → панель заказов; /clear_orders → сброс.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    OWNER_ID,
    VPN_PRICE,
    VPN_PRICE_AMOUNT,
    VPN_TRAFFIC_LIMIT_GB,
    VPN_DURATION_DAYS,
    LAVA_SHOP_ID,
)
from app.db.models import VpnOrder
from app.vpn_manager import xui_client
from app import lava_client

router = Router()
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════
#  Вспомогательные функции
# ═══════════════════════════════════════════════════

def _vpn_offer_text() -> str:
    return (
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


def _vpn_instructions_text(vless_link: str, expires: datetime) -> str:
    return (
        "🎉 <b>VPN подключён!</b>\n\n"
        f"📊 Трафик: {VPN_TRAFFIC_LIMIT_GB} ГБ\n"
        f"⏳ Действует до: {expires.strftime('%d.%m.%Y')}\n\n"
        "📋 <b>Ваша ссылка для подключения:</b>\n"
        f"<code>{vless_link}</code>\n\n"
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


async def activate_vpn_order(order: VpnOrder, session: AsyncSession, bot) -> bool:
    """
    Создаёт VPN-клиента в 3x-ui и отправляет конфиг пользователю.
    Возвращает True при успехе, False при ошибке.
    Вызывается из webhook, polling и ручного подтверждения.
    """
    result = await xui_client.add_vpn_client(
        order.user_id, order.username or f"id{order.user_id}"
    )

    if not result:
        order.status = "error"
        await session.commit()
        logger.error(f"VPN activation failed for order #{order.id}, user_id={order.user_id}")
        return False

    order.status = "active"
    order.uuid = result["uuid"]
    order.vless_link = result["vless_link"]
    order.paid_at = order.paid_at or datetime.utcnow()
    order.activated_at = datetime.utcnow()
    await session.commit()

    expires = datetime.utcnow() + timedelta(days=VPN_DURATION_DAYS)
    user_text = _vpn_instructions_text(result["vless_link"], expires)

    try:
        await bot.send_message(order.user_id, user_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Не удалось отправить VPN-конфиг пользователю {order.user_id}: {e}")
        try:
            await bot.send_message(
                OWNER_ID,
                f"⚠️ Не удалось доставить конфиг пользователю {order.user_id}.\n"
                f"Ссылка:\n<code>{result['vless_link']}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    return True


# ═══════════════════════════════════════════════════
#  /vpn — точка входа (разделение user / admin)
# ═══════════════════════════════════════════════════

@router.message(Command("vpn"))
async def cmd_vpn(message: Message, session: AsyncSession):
    if message.chat.type != "private":
        await message.reply("💬 Напишите мне в личные сообщения для покупки VPN.")
        return

    if message.from_user and message.from_user.id == OWNER_ID:
        await _show_admin_panel(message, session)
    else:
        await _send_vpn_offer(message)


async def _send_vpn_offer(target: Message):
    """Карточка VPN для обычного пользователя."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить VPN", callback_data="vpn:buy")
    await target.answer(_vpn_offer_text(), parse_mode="HTML", reply_markup=kb.as_markup())


async def _show_admin_panel(message: Message, session: AsyncSession):
    """Панель управления VPN для владельца."""
    pending = (await session.execute(
        select(VpnOrder).where(VpnOrder.status == "pending").order_by(VpnOrder.created_at)
    )).scalars().all()

    error_orders = (await session.execute(
        select(VpnOrder).where(VpnOrder.status == "error").order_by(VpnOrder.created_at)
    )).scalars().all()

    active_count = (await session.execute(
        select(VpnOrder).where(VpnOrder.status == "active")
    )).scalars().all()

    lines = [
        "📊 <b>VPN Админ-панель</b>\n",
        f"✅ Активных подписок: {len(active_count)}",
        f"⏳ Ожидают оплаты: {len(pending)}",
        f"❌ С ошибкой: {len(error_orders)}",
    ]

    if pending:
        lines.append("\n<b>Ожидающие заказы:</b>")
        for o in pending:
            lines.append(f"  #{o.id} — @{o.username} ({o.created_at.strftime('%d.%m %H:%M')})")

    if error_orders:
        lines.append("\n<b>Заказы с ошибкой:</b>")
        for o in error_orders:
            lines.append(f"  #{o.id} — @{o.username} ({o.created_at.strftime('%d.%m %H:%M')})")

    kb = InlineKeyboardBuilder()
    for o in pending:
        kb.button(text=f"✅ #{o.id} @{o.username}", callback_data=f"vpn:confirm:{o.id}")
    for o in error_orders:
        kb.button(text=f"🔄 #{o.id} @{o.username}", callback_data=f"vpn:retry:{o.id}")
    kb.button(text="🛒 Тест-покупка (для себя)", callback_data="vpn:buy")
    kb.adjust(1)

    lines.append("\n<i>Команды: /clear_orders — очистить все заказы</i>")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


# ═══════════════════════════════════════════════════
#  Пользовательский flow: покупка через Lava
# ═══════════════════════════════════════════════════

@router.callback_query(F.data == "vpn:show")
async def on_vpn_show(callback: CallbackQuery, session: AsyncSession):
    """Кнопка 'Купить VPN' из /start."""
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить VPN", callback_data="vpn:buy")
    await callback.message.edit_text(
        _vpn_offer_text(), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(F.data == "vpn:buy")
async def on_vpn_buy(callback: CallbackQuery, session: AsyncSession):
    """Пользователь нажал 'Купить VPN' — создаём счёт в Lava."""
    user = callback.from_user
    username = user.username or f"id{user.id}"
    is_admin = user.id == OWNER_ID

    # Проверка на дубль (не для админа)
    if not is_admin:
        existing = (await session.execute(
            select(VpnOrder).where(
                VpnOrder.user_id == user.id,
                VpnOrder.status.in_(["pending"]),
            )
        )).scalar_one_or_none()

        if existing:
            await callback.answer("⏳ У вас уже есть неоплаченный счёт.", show_alert=True)
            return

    await callback.answer("⏳ Создаю счёт на оплату...")

    # Создаём заказ в БД
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

    pay_oid = f"vpn_{order.id}_{int(datetime.utcnow().timestamp())}"
    order.aaio_order_id = pay_oid
    await session.commit()

    # Создаём счёт в Lava
    if LAVA_SHOP_ID:
        lava_result = await lava_client.create_payment(
            order_id=pay_oid,
            amount=VPN_PRICE_AMOUNT,
            comment=f"VPN {VPN_TRAFFIC_LIMIT_GB}GB / {VPN_DURATION_DAYS}d",
        )
        pay_url = lava_result["url"] if lava_result else None
    else:
        pay_url = None

    if pay_url:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"vpn:check:{order.id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"vpn:cancel:{order.id}")],
        ])
        await callback.message.edit_text(
            f"💳 <b>Счёт на оплату VPN</b>\n\n"
            f"📦 Тариф: {VPN_TRAFFIC_LIMIT_GB} ГБ / {VPN_DURATION_DAYS} дней\n"
            f"💰 Сумма: <b>{VPN_PRICE}</b>\n\n"
            f"Нажмите кнопку <b>«Оплатить»</b> для перехода на страницу оплаты.\n"
            f"После оплаты нажмите <b>«Проверить оплату»</b> — конфиг выдастся автоматически.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        # Fallback: Lava не настроен — ручное подтверждение админом
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=f"vpn:cancel:{order.id}")
        await callback.message.edit_text(
            "⚠️ Платёжная система временно недоступна.\n"
            "Администратор свяжется с вами для оплаты.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        # Уведомляем админа
        admin_kb = InlineKeyboardBuilder()
        admin_kb.button(text="✅ Подтвердить", callback_data=f"vpn:confirm:{order.id}")
        admin_kb.button(text="❌ Отклонить", callback_data=f"vpn:reject:{order.id}")
        admin_kb.adjust(1)
        try:
            await callback.bot.send_message(
                OWNER_ID,
                f"💰 <b>Заявка на VPN (ручная)</b>\n\n"
                f"👤 @{username} (ID: <code>{user.id}</code>)\n"
                f"📦 {VPN_TRAFFIC_LIMIT_GB} ГБ / {VPN_DURATION_DAYS} дней\n"
                f"💵 {VPN_PRICE}",
                parse_mode="HTML",
                reply_markup=admin_kb.as_markup(),
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа: {e}")


@router.callback_query(F.data.startswith("vpn:check:"))
async def on_vpn_check(callback: CallbackQuery, session: AsyncSession):
    """Пользователь нажал 'Проверить оплату' — опрашиваем Lava."""
    order_id = int(callback.data.split(":")[2])
    order = (await session.execute(
        select(VpnOrder).where(VpnOrder.id == order_id)
    )).scalar_one_or_none()

    if not order or not order.aaio_order_id:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    if order.status == "active":
        await callback.answer("✅ VPN уже выдан!", show_alert=True)
        return

    if order.status not in ("pending", "error"):
        await callback.answer(f"Статус заказа: {order.status}", show_alert=True)
        return

    await callback.answer("⏳ Проверяю оплату...")

    info = await lava_client.check_order_status(order.aaio_order_id)
    lava_status = info.get("data", {}).get("status") if info and info.get("data") else None

    if lava_status == "success":
        order.paid_at = datetime.utcnow()
        await session.commit()

        ok = await activate_vpn_order(order, session, callback.bot)
        if ok:
            await callback.message.edit_text(
                "✅ <b>Оплата подтверждена! VPN-конфиг отправлен выше.</b>",
                parse_mode="HTML",
            )
        else:
            await callback.message.edit_text(
                "💳 Оплата прошла, но при создании VPN произошла ошибка.\n"
                "Администратор разберётся и выдаст конфиг вручную.",
                parse_mode="HTML",
            )
            try:
                await callback.bot.send_message(
                    OWNER_ID,
                    f"⚠️ Оплата прошла, но VPN не создан!\n"
                    f"Заказ #{order.id}, user @{order.username} ({order.user_id})\n"
                    f"Статус: {order.status}",
                )
            except Exception:
                pass
    elif lava_status == "expired":
        order.status = "expired"
        await session.commit()
        await callback.message.edit_text(
            "⏰ Срок оплаты истёк. Нажмите /vpn чтобы создать новый счёт."
        )
    else:
        await callback.answer(
            f"Оплата ещё не поступила (статус: {lava_status or 'unknown'}).\nПопробуйте позже.",
            show_alert=True,
        )


@router.callback_query(F.data.startswith("vpn:cancel:"))
async def on_vpn_cancel(callback: CallbackQuery, session: AsyncSession):
    """Пользователь отменил покупку."""
    order_id = int(callback.data.split(":")[2])
    order = (await session.execute(
        select(VpnOrder).where(VpnOrder.id == order_id)
    )).scalar_one_or_none()

    if order and order.status == "pending":
        order.status = "cancelled"
        await session.commit()

    await callback.answer("Покупка отменена.")
    await callback.message.edit_text("❌ Покупка VPN отменена.\n\nНажмите /vpn чтобы начать заново.")


# ═══════════════════════════════════════════════════
#  Админ: подтверждение / отклонение / retry
# ═══════════════════════════════════════════════════

@router.callback_query(F.data.startswith("vpn:confirm:"))
async def on_vpn_confirm(callback: CallbackQuery, session: AsyncSession):
    """Владелец вручную подтверждает — создаёт VPN-клиента."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Только владелец.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[2])
    order = (await session.execute(
        select(VpnOrder).where(VpnOrder.id == order_id)
    )).scalar_one_or_none()

    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if order.status == "active":
        await callback.answer("Уже активен.", show_alert=True)
        return

    await callback.answer("⏳ Создаю VPN...")

    ok = await activate_vpn_order(order, session, callback.bot)
    if ok:
        await callback.message.edit_text(
            callback.message.text + "\n\n✅ <b>VPN выдан!</b>",
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            callback.message.text + "\n\n❌ <b>Ошибка 3x-ui! Статус → error.</b>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("vpn:retry:"))
async def on_vpn_retry(callback: CallbackQuery, session: AsyncSession):
    """Повторная попытка создания VPN для заказа со статусом error."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Только владелец.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[2])
    order = (await session.execute(
        select(VpnOrder).where(VpnOrder.id == order_id)
    )).scalar_one_or_none()

    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    await callback.answer("⏳ Повторная попытка...")

    ok = await activate_vpn_order(order, session, callback.bot)
    if ok:
        await callback.message.edit_text(
            callback.message.text + "\n\n✅ <b>Повтор успешен, VPN выдан!</b>",
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            callback.message.text + "\n\n❌ <b>Повтор не удался. Проверьте панель.</b>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("vpn:reject:"))
async def on_vpn_reject(callback: CallbackQuery, session: AsyncSession):
    """Владелец отклонил заказ."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Только владелец.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[2])
    order = (await session.execute(
        select(VpnOrder).where(VpnOrder.id == order_id)
    )).scalar_one_or_none()

    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    order.status = "rejected"
    await session.commit()

    await callback.answer("Отклонён.")
    await callback.message.edit_text(
        callback.message.text + "\n\n❌ <b>Отклонён.</b>",
        parse_mode="HTML",
    )

    try:
        await callback.bot.send_message(
            order.user_id,
            "❌ <b>Заявка на VPN отклонена.</b>\n"
            "Свяжитесь с администратором если оплата была произведена.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════
#  /clear_orders — полный сброс таблицы заказов
# ═══════════════════════════════════════════════════

@router.message(Command("clear_orders"))
async def cmd_clear_orders(message: Message, session: AsyncSession):
    if message.chat.type != "private":
        return
    if not message.from_user or message.from_user.id != OWNER_ID:
        return

    await session.execute(delete(VpnOrder))
    await session.commit()
    await message.reply("🗑 Все VPN-заказы удалены.")
