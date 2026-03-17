"""
Клиент для платёжного агрегатора AAIO (aaio.so).
Создание счетов, проверка подписи webhook, запрос статуса заказа.
"""

import hashlib
import logging
from urllib.parse import urlencode

import aiohttp

from app.config import (
    AAIO_MERCHANT_ID,
    AAIO_SECRET_1,
    AAIO_SECRET_2,
    AAIO_API_KEY,
)

logger = logging.getLogger(__name__)

AAIO_PAY_URL = "https://aaio.so/merchant/get_pay_url"
AAIO_INFO_URL = "https://aaio.so/api/info-pay"


def _make_sign(merchant_id: str, amount: str, currency: str, secret: str, order_id: str) -> str:
    """SHA-256 подпись: merchant_id:amount:currency:secret:order_id"""
    raw = ":".join([merchant_id, amount, currency, secret, order_id])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def create_payment(order_id: str, amount: float, description: str = "VPN подписка") -> str | None:
    """
    Создаёт счёт на оплату через AAIO API.
    Возвращает URL страницы оплаты или None при ошибке.
    """
    if not AAIO_MERCHANT_ID or not AAIO_SECRET_1:
        print("[AAIO] ОШИБКА: AAIO_MERCHANT_ID или AAIO_SECRET_1 не заданы")
        return None

    amount_str = f"{amount:.2f}"
    currency = "RUB"
    sign = _make_sign(AAIO_MERCHANT_ID, amount_str, currency, AAIO_SECRET_1, order_id)

    params = {
        "merchant_id": AAIO_MERCHANT_ID,
        "amount": amount_str,
        "currency": currency,
        "order_id": order_id,
        "sign": sign,
        "desc": description,
        "lang": "ru",
    }

    print(f"[AAIO] Создание счёта: order_id={order_id}, amount={amount_str}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                AAIO_PAY_URL,
                data=params,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()
                print(f"[AAIO] Ответ: status={resp.status}, body={body}")

                if resp.status == 200 and body.get("type") == "success":
                    url = body.get("url")
                    print(f"[AAIO] Ссылка на оплату: {url}")
                    return url

                print(f"[AAIO] ОШИБКА создания счёта: {body}")
                return None
    except Exception as e:
        print(f"[AAIO] ИСКЛЮЧЕНИЕ: {type(e).__name__}: {e}")
        return None


def verify_webhook_sign(data: dict) -> bool:
    """
    Проверяет подпись webhook-уведомления от AAIO.
    Использует Secret Key 2 для верификации.
    """
    if not AAIO_SECRET_2:
        print("[AAIO] ОШИБКА: AAIO_SECRET_2 не задан")
        return False

    merchant_id = data.get("merchant_id", "")
    amount = data.get("amount", "")
    currency = data.get("currency", "")
    order_id = data.get("order_id", "")
    received_sign = data.get("sign", "")

    expected_sign = _make_sign(merchant_id, amount, currency, AAIO_SECRET_2, order_id)

    if received_sign == expected_sign:
        print(f"[AAIO] Подпись webhook верна для order_id={order_id}")
        return True

    print(f"[AAIO] НЕВЕРНАЯ подпись webhook! order_id={order_id}")
    print(f"[AAIO]   получена:  {received_sign}")
    print(f"[AAIO]   ожидалась: {expected_sign}")
    return False


async def check_order_status(order_id: str) -> dict | None:
    """
    Запрашивает статус заказа через AAIO API.
    Возвращает dict с полями type, status и т.д. или None при ошибке.
    """
    if not AAIO_API_KEY or not AAIO_MERCHANT_ID:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                AAIO_INFO_URL,
                data={
                    "merchant_id": AAIO_MERCHANT_ID,
                    "order_id": order_id,
                },
                headers={
                    "Accept": "application/json",
                    "X-Api-Key": AAIO_API_KEY,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 400, 401):
                    body = await resp.json()
                    return body
                return None
    except Exception as e:
        print(f"[AAIO] Ошибка проверки статуса order_id={order_id}: {e}")
        return None
