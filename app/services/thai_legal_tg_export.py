# === DO NOT DELETE: THAI LEGAL BOT EXPORT FEATURE ===
"""
Фоновый экспорт завершённых диалогов веб-виджета в Telegram (Thai Legal Bot).

Управляется через .env:
  ENABLE_TELEGRAM_EXPORT=false (по умолчанию)
  TELEGRAM_TOKEN=
  TELEGRAM_CHAT_ID=
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import Client, Conversation, Message

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
EXPORT_LOOP_INTERVAL_SECONDS = 90
EXPORT_IDLE_MINUTES = 10
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
WEB_SOURCES = ("web", "web_widget")
BOT_SENDERS = frozenset({"bot", "assistant", "operator"})


def _export_enabled() -> bool:
    return bool(
        settings.ENABLE_TELEGRAM_EXPORT
        and settings.TELEGRAM_TOKEN
        and settings.TELEGRAM_CHAT_ID
    )


def _display_value(value) -> str:
    if value is None:
        return "Неизвестно"
    text = str(value).strip()
    return text if text else "-"


def _visitor_id(client: Client, conversation: Conversation) -> str:
    return _display_value(client.axolotl_visitor_id or conversation.social_id)


def _format_message_block(message: Message) -> str | None:
    if message.is_internal:
        return None
    if message.sender == "client":
        return f"👤 Клиент:\n{message.content.strip()}\n"
    if message.sender in BOT_SENDERS:
        return f"🤖 Бот:\n{message.content.strip()}\n"
    return None


def _collect_export_blocks(messages: list[Message]) -> tuple[list[str], list[Message]]:
    blocks: list[str] = []
    exported_messages: list[Message] = []
    for message in messages:
        block = _format_message_block(message)
        if block:
            blocks.append(block)
            exported_messages.append(message)
    return blocks, exported_messages


def _build_export_text(client: Client, conversation: Conversation, messages: list[Message]) -> tuple[str | None, list[Message]]:
    blocks, exported_messages = _collect_export_blocks(messages)
    if not blocks:
        return None, []

    header = (
        "💬 НОВЫЙ ДИАЛОГ\n"
        f"🆔 {_visitor_id(client, conversation)}\n"
        f"📍 Локация: {_display_value(client.location)}\n"
        f"🌐 IP: {_display_value(client.ip)}\n"
        f"💻 Устройство: {_display_value(client.os_device)} / {_display_value(client.browser)}\n"
        "➖➖➖➖➖➖➖\n\n"
    )
    body = "".join(blocks)
    footer = "➖➖➖➖➖➖➖"
    return f"{header}{body}\n{footer}", exported_messages


def _split_telegram_text(text: str) -> list[str]:
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, TELEGRAM_MAX_MESSAGE_LENGTH)
        if split_at <= 0:
            split_at = TELEGRAM_MAX_MESSAGE_LENGTH
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


async def _send_to_telegram(text: str) -> bool:
    url = f"{TELEGRAM_API}/bot{settings.TELEGRAM_TOKEN}/sendMessage"
    payload_base = {"chat_id": settings.TELEGRAM_CHAT_ID}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for chunk in _split_telegram_text(text):
                resp = await client.post(url, json={**payload_base, "text": chunk})
                if resp.status_code != 200:
                    log.warning(
                        "Thai Legal TG export: Telegram API returned %s: %s",
                        resp.status_code,
                        resp.text,
                    )
                    return False
        return True
    except httpx.RequestError as exc:
        log.error("Thai Legal TG export: network error: %s", exc)
        return False


async def _export_pending_conversations() -> None:
    if not _export_enabled():
        return

    now = datetime.now(timezone.utc)
    cutoff_naive = (now - timedelta(minutes=EXPORT_IDLE_MINUTES)).replace(tzinfo=None)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Conversation)
            .where(
                Conversation.source.in_(WEB_SOURCES),
                Conversation.last_interaction_at.isnot(None),
                Conversation.last_interaction_at < cutoff_naive,
            )
            .options(
                selectinload(Conversation.client),
                selectinload(Conversation.messages),
            )
        )
        conversations = list(result.scalars().unique().all())

        for conv in conversations:
            try:
                client = conv.client
                if client is None:
                    continue

                unexported = [
                    msg
                    for msg in sorted(conv.messages, key=lambda m: m.created_at or datetime.min)
                    if not getattr(msg, "is_exported_to_tg", False)
                ]
                if not unexported:
                    continue

                export_text, exported_messages = _build_export_text(client, conv, unexported)
                if not export_text or not exported_messages:
                    continue

                sent = await _send_to_telegram(export_text)
                if not sent:
                    continue

                for msg in exported_messages:
                    msg.is_exported_to_tg = True
                await session.commit()
                log.info(
                    "Thai Legal TG export: conv_id=%s messages=%s",
                    conv.id,
                    len(exported_messages),
                )
            except Exception as exc:
                log.error("Thai Legal TG export error for conv %s: %s", conv.id, exc)
                await session.rollback()


async def thai_legal_tg_export_loop() -> None:
    """Периодически отправляет завершённые диалоги веб-виджета в Telegram."""
    while True:
        await asyncio.sleep(EXPORT_LOOP_INTERVAL_SECONDS)
        try:
            await _export_pending_conversations()
        except Exception as exc:
            log.error("Thai Legal TG export loop error: %s", exc)
# === DO NOT DELETE: THAI LEGAL BOT EXPORT FEATURE ===
