"""
Axoloti Terminal — Сервис генерации черновиков через OpenAI API.
"""

import io
import logging

from openai import AsyncOpenAI, APIError

from app.core.config import settings
from app.models import Message

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — вежливый ассистент службы поддержки. "
    "Твоя задача — написать краткий, грамотный черновик ответа "
    "на последнее сообщение клиента. "
    "Отвечай по-русски. Не используй markdown-разметку. "
    "Пиши только текст ответа, без пояснений."
)

SENDER_TO_ROLE = {
    "client": "user",
    "support": "assistant",
    "assistant": "assistant",
}


async def transcribe_voice(audio_data: bytes, filename: str = "voice.ogg") -> str | None:
    """Транскрибирует аудиофайл через OpenAI Whisper API (модель whisper-1)."""
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        log.warning("OPENAI_API_KEY не задан — транскрипция невозможна")
        return None

    try:
        client = AsyncOpenAI(api_key=api_key)
        audio_file = io.BytesIO(audio_data)
        audio_file.name = filename

        response = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru",
        )
        text = response.text.strip()
        if text:
            log.info("Whisper транскрипция (%s): %d символов", filename, len(text))
            return text

        log.warning("Whisper вернул пустой текст для %s", filename)
        return None
    except APIError as exc:
        log.error("OpenAI Whisper API error: %s", exc)
    except Exception as exc:
        log.error("Непредвиденная ошибка при транскрипции: %s", exc)

    return None


async def generate_draft(messages: list[Message]) -> str | None:
    """Генерирует черновик ответа на основе истории диалога.

    Принимает последние N ORM-объектов Message,
    возвращает текст черновика или None при ошибке.
    """
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        log.warning("OPENAI_API_KEY не задан — генерация невозможна")
        return None

    openai_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in messages:
        role = SENDER_TO_ROLE.get(msg.sender, "user")
        openai_messages.append({"role": role, "content": msg.content})

    try:
        client = AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=openai_messages,
            max_tokens=300,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except APIError as exc:
        log.error("OpenAI API error: %s", exc)
    except Exception as exc:
        log.error("Непредвиденная ошибка при генерации черновика: %s", exc)

    return None
