import asyncio
import json
import urllib.request
from typing import Any

from loguru import logger


class TelegramNotifier:
    """Telegram link for the bot: validates the token on startup, discovers the
    chat_id if it's missing, and pushes notifications (new predictions and
    bet results)."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._api = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id

    async def connect(self) -> None:
        """Verifies the token, resolves chat_id, and sends a startup message so
        the whole link is proven working before the bot does anything else."""
        me = await asyncio.to_thread(self._get, "getMe")
        if not me.get("ok"):
            raise RuntimeError(f"Token de Telegram inválido: {me}")
        logger.info("Telegram conectado como @{}", me["result"].get("username"))

        if not self._chat_id:
            updates = await asyncio.to_thread(self._get, "getUpdates")
            messages = [u["message"] for u in updates.get("result", []) if u.get("message")]
            if not messages:
                raise RuntimeError(
                    "TELEGRAM_CHAT_ID está vacío y el bot no tiene mensajes. "
                    "Mandale cualquier mensaje al bot en Telegram y volvé a iniciar."
                )
            self._chat_id = str(messages[-1]["chat"]["id"])
            logger.info(
                "chat_id detectado: {} — guardalo en .env como TELEGRAM_CHAT_ID", self._chat_id
            )

        await self.send("✅ Bot forg1 conectado y en marcha")

    async def send(self, text: str) -> None:
        await asyncio.to_thread(self._send_sync, text)

    def _get(self, method: str) -> dict[str, Any]:
        with urllib.request.urlopen(f"{self._api}/{method}", timeout=10) as response:
            data: dict[str, Any] = json.load(response)
            return data

    def _send_sync(self, text: str) -> None:
        payload = json.dumps({"chat_id": self._chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"{self._api}/sendMessage", data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status != 200:
                    logger.error("Telegram send failed: HTTP {}", response.status)
        except Exception as exc:
            logger.error("Telegram send failed: {}", exc)
