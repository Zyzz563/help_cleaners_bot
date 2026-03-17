"""
Клиент для платёжного агрегатора Lava (lava.ru).
Создание счетов, проверка подписи webhook, запрос статуса.

Документация: https://dev.lava.ru/api-invoice-create
"""

import hashlib
import hmac
import json
import logging

import aiohttp

from app.config import LAVA_SHOP_ID, LAVA_SECRET_KEY, LAVA_WEBHOOK_SECRET

logger = logging.getLogger(__name__)

LAVA_CREATE_URL = "https://api.lava.ru/business/invoice/create"
LAVA_STATUS_URL = "https://api.lava.ru/business/invoice/status"


def _make_signature(payload: dict, secret_key: str) -> str:
    """
    HMAC-SHA256 подпись для Lava API.
    Ключ — секретный ключ, данные — JSON тела запроса.
    """
    data_json = json.dumps(payload, separators=(",", ":"))
    return hmac.new(
        secret_key.encode("utf-8"),
        data_json.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def create_payment(order_id: str, amount: float, comment: str = "VPN подписка") -> dict | None:
    """
    Создаёт счёт на оплату через Lava API.
    Возвращает {"url": "...", "invoice_id": "..."} или None при ошибке.
    """
    if not LAVA_SHOP_ID or not LAVA_SECRET_KEY:
        print("[LAVA] ОШИБКА: LAVA_SHOP_ID или LAVA_SECRET_KEY не заданы")
        return None

    payload = {
        "sum": amount,
        "orderId": order_id,
        "shopId": LAVA_SHOP_ID,
        "comment": comment,
        "expire": 1440,
    }

    signature = _make_signature(payload, LAVA_SECRET_KEY)

    print(f"[LAVA] Создание счёта: order_id={order_id}, amount={amount}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                LAVA_CREATE_URL,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Signature": signature,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()
                print(f"[LAVA] Ответ create: status={resp.status}, body={body}")

                if resp.status == 200 and body.get("data"):
                    data = body["data"]
                    url = data.get("url")
                    invoice_id = data.get("id")
                    print(f"[LAVA] Ссылка: {url}, invoice_id: {invoice_id}")
                    return {"url": url, "invoice_id": invoice_id}

                print(f"[LAVA] ОШИБКА создания счёта: {body}")
                return None
    except Exception as e:
        print(f"[LAVA] ИСКЛЮЧЕНИЕ: {type(e).__name__}: {e}")
        return None


def verify_webhook_sign(data: dict) -> bool:
    """
    Проверяет подпись webhook от Lava.
    Формула: md5("invoice_id:amount:pay_time:secret_key_2")
    """
    if not LAVA_WEBHOOK_SECRET:
        print("[LAVA] ПРЕДУПРЕЖДЕНИЕ: LAVA_WEBHOOK_SECRET не задан, пропуск проверки")
        return True

    invoice_id = data.get("invoice_id", "")
    amount = data.get("amount", "")
    pay_time = data.get("pay_time", "")
    received_sign = data.get("sign", "")

    raw = f"{invoice_id}:{amount}:{pay_time}:{LAVA_WEBHOOK_SECRET}"
    expected = hashlib.md5(raw.encode("utf-8")).hexdigest()

    if received_sign == expected:
        print(f"[LAVA] Подпись webhook верна для invoice_id={invoice_id}")
        return True

    print(f"[LAVA] НЕВЕРНАЯ подпись! invoice={invoice_id}")
    print(f"[LAVA]   получена:  {received_sign}")
    print(f"[LAVA]   ожидалась: {expected}")
    return False


async def check_order_status(order_id: str) -> dict | None:
    """
    Запрашивает статус счёта через Lava API.
    Возвращает dict с данными заказа или None.
    """
    if not LAVA_SHOP_ID or not LAVA_SECRET_KEY:
        return None

    payload = {
        "shopId": LAVA_SHOP_ID,
        "orderId": order_id,
    }

    signature = _make_signature(payload, LAVA_SECRET_KEY)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                LAVA_STATUS_URL,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Signature": signature,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    return body
                return None
    except Exception as e:
        print(f"[LAVA] Ошибка проверки статуса order_id={order_id}: {e}")
        return None
