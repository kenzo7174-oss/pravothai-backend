"""Мониторинг занятого места в базе данных PostgreSQL."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import DATABASE_URL, is_postgres
from app.models import Client, Conversation, Message

NEON_LIMIT_BYTES = 524288000       # 500 МБ
AIVEN_LIMIT_BYTES = 5368709120     # 5 ГБ
DEFAULT_POSTGRES_LIMIT_BYTES = NEON_LIMIT_BYTES

WARNING_THRESHOLD_PERCENT = 90.0
CRITICAL_THRESHOLD_PERCENT = 95.0

ALLOWED_CLEANUP_DAYS = frozenset({30, 60, 90})
ALLOWED_DELETE_MODES = frozenset({"full", "messages_only"})


def detect_database_provider() -> tuple[str, int]:
    """Определяет провайдера БД по DATABASE_URL и возвращает (имя, лимит в байтах)."""
    url_lower = DATABASE_URL.lower()
    if "neon.tech" in url_lower:
        return "Neon", NEON_LIMIT_BYTES
    if "aivencloud.com" in url_lower:
        return "Aiven", AIVEN_LIMIT_BYTES
    if is_postgres():
        return "PostgreSQL", DEFAULT_POSTGRES_LIMIT_BYTES
    return "SQLite", NEON_LIMIT_BYTES


async def get_database_size_bytes(session: AsyncSession) -> int:
    """Возвращает текущий размер базы данных в байтах."""
    if is_postgres():
        result = await session.execute(text("SELECT pg_database_size(current_database())"))
        return int(result.scalar_one())

    if "sqlite" in DATABASE_URL:
        url_path = DATABASE_URL.split("///", 1)[-1]
        db_path = Path(url_path)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        if db_path.exists():
            return db_path.stat().st_size
    return 0


def build_storage_status(size_bytes: int) -> dict:
    """Собирает ответ API: размер, лимит, процент и уровень предупреждения."""
    provider_name, limit = detect_database_provider()
    if limit > 0:
        percent = round((size_bytes / limit) * 100, 2)
    else:
        percent = 0.0
    percent = min(percent, 100.0)

    if percent >= CRITICAL_THRESHOLD_PERCENT:
        level = "critical"
    elif percent >= WARNING_THRESHOLD_PERCENT:
        level = "warning"
    else:
        level = "ok"

    return {
        "size_bytes": size_bytes,
        "limit_bytes": limit,
        "usage_percent": percent,
        "level": level,
        "is_postgres": is_postgres(),
        "provider_name": provider_name,
    }


async def get_unique_channels(session: AsyncSession) -> list[str]:
    """Возвращает отсортированный список уникальных каналов (Conversation.source)."""
    result = await session.execute(
        select(Conversation.source)
        .distinct()
        .where(Conversation.source.is_not(None), Conversation.source != "")
        .order_by(Conversation.source)
    )
    return [row[0] for row in result.all()]


async def _get_inactive_conversations(
    session: AsyncSession,
    days: int,
    channels: list[str],
) -> list[Conversation]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    last_msg_sq = (
        select(
            Message.conversation_id.label("conv_id"),
            func.max(Message.created_at).label("last_msg_at"),
        )
        .group_by(Message.conversation_id)
        .subquery()
    )

    result = await session.execute(
        select(Conversation)
        .outerjoin(last_msg_sq, Conversation.id == last_msg_sq.c.conv_id)
        .where(
            Conversation.source.in_(channels),
            func.coalesce(Conversation.last_interaction_at, last_msg_sq.c.last_msg_at) < cutoff,
        )
    )
    return result.scalars().all()


async def cleanup_old_conversations(
    session: AsyncSession,
    days: int,
    channels: list[str],
    delete_mode: str,
) -> int:
    """Удаляет неактивные диалоги или только их сообщения по выбранным каналам."""
    if days not in ALLOWED_CLEANUP_DAYS:
        raise ValueError(f"Недопустимый период: {days}")
    if delete_mode not in ALLOWED_DELETE_MODES:
        raise ValueError(f"Недопустимый режим удаления: {delete_mode}")

    cleaned_channels = [item.strip() for item in channels if item and item.strip()]
    if not cleaned_channels:
        raise ValueError("Необходимо выбрать хотя бы один канал")

    conversations = await _get_inactive_conversations(session, days, cleaned_channels)
    if not conversations:
        return 0

    if delete_mode == "messages_only":
        deleted_count = 0
        for conv in conversations:
            messages_result = await session.execute(
                select(Message).where(Message.conversation_id == conv.id)
            )
            messages = messages_result.scalars().all()
            deleted_count += len(messages)
            for message in messages:
                await session.delete(message)
            conv.pending_draft = ""
            conv.pending_draft_message_id = None
            conv.last_ai_handled_message_id = None
            conv.last_interaction_at = None
        await session.commit()
        return deleted_count

    deleted_count = len(conversations)
    client_ids_affected = {conv.client_id for conv in conversations}
    for conv in conversations:
        await session.delete(conv)

    await session.flush()

    for client_id in client_ids_affected:
        remaining = await session.execute(
            select(Conversation.id).where(Conversation.client_id == client_id).limit(1)
        )
        if remaining.scalar_one_or_none() is None:
            client = await session.get(Client, client_id)
            if client:
                await session.delete(client)

    await session.commit()
    return deleted_count
