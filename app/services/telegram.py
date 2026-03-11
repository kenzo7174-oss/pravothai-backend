"""
Axoloti Terminal — Сервис интеграции с Telegram Bot API.

Предоставляет асинхронную отправку сообщений через httpx
и хелпер для разбора входящих Webhook-обновлений.
"""

import logging

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


async def _telegram_api_post(method: str, payload: dict) -> dict | None:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN не задан — вызов %s пропущен", method)
        return None

    url = f"{TELEGRAM_API}/bot{token}/{method}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        log.error(
            "Telegram API %s вернул %s: %s",
            method,
            exc.response.status_code,
            exc.response.text,
        )
    except httpx.RequestError as exc:
        log.error("Ошибка сети при вызове Telegram API %s: %s", method, exc)

    return None


async def get_telegram_webhook_info() -> dict | None:
    """
    Возвращает текущее состояние webhook у Telegram Bot API.
    """
    return await _telegram_api_post("getWebhookInfo", {})


async def send_telegram_message(chat_id: str, text: str) -> bool:
    """
    Отправляет текстовое сообщение в Telegram-чат.
    Возвращает True при успехе, False при ошибке.
    """
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    return await _telegram_api_post("sendMessage", payload) is not None


async def get_telegram_file_url(file_id: str) -> str | None:
    """Получает прямую ссылку на файл через Telegram getFile API."""
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN не задан — getFile пропущен")
        return None

    data = await _telegram_api_post("getFile", {"file_id": file_id})
    if not data:
        return None

    file_path = data.get("result", {}).get("file_path")
    if not file_path:
        log.error("getFile не вернул file_path для file_id=%s", file_id)
        return None

    return f"{TELEGRAM_API}/file/bot{token}/{file_path}"


async def download_telegram_file(file_id: str) -> tuple[bytes, str] | None:
    """Скачивает файл из Telegram по file_id.

    Возвращает (bytes, filename) или None при ошибке.
    """
    url = await get_telegram_file_url(file_id)
    if not url:
        return None

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            filename = url.rsplit("/", 1)[-1]
            return resp.content, filename
    except httpx.HTTPStatusError as exc:
        log.error("Ошибка скачивания файла Telegram: %s %s", exc.response.status_code, exc.response.text)
    except httpx.RequestError as exc:
        log.error("Ошибка сети при скачивании файла Telegram: %s", exc)

    return None


async def set_telegram_webhook(webhook_url: str) -> dict | None:
    """
    Обновляет webhook бота на новый публичный URL.
    """
    payload = {
        "url": webhook_url,
        "allowed_updates": ["message", "edited_message"],
        "drop_pending_updates": False,
    }
    response = await _telegram_api_post("setWebhook", payload)
    if response is None:
        return None

    log.info("Telegram webhook обновлён: %s", webhook_url)
    log.info("Telegram setWebhook response: %s", response)
    return response
