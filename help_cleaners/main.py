import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import TelegramObject, Update
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine, AsyncSession, async_sessionmaker

from app.handlers import commands_router, photos_router, vpn_router
from app.db.models import Base, Shift, Timesheet, Group, Manager, VpnOrder
from app.config import DEFAULT_TZ, AAIO_WEBHOOK_PORT, AAIO_MERCHANT_ID
from app import aaio_client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = os.getenv("BOT_TOKEN") or "7669076544:AAF7D9FSNqzclEos9AP3NSDyZ0U3fjUsDbk"
OWNER_ID = int(os.getenv("OWNER_ID") or "6405212136")

logger.info(f"Bot token: {BOT_TOKEN[:10]}...")
logger.info(f"Owner ID: {OWNER_ID}")

# Initialize bot and dispatcher
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Database configuration
DATABASE_URL = "sqlite+aiosqlite:///./help_cleaners.db"
engine = create_async_engine(DATABASE_URL, echo=False)
session_maker = async_sessionmaker(engine, expire_on_commit=False)


class DbSessionMiddleware(BaseMiddleware):
    """Middleware that injects an AsyncSession into every handler."""

    def __init__(self, session_pool: async_sessionmaker[AsyncSession]):
        super().__init__()
        self.session_pool = session_pool

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with self.session_pool() as session:
            data["session"] = session
            return await handler(event, data)


class DebugMiddleware(BaseMiddleware):
    """Middleware to log every incoming update for debugging."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            update = event
            if update.message:
                msg = update.message
                logger.info(
                    f"[DEBUG] Message from chat={msg.chat.id} "
                    f"type={msg.chat.type} user={msg.from_user.id if msg.from_user else '?'} "
                    f"content_type={msg.content_type} text={msg.text!r}"
                )
            elif update.callback_query:
                cb = update.callback_query
                logger.info(
                    f"[DEBUG] CallbackQuery from user={cb.from_user.id} data={cb.data!r}"
                )
            elif update.my_chat_member:
                mcm = update.my_chat_member
                logger.info(
                    f"[DEBUG] MyChatMember chat={mcm.chat.id} "
                    f"old={mcm.old_chat_member.status} new={mcm.new_chat_member.status}"
                )
            else:
                logger.info(f"[DEBUG] Other update type: {update.model_dump_json()[:200]}")
        return await handler(event, data)


# Register OUTER middleware on dp.update so it runs before everything
# This ensures 'session' is always in data dict for all handlers
dp.update.outer_middleware(DebugMiddleware())
dp.update.outer_middleware(DbSessionMiddleware(session_maker))

# Include routers
dp.include_router(vpn_router)
dp.include_router(commands_router)
dp.include_router(photos_router)


async def init_db(eng: AsyncEngine) -> None:
    """Create all tables if they don't exist, then run migrations."""
    # 1. Create all tables from models
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")

    # 2. Run incremental migrations (ALTER TABLE, etc.)
    from app.database import init_db as run_migrations
    await run_migrations(eng)
    logger.info("Database migrations completed")


async def on_startup(b: Bot, eng: AsyncEngine):
    """Bot startup handler"""
    logger.info("Bot starting up...")
    await init_db(eng)
    logger.info("Bot startup completed!")


async def on_shutdown(b: Bot, eng: AsyncEngine):
    """Bot shutdown handler"""
    logger.info("Bot shutting down...")
    await eng.dispose()
    logger.info("Bot shutdown completed!")


async def auto_confirm_shifts(sm: async_sessionmaker[AsyncSession]):
    """
    Фоновая задача: каждые 30 мин проверяет смены, у которых время прошло,
    и автоматически заносит их в табель.
    Дневная (09:00–21:00): подтверждается после 21:00 того же дня.
    Ночная  (21:00–09:00): подтверждается после 09:00 следующего дня.
    """
    import pytz
    while True:
        try:
            async with sm() as session:
                # Текущее время по Москве
                tz = pytz.timezone(DEFAULT_TZ)
                now = datetime.now(tz)
                today = now.date()

                # Все смены за последние 3 дня (чтобы не пропустить ночные)
                cutoff = today - timedelta(days=3)
                shifts = (await session.execute(
                    select(Shift).where(Shift.date >= cutoff)
                )).scalars().all()

                confirmed = 0
                for shift in shifts:
                    # Проверяем: прошло ли время смены?
                    if shift.type == "day":
                        # Дневная смена заканчивается в 21:00 того же дня
                        end_dt = tz.localize(datetime.combine(shift.date, datetime.min.time()).replace(hour=21))
                    elif shift.type == "night":
                        # Ночная смена заканчивается в 09:00 СЛЕДУЮЩЕГО дня
                        next_day = shift.date + timedelta(days=1)
                        end_dt = tz.localize(datetime.combine(next_day, datetime.min.time()).replace(hour=9))
                    else:
                        continue

                    if now < end_dt:
                        continue  # Смена ещё не закончилась

                    # Проверяем: нет ли уже записи в табеле?
                    existing = (await session.execute(
                        select(Timesheet).where(
                            Timesheet.chat_id == shift.chat_id,
                            Timesheet.user_id == shift.user_id,
                            Timesheet.date == shift.date,
                            Timesheet.shift_type == shift.type,
                        )
                    )).scalar_one_or_none()

                    if existing:
                        continue  # Уже в табеле

                    # Создаём запись в табеле
                    ts = Timesheet(
                        chat_id=shift.chat_id,
                        user_id=shift.user_id,
                        date=shift.date,
                        shift_type=shift.type,
                        photo_count=0,
                        confirmed_at=datetime.utcnow(),
                    )
                    session.add(ts)
                    confirmed += 1

                if confirmed:
                    await session.commit()
                    logger.info(f"[AutoConfirm] Перенесено в табель: {confirmed} смен")

        except Exception as e:
            logger.error(f"[AutoConfirm] Ошибка: {e}")

        await asyncio.sleep(30 * 60)  # Каждые 30 минут


async def aaio_webhook_handler(request: web.Request) -> web.Response:
    """
    Принимает POST от AAIO при успешной оплате.
    Проверяет подпись, активирует VPN, отправляет конфиг пользователю.
    """
    try:
        data = await request.post()
        data = dict(data)
        print(f"[AAIO-WEBHOOK] Получен запрос: {data}")

        if not aaio_client.verify_webhook_sign(data):
            print("[AAIO-WEBHOOK] Неверная подпись — отклонено")
            return web.Response(text="bad sign", status=400)

        order_id_str = data.get("order_id", "")
        print(f"[AAIO-WEBHOOK] Обработка order_id={order_id_str}")

        async with session_maker() as session:
            order = (await session.execute(
                select(VpnOrder).where(VpnOrder.aaio_order_id == order_id_str)
            )).scalar_one_or_none()

            if not order:
                print(f"[AAIO-WEBHOOK] Заказ не найден: {order_id_str}")
                return web.Response(text="order not found", status=404)

            if order.status == "active":
                print(f"[AAIO-WEBHOOK] Заказ уже активен: #{order.id}")
                return web.Response(text="OK")

            order.paid_at = datetime.utcnow()
            await session.commit()

            from app.handlers.vpn import activate_vpn_order
            ok = await activate_vpn_order(order, session, bot)

            if ok:
                print(f"[AAIO-WEBHOOK] VPN выдан: order #{order.id}, user {order.user_id}")
                try:
                    await bot.send_message(
                        OWNER_ID,
                        f"✅ Авто-оплата: @{order.username} (#{order.id}) — VPN выдан!",
                    )
                except Exception:
                    pass
            else:
                print(f"[AAIO-WEBHOOK] Ошибка создания VPN для order #{order.id}")
                try:
                    await bot.send_message(
                        OWNER_ID,
                        f"⚠️ Оплата #{order.id} @{order.username} прошла, "
                        f"но VPN не создан! Проверьте 3x-ui.",
                    )
                except Exception:
                    pass

        return web.Response(text="OK")
    except Exception as e:
        print(f"[AAIO-WEBHOOK] Ошибка: {e}")
        return web.Response(text="error", status=500)


async def aaio_payment_poller(sm: async_sessionmaker[AsyncSession]):
    """
    Фоновая задача: каждые 60 секунд проверяет pending-заказы через AAIO API.
    Если оплата прошла — автоматически активирует VPN.
    """
    await asyncio.sleep(10)
    while True:
        try:
            if not AAIO_MERCHANT_ID:
                await asyncio.sleep(60)
                continue

            async with sm() as session:
                cutoff = datetime.utcnow() - timedelta(minutes=2)
                orders = (await session.execute(
                    select(VpnOrder).where(
                        VpnOrder.status == "pending",
                        VpnOrder.aaio_order_id.isnot(None),
                        VpnOrder.created_at <= cutoff,
                    )
                )).scalars().all()

                for order in orders:
                    info = await aaio_client.check_order_status(order.aaio_order_id)
                    if not info or info.get("type") != "success":
                        continue

                    if info.get("status") == "success":
                        print(f"[POLLER] Оплата подтверждена для order #{order.id}")
                        order.paid_at = datetime.utcnow()
                        await session.commit()

                        from app.handlers.vpn import activate_vpn_order
                        ok = await activate_vpn_order(order, session, bot)
                        if ok:
                            try:
                                await bot.send_message(
                                    OWNER_ID,
                                    f"✅ Авто-оплата (poller): @{order.username} (#{order.id}) — VPN выдан!",
                                )
                            except Exception:
                                pass

                    elif info.get("status") == "expired":
                        order.status = "expired"
                        await session.commit()

        except Exception as e:
            logger.error(f"[POLLER] Ошибка: {e}")

        await asyncio.sleep(60)


async def main():
    """Main function"""
    logger.info("Starting bot...")

    try:
        # Startup
        await on_startup(bot, engine)

        # Фоновые задачи
        asyncio.create_task(auto_confirm_shifts(session_maker))
        logger.info("Auto-confirm background task started")

        asyncio.create_task(aaio_payment_poller(session_maker))
        logger.info("AAIO payment poller started")

        # Webhook-сервер для AAIO
        app = web.Application()
        app.router.add_post("/aaio/webhook", aaio_webhook_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", AAIO_WEBHOOK_PORT)
        await site.start()
        logger.info(f"AAIO webhook server listening on port {AAIO_WEBHOOK_PORT}")

        # Start polling
        logger.info("Starting polling...")
        await dp.start_polling(bot)

    except Exception as e:
        logger.error(f"Error in main: {e}")
        raise
    finally:
        # Shutdown
        await on_shutdown(bot, engine)


if __name__ == "__main__":
    try:
        logger.info("Bot main function started")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        raise
