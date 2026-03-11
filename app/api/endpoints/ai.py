"""
Axoloti Terminal — AI endpoints (summaries, etc.).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI, APIError
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.auth import get_current_operator
from app.core.database import get_session
from app.models import Client, Conversation, Message, Operator
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])

SENDER_TO_LABEL = {
    "client": "Клиент",
    "support": "Оператор",
    "operator": "Оператор",
    "bot": "ИИ",
    "assistant": "ИИ",
}

SYSTEM_PROMPT = (
    "Ты — ассистент. Кратко суммируй диалог в 2–3 предложениях. "
    "Отвечай на русском языке."
)


def _format_messages_for_prompt(messages: list[Message]) -> str:
    lines = []
    for msg in messages:
        label = SENDER_TO_LABEL.get(msg.sender, msg.sender)
        lines.append(f"{label}: {msg.content}")
    return "\n".join(lines) if lines else ""


@router.post("/summary/{client_id}")
async def generate_client_summary(
    client_id: int,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """
    Генерирует AI-резюме всех диалогов клиента.
    Требует OPENAI_API_KEY. При его отсутствии возвращает 503.
    """
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="AI disabled",
        )

    client = await session.get(
        Client,
        client_id,
        options=[selectinload(Client.conversations).selectinload(Conversation.messages)],
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    all_messages: list[Message] = []
    for conv in client.conversations:
        all_messages.extend(conv.messages)
    all_messages.sort(key=lambda m: m.created_at)

    if not all_messages:
        return {"summary": "Диалогов пока нет."}

    prompt = _format_messages_for_prompt(all_messages)

    try:
        openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        summary = response.choices[0].message.content.strip()
        return {"summary": summary}
    except APIError as exc:
        log.error("OpenAI API error: %s", exc)
        raise HTTPException(status_code=502, detail="AI service error")
    except Exception as exc:
        log.exception("Unexpected error generating summary: %s", exc)
        raise HTTPException(status_code=500, detail="Internal error")
