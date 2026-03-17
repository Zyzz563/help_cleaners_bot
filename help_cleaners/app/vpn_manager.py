"""
Модуль управления VPN через API панели 3x-ui (VLESS + Reality).
Авторизация, создание клиентов и генерация ссылок подключения.
"""

import aiohttp
import uuid
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import quote

from app.config import (
    VPN_PANEL_URL,
    VPN_PANEL_USER,
    VPN_PANEL_PASS,
    VPN_SERVER_IP,
    VPN_SERVER_PORT,
    VPN_INBOUND_ID,
    VPN_TRAFFIC_LIMIT_GB,
    VPN_DURATION_DAYS,
)

logger = logging.getLogger(__name__)


class XUIClient:
    """Асинхронный клиент для API панели 3x-ui."""

    def __init__(self):
        self.base_url = VPN_PANEL_URL.rstrip("/")
        self.username = VPN_PANEL_USER
        self.password = VPN_PANEL_PASS
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Создаёт HTTP-сессию с хранилищем кук, если её ещё нет."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
        return self._session

    async def close(self):
        """Закрывает HTTP-сессию."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_token(self) -> bool:
        """
        Авторизация в панели 3x-ui.
        Отправляет логин/пароль, сохраняет сессионную куку.
        """
        # Закрываем старую сессию чтобы куки не протухли
        await self.close()
        session = await self._ensure_session()

        url = f"{self.base_url}/login"
        print(f"[VPN-DEBUG] Авторизация: POST {url}")
        print(f"[VPN-DEBUG] Логин: {self.username}")

        try:
            async with session.post(
                url,
                data={"username": self.username, "password": self.password},
                timeout=aiohttp.ClientTimeout(total=15),
                ssl=False,
            ) as resp:
                status = resp.status
                body = await resp.text()
                print(f"[VPN-DEBUG] Ответ login: status={status}")
                print(f"[VPN-DEBUG] Ответ login body: {body[:500]}")
                print(f"[VPN-DEBUG] Куки после login: {session.cookie_jar.filter_cookies(self.base_url)}")

                if status != 200:
                    print(f"[VPN-DEBUG] ОШИБКА: HTTP {status} при авторизации")
                    return False

                try:
                    result = json.loads(body)
                except json.JSONDecodeError:
                    print(f"[VPN-DEBUG] ОШИБКА: ответ не JSON — {body[:200]}")
                    return False

                if result.get("success"):
                    print("[VPN-DEBUG] Авторизация УСПЕШНА ✓")
                    return True

                print(f"[VPN-DEBUG] ОШИБКА авторизации: {result}")
                return False
        except aiohttp.ClientConnectorError as e:
            print(f"[VPN-DEBUG] НЕ УДАЛОСЬ ПОДКЛЮЧИТЬСЯ к {url}: {e}")
            return False
        except Exception as e:
            print(f"[VPN-DEBUG] ИСКЛЮЧЕНИЕ при авторизации: {type(e).__name__}: {e}")
            return False

    async def get_inbound_info(self, inbound_id: int) -> dict | None:
        """Получает настройки inbound для извлечения Reality-ключей."""
        session = await self._ensure_session()
        url = f"{self.base_url}/panel/api/inbounds/get/{inbound_id}"
        print(f"[VPN-DEBUG] Получение inbound: GET {url}")

        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                ssl=False,
            ) as resp:
                status = resp.status
                body = await resp.text()
                print(f"[VPN-DEBUG] Ответ inbound: status={status}")
                print(f"[VPN-DEBUG] Ответ inbound body: {body[:800]}")

                if status != 200:
                    print(f"[VPN-DEBUG] ОШИБКА: HTTP {status} при получении inbound")
                    return None

                data = json.loads(body)
                if not data.get("success"):
                    print(f"[VPN-DEBUG] ОШИБКА: inbound не получен — {data}")
                    return None

                print("[VPN-DEBUG] Inbound получен ✓")
                return data.get("obj")
        except Exception as e:
            print(f"[VPN-DEBUG] ИСКЛЮЧЕНИЕ при получении inbound: {type(e).__name__}: {e}")
            return None

    async def add_vpn_client(self, user_id: int, username: str) -> dict | None:
        """
        Создаёт нового VPN-клиента в панели 3x-ui.
        Возвращает {uuid, vless_link, expires_at_ms, traffic_limit_gb, email} или None.
        """
        print(f"[VPN-DEBUG] === Создание VPN-клиента для user_id={user_id}, username={username} ===")
        print(f"[VPN-DEBUG] Панель: {self.base_url}")

        # Шаг 1: авторизация
        if not await self.get_token():
            print("[VPN-DEBUG] ПРОВАЛ: авторизация не пройдена")
            return None

        # Шаг 2: получаем Reality-ключи из inbound
        inbound = await self.get_inbound_info(VPN_INBOUND_ID)
        if not inbound:
            print("[VPN-DEBUG] ПРОВАЛ: не удалось получить inbound")
            return None

        # Парсим streamSettings — может быть строкой или dict
        stream_raw = inbound.get("streamSettings", "{}")
        stream_settings = json.loads(stream_raw) if isinstance(stream_raw, str) else stream_raw
        print(f"[VPN-DEBUG] streamSettings keys: {list(stream_settings.keys())}")

        reality_settings = stream_settings.get("realitySettings", {})
        print(f"[VPN-DEBUG] realitySettings keys: {list(reality_settings.keys())}")

        # publicKey лежит в realitySettings.settings.publicKey
        settings_inner = reality_settings.get("settings", {})
        public_key = settings_inner.get("publicKey", "")
        short_ids = reality_settings.get("shortIds", [])
        short_id = short_ids[0] if short_ids else ""
        server_names = reality_settings.get("serverNames", ["www.microsoft.com"])
        sni = server_names[0] if server_names else "www.microsoft.com"

        print(f"[VPN-DEBUG] publicKey: {public_key[:20]}..." if public_key else "[VPN-DEBUG] publicKey: ПУСТО!")
        print(f"[VPN-DEBUG] shortId: {short_id}")
        print(f"[VPN-DEBUG] sni: {sni}")

        if not public_key:
            print("[VPN-DEBUG] ПРОВАЛ: publicKey не найден в Reality-настройках")
            # Пробуем альтернативные пути
            print(f"[VPN-DEBUG] Полный realitySettings: {json.dumps(reality_settings, indent=2)[:500]}")
            return None

        # Шаг 3: генерируем UUID и параметры
        client_uuid = str(uuid.uuid4())
        email = f"tg_{user_id}_{username}"
        expires_at_ms = int(
            (datetime.utcnow() + timedelta(days=VPN_DURATION_DAYS)).timestamp() * 1000
        )
        traffic_bytes = VPN_TRAFFIC_LIMIT_GB * 1024 * 1024 * 1024

        print(f"[VPN-DEBUG] UUID: {client_uuid}")
        print(f"[VPN-DEBUG] email: {email}")
        print(f"[VPN-DEBUG] трафик: {traffic_bytes} байт ({VPN_TRAFFIC_LIMIT_GB} ГБ)")
        print(f"[VPN-DEBUG] истекает: {expires_at_ms} ({VPN_DURATION_DAYS} дней)")

        # Шаг 4: добавляем клиента через API
        client_payload = {
            "id": VPN_INBOUND_ID,
            "settings": json.dumps({
                "clients": [{
                    "id": client_uuid,
                    "flow": "xtls-rprx-vision",
                    "email": email,
                    "limitIp": 2,
                    "totalGB": traffic_bytes,
                    "expiryTime": expires_at_ms,
                    "enable": True,
                    "tgId": str(user_id),
                    "subId": "",
                }]
            }),
        }

        url = f"{self.base_url}/panel/api/inbounds/addClient"
        print(f"[VPN-DEBUG] Добавление клиента: POST {url}")
        print(f"[VPN-DEBUG] Payload: {json.dumps(client_payload, indent=2)[:600]}")

        session = await self._ensure_session()
        try:
            async with session.post(
                url,
                json=client_payload,
                timeout=aiohttp.ClientTimeout(total=15),
                ssl=False,
            ) as resp:
                status = resp.status
                body = await resp.text()
                print(f"[VPN-DEBUG] Ответ addClient: status={status}")
                print(f"[VPN-DEBUG] Ответ addClient body: {body[:500]}")

                if status != 200:
                    print(f"[VPN-DEBUG] ОШИБКА: HTTP {status} при добавлении клиента")
                    return None

                result = json.loads(body)
                if not result.get("success"):
                    print(f"[VPN-DEBUG] ОШИБКА API: {result}")
                    return None
        except Exception as e:
            print(f"[VPN-DEBUG] ИСКЛЮЧЕНИЕ при addClient: {type(e).__name__}: {e}")
            return None

        # Шаг 5: генерируем VLESS-ссылку
        vless_link = self._generate_vless_link(
            client_uuid=client_uuid,
            remark=email,
            public_key=public_key,
            short_id=short_id,
            sni=sni,
        )

        print(f"[VPN-DEBUG] УСПЕХ ✓ VPN-клиент создан!")
        print(f"[VPN-DEBUG] VLESS-ссылка: {vless_link[:80]}...")

        return {
            "uuid": client_uuid,
            "vless_link": vless_link,
            "expires_at_ms": expires_at_ms,
            "traffic_limit_gb": VPN_TRAFFIC_LIMIT_GB,
            "email": email,
        }

    def _generate_vless_link(
        self,
        client_uuid: str,
        remark: str,
        public_key: str,
        short_id: str,
        sni: str = "www.microsoft.com",
    ) -> str:
        """Собирает VLESS + Reality ссылку для v2rayNG / Shadowrocket / Hiddify."""
        params = (
            f"type=tcp"
            f"&security=reality"
            f"&pbk={quote(public_key)}"
            f"&fp=chrome"
            f"&sni={sni}"
            f"&sid={short_id}"
            f"&spx=%2F"
            f"&flow=xtls-rprx-vision"
        )
        return f"vless://{client_uuid}@{VPN_SERVER_IP}:{VPN_SERVER_PORT}?{params}#{quote(remark)}"


# Глобальный экземпляр — используется в обработчиках бота
xui_client = XUIClient()
