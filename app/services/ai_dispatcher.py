"""
Сервис фоновой обработки входящих сообщений клиента.

В зависимости от режима диалога:
- bot: генерирует ответ ИИ, сохраняет его в БД и отправляет клиенту;
- prompter: генерирует черновик и сохраняет его в conversation.pending_draft.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.sse import sse_manager
from app.models import Conversation, Message
from app.services.openai_service import generate_draft
from app.services.telegram import send_telegram_message

log = logging.getLogger(__name__)

# Режимы перехвата (импортируются в main.py)
INTERCEPT_MODE_BOT = "bot"
INTERCEPT_MODE_PROMPTER = "prompter"
INTERCEPT_MODE_MANUAL = "manual"
INTERCEPT_MODE_SENIOR = "senior"

VALID_INTERCEPT_MODES = frozenset({
    INTERCEPT_MODE_BOT,
    INTERCEPT_MODE_PROMPTER,
    INTERCEPT_MODE_MANUAL,
    INTERCEPT_MODE_SENIOR,
})

# Русские названия режимов для системных сообщений (строчные)
INTERCEPT_MODE_RU = {
    INTERCEPT_MODE_PROMPTER: "суфлер",
    INTERCEPT_MODE_MANUAL: "полный контроль",
    INTERCEPT_MODE_SENIOR: "старший специалист",
    "full_control": "полный контроль",
}


def get_intercept_mode_label_ru(mode: str) -> str:
    """Возвращает русское название режима перехвата для отображения в системных сообщениях."""
    return INTERCEPT_MODE_RU.get(mode, mode)

# Короткая фраза перевода на специалиста (жёстко блокирует overtalking ИИ)
AI_TRANSFER_PHRASE = "Перевожу вас на специалиста, одну минуту..."


async def process_incoming_client_message(
    conversation_id: int,
    message_id: int,
) -> None:
    """Запускает ИИ-обработку нового клиентского сообщения в фоне."""
    async with AsyncSessionLocal() as session:
        conv = await session.get(Conversation, conversation_id)
        msg = await session.get(Message, message_id)
        # ЖЁСТКАЯ ПРОВЕРКА: ИИ реагирует ТОЛЬКО на сообщения клиентов
        if not conv or not msg or msg.sender not in ("client",):
            return

        mode = conv.intercept_mode or INTERCEPT_MODE_BOT
        if mode not in {INTERCEPT_MODE_BOT, INTERCEPT_MODE_PROMPTER}:
            return

        if conv.last_ai_handled_message_id == message_id:
            return

        result = await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(10)
        )
        recent_messages = list(reversed(result.scalars().all()))
        if not recent_messages:
            return

        draft, request_operator = await generate_draft(recent_messages, session=session)
        if not draft and not request_operator:
            log.warning(
                "Не удалось сгенерировать ИИ-ответ для conversation_id=%s, mode=%s",
                conversation_id,
                mode,
            )
            return

        # При вызове специалиста: СТРОГО короткая фраза, блокировка ИИ, статус "Требует внимания"
        # Гарантия: права ИИ отзываются сразу — даже если он «захочет» написать ещё, доступ к чату уже закрыт
        if request_operator:
            content_to_send = AI_TRANSFER_PHRASE
            conv.intercept_mode = INTERCEPT_MODE_MANUAL  # is_ai_active = false
            conv.specialist_requested = True
            conv.specialist_requested_at = datetime.utcnow()
            # Внутреннее системное сообщение (как при перехвате оператором)
            internal_msg = Message(
                conversation_id=conversation_id,
                content=f"Система — перевёл на специалиста по запросу клиента. Режим: {get_intercept_mode_label_ru(INTERCEPT_MODE_MANUAL)}",
                sender="system",
                is_read=False,
                is_voice=False,
                is_internal=True,
            )
            session.add(internal_msg)
        else:
            content_to_send = draft

        # В режиме prompter: сгенерированный текст ТОЛЬКО в pending_draft, никогда как готовое сообщение в чат.
        # СТРОГО прерываем выполнение — ИИ не сохраняет черновик как сообщение в БД.
        if mode == INTERCEPT_MODE_PROMPTER and not request_operator:
            conv.pending_draft = draft
            conv.pending_draft_message_id = message_id
            conv.last_ai_handled_message_id = message_id
            await session.commit()
            await sse_manager.broadcast("chat_updated", {"conversation_id": conversation_id})
            return

        ai_message = Message(
            conversation_id=conversation_id,
            content=content_to_send,
            sender="assistant",
            is_read=False,
        )
        session.add(ai_message)
        conv.pending_draft = ""
        conv.pending_draft_message_id = None
        conv.last_ai_handled_message_id = message_id
        conv.last_interaction_at = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            await session.commit()
            await session.refresh(ai_message)
            if conv.source == "telegram" and conv.social_id:
                ok = await send_telegram_message(conv.social_id, content_to_send)
                if not ok:
                    log.warning(
                        "ИИ-ответ сохранён, но не отправлен в Telegram (conv=%s, social_id=%s)",
                        conversation_id,
                        conv.social_id,
                    )
        except Exception as e:
            log.error("Ошибка диспетчера: %s", e)
        finally:
            if request_operator:
                await sse_manager.broadcast("chat_updated", {"conversation_id": conversation_id})
