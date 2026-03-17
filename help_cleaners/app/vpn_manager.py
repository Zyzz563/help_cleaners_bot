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

    # ──────────────────────────────────────────────
    #  Авторизация
    # ──────────────────────────────────────────────
    async def get_token(self) -> bool:
        """
        Авторизация в панели 3x-ui.
        Отправляет логин/пароль, сохраняет сессионную куку.
        Возвращает True при успешной авторизации.
        """
        session = await self._ensure_session()
        try:
            async with session.post(
                f"{self.base_url}/login",
                data={"username": self.username, "password": self.password},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                result = await resp.json()
                if result.get("success"):
                    logger.info("3x-ui: авторизация успешна")
                    return True
                logger.error(f"3x-ui: ошибка авторизации — {result}")
                return False
        except Exception as e:
            logger.error(f"3x-ui: не удалось подключиться к панели — {e}")
            return False

    # ──────────────────────────────────────────────
    #  Получение информации об Inbound
    # ──────────────────────────────────────────────
    async def get_inbound_info(self, inbound_id: int) -> dict | None:
        """
        Получает настройки inbound — нужны для извлечения
        publicKey и shortIds из Reality-конфигурации.
        """
        session = await self._ensure_session()
        try:
            async with session.get(
                f"{self.base_url}/panel/api/inbounds/get/{inbound_id}",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                if not data.get("success"):
                    logger.error(f"3x-ui: не удалось получить inbound — {data}")
                    return None
                return data.get("obj")
        except Exception as e:
            logger.error(f"3x-ui: ошибка получения inbound — {e}")
            return None

    # ──────────────────────────────────────────────
    #  Создание VPN-клиента
    # ──────────────────────────────────────────────
    async def add_vpn_client(self, user_id: int, username: str) -> dict | None:
        """
        Создаёт нового VPN-клиента в панели 3x-ui.

        Возвращает словарь {uuid, vless_link, expires_at_ms, traffic_limit_gb, email}
        или None при ошибке.
        """
        # Авторизация (каждый раз свежая — куки могут протухнуть)
        if not await self.get_token():
            return None

        # Получаем Reality-ключи из настроек inbound
        inbound = await self.get_inbound_info(VPN_INBOUND_ID)
        if not inbound:
            return None

        stream_raw = inbound.get("streamSettings", "{}")
        stream_settings = json.loads(stream_raw) if isinstance(stream_raw, str) else stream_raw

        reality_settings = stream_settings.get("realitySettings", {})
        public_key = reality_settings.get("settings", {}).get("publicKey", "")
        short_ids = reality_settings.get("shortIds", [])
        short_id = short_ids[0] if short_ids else ""

        server_names = reality_settings.get("serverNames", ["www.microsoft.com"])
        sni = server_names[0] if server_names else "www.microsoft.com"

        if not public_key:
            logger.error("3x-ui: не найден publicKey в Reality-настройках")
            return None

        # Генерируем уникальные данные клиента
        client_uuid = str(uuid.uuid4())
        email = f"tg_{user_id}_{username}"

        expires_at_ms = int(
            (datetime.utcnow() + timedelta(days=VPN_DURATION_DAYS)).timestamp() * 1000
        )
        traffic_bytes = VPN_TRAFFIC_LIMIT_GB * 1024 * 1024 * 1024

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

        session = await self._ensure_session()
        try:
            async with session.post(
                f"{self.base_url}/panel/api/inbounds/addClient",
                json=client_payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                result = await resp.json()
                if not result.get("success"):
                    logger.error(f"3x-ui: не удалось добавить клиента — {result}")
                    return None
        except Exception as e:
            logger.error(f"3x-ui: ошибка при добавлении клиента — {e}")
            return None

        vless_link = self._generate_vless_link(
            client_uuid=client_uuid,
            remark=email,
            public_key=public_key,
            short_id=short_id,
            sni=sni,
        )

        logger.info(f"VPN-клиент создан: user_id={user_id}, uuid={client_uuid}")

        return {
            "uuid": client_uuid,
            "vless_link": vless_link,
            "expires_at_ms": expires_at_ms,
            "traffic_limit_gb": VPN_TRAFFIC_LIMIT_GB,
            "email": email,
        }

    # ──────────────────────────────────────────────
    #  Генерация VLESS-ссылки
    # ──────────────────────────────────────────────
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
