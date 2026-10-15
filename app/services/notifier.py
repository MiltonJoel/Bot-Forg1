import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from loguru import logger

from app.utils import http as http_ipv4
from app.utils.retry import short_error


class TelegramNotifier:
    """Telegram link for the bot: validates the token on startup, discovers the
    chat_id if it's missing, and pushes notifications (new predictions and
    bet results)."""

    def __init__(
        self, bot_token: str, chat_id: str, timeout_seconds: float = 20.0, attempts: int = 3
    ) -> None:
        self._api = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id
        self._timeout = timeout_seconds
        self._attempts = attempts

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
        return self._call(f"{self._api}/{method}")

    @property
    def chat_id(self) -> str:
        return self._chat_id

    async def pending_commands(self, offset: int) -> tuple[list[str], int]:
        """Texts sent to the bot since `offset`, and the next offset to use.

        Only messages from the configured chat are returned: the bot token is
        effectively public (anyone who learns it can message the bot), so a
        command from any other chat must never reach the control path.
        """
        updates = await asyncio.to_thread(self._get, f"getUpdates?offset={offset}&timeout=0")
        texts: list[str] = []
        next_offset = offset
        for update in updates.get("result", []):
            next_offset = max(next_offset, int(update["update_id"]) + 1)
            message = update.get("message") or {}
            if str(message.get("chat", {}).get("id")) != self._chat_id:
                continue
            if text := message.get("text"):
                texts.append(str(text).strip())
        return texts, next_offset

    def _call(self, url: str, payload: bytes | None = None) -> dict[str, Any]:
        """One Telegram API call, retried on transient network failure.

        api.telegram.org is reachable but SLOW from this network: the TLS
        handshake completes in ~0.25s and the full response then takes 10-16s,
        with occasional attempts stalling past 30s. The old 10s timeout sat
        below normal latency, so sends failed roughly at random with "handshake
        operation timed out". A stalled attempt does not recover, but a fresh
        connection succeeds in about a second — so retrying is what actually
        delivers the message, not waiting longer.

        HTTP 4xx is NOT retried — a bad token or chat_id is permanent, and
        hammering it just delays the error. Note a send that times out may still
        have been delivered, so a retry can duplicate a message; for
        notifications that beats silently losing a "bet placed" alert.
        """
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"} if payload else {},
        )
        last: Exception = RuntimeError("no attempt made")
        for attempt in range(1, self._attempts + 1):
            try:
                with http_ipv4.urlopen(request, timeout=self._timeout) as response:
                    body: dict[str, Any] = json.load(response)
                    return body
            except urllib.error.HTTPError as exc:
                # Telegram puts the real reason ("chat not found", "bot was
                # blocked") in the body; the status line alone is undebuggable.
                detail = exc.read().decode("utf-8", "replace")[:200]
                raise RuntimeError(f"Telegram HTTP {exc.code} — {detail}") from exc
            except Exception as exc:
                last = exc
                if attempt < self._attempts:
                    logger.warning(
                        "Telegram call slow/failed ({}); retrying {}/{}.",
                        short_error(exc),
                        attempt,
                        self._attempts - 1,
                    )
        raise last

    def _send_sync(self, text: str) -> None:
        payload = json.dumps({"chat_id": self._chat_id, "text": text}).encode()
        try:
            self._call(f"{self._api}/sendMessage", payload)
        except Exception as exc:  # notifications must never take the bot down
            logger.error(
                "Telegram send failed after {} attempts: {}", self._attempts, short_error(exc)
            )
