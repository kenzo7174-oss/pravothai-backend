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
# Telegram request URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_API = "https://api.telegram.org"
EXPORT_IDLE_MINUTES = 10
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
WEB_SOURCES = ("web", "web_widget")
BOT_SENDERS = frozenset({"bot", "assistant", "operator"})

# Словарь для хранения таймеров в памяти: {conversation_id: task}
active_timers = {}
_delivery_lock = asyncio.Lock()


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
                if resp.status_code != 200 or resp.json().get("ok") is not True:
                    log.warning(
                        "Thai Legal TG export: Telegram API returned %s",
                        resp.status_code,
                    )
                    return False
        return True
    except (httpx.RequestError, ValueError) as exc:
        log.error("Thai Legal TG export: request failed (%s)", type(exc).__name__)
        return False


async def send_contact_notification(message_id: int) -> None:
    """Send a saved contact request; failed deliveries remain pending in the CRM."""
    if not settings.TELEGRAM_TOKEN or not settings.TELEGRAM_CHAT_ID:
        log.warning("Contact notification: Telegram settings are missing")
        return
    # ponytail: one process serializes sends; use a DB outbox when running multiple replicas.
    async with _delivery_lock:
        try:
            async with AsyncSessionLocal() as session:
                message = await session.get(Message, message_id)
                if message is None or message.is_exported_to_tg:
                    return
                text = f"📩 Новая заявка с pravothai.org\nДиалог CRM: {message.conversation_id}\n\n{message.content}"
                if await _send_to_telegram(text):
                    message.is_exported_to_tg = True
                    await session.commit()
                else:
                    log.warning("Contact notification pending: message_id=%s", message_id)
        except Exception as exc:
            log.error("Contact notification failed: message_id=%s type=%s", message_id, type(exc).__name__)


async def _retry_pending_contacts() -> None:
    if not settings.TELEGRAM_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return
    async with AsyncSessionLocal() as session:
        ids = list((await session.scalars(select(Message.id).join(Conversation).where(
            Conversation.source.in_(WEB_SOURCES), Message.sender == "client",
            Message.content.startswith("Заявка на связь\nИмя:"),
            Message.is_exported_to_tg.is_(False),
        ).order_by(Message.id))).all())
    for message_id in ids:
        await send_contact_notification(message_id)


async def _process_single_conversation(session, conv: Conversation) -> None:
    """Вспомогательная функция для выгрузки одного диалога"""
    try:
        client = conv.client
        if client is None:
            return

        unexported = [
            msg
            for msg in sorted(conv.messages, key=lambda m: m.created_at or datetime.min)
            if not getattr(msg, "is_exported_to_tg", False)
        ]
        if not unexported:
            return

        export_text, exported_messages = _build_export_text(client, conv, unexported)
        if not export_text or not exported_messages:
            return

        sent = await _send_to_telegram(export_text)
        if not sent:
            return

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


async def _export_pending_conversations() -> None:
    """Retry saved dialogues after the idle period, including lost timers."""
    if not _export_enabled():
        return

    now = datetime.now(timezone.utc)
    cutoff_naive = (now - timedelta(minutes=EXPORT_IDLE_MINUTES)).replace(tzinfo=None)

    async with _delivery_lock:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Conversation)
                .where(
                    Conversation.source.in_(WEB_SOURCES),
                    Conversation.last_interaction_at.isnot(None),
                    Conversation.last_interaction_at < cutoff_naive,
                    Conversation.messages.any(Message.is_exported_to_tg.is_(False)),
                )
                .options(
                    selectinload(Conversation.client),
                    selectinload(Conversation.messages),
                )
            )
            conversations = list(result.scalars().unique().all())

            for conv in conversations:
                await _process_single_conversation(session, conv)


async def _wait_and_export(conv_id: int):
    """Спит 10 минут в памяти, затем выгружает конкретный диалог."""
    try:
        await asyncio.sleep(EXPORT_IDLE_MINUTES * 60)
        
        async with _delivery_lock:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Conversation)
                    .where(Conversation.id == conv_id)
                    .options(
                        selectinload(Conversation.client),
                        selectinload(Conversation.messages),
                    )
                )
                conv = result.scalar_one_or_none()
                if conv:
                    await _process_single_conversation(session, conv)
    except asyncio.CancelledError:
        pass  # Таймер отменили новым сообщением
    finally:
        # A cancelled old timer must not remove the replacement timer.
        if active_timers.get(conv_id) is asyncio.current_task():
            active_timers.pop(conv_id, None)


def restart_export_timer(conv_id: int):
    """Запускает или обновляет таймер диалога. База в этот момент спит."""
    if not _export_enabled():
        return
        
    if conv_id in active_timers:
        active_timers[conv_id].cancel()
        
    active_timers[conv_id] = asyncio.create_task(_wait_and_export(conv_id))


async def thai_legal_tg_export_loop() -> None:
    """Recover lost timers and retry failed deliveries from saved messages."""
    log.info("Thai Legal TG export worker: enabled=%s", _export_enabled())
    while True:
        try:
            await _retry_pending_contacts()
            await _export_pending_conversations()
        except Exception as exc:
            log.error("Thai Legal TG export retry failed: type=%s", type(exc).__name__)
        await asyncio.sleep(60)
# === DO NOT DELETE: THAI LEGAL BOT EXPORT FEATURE ===
