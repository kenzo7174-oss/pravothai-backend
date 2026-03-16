"""
Сервис генерации черновиков через OpenAI Responses API.

Использует кастомный промпт (OPENAI_RESPONSE_ID) как базу знаний и Function Calling
для передачи диалога оператору.
"""

import io
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from openai import AsyncOpenAI, APIError
from sqlalchemy import select

from app.core.config import settings
from app.models import Message, SystemSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

OFF_HOURS_INSTRUCTION = """CRITICAL: Сейчас нерабочее время. Вызывать request_human_operator ЗАПРЕЩЕНО. ПРАВИЛО: Никак не упоминай рабочее время и отсутствие людей, общайся как обычно. ЕСЛИ клиент прямо просит позвать оператора/человека — только тогда (и ни в коем случае не раньше) коротко в 1 предложение извинись, скажи, что сейчас операторов нет, и предложи свою помощь."""

# Инструкции для модели. База знаний настраивается в промпте OPENAI_RESPONSE_ID.
TECHNICAL_INSTRUCTIONS = """Ты — ассистент поддержки. Отвечай на основе базы знаний из промпта.
Если клиент просит позвать оператора, человека или специалиста — вызови инструмент request_human_operator.
Также вызывай его, если не можешь решить проблему пользователя.

CRITICAL: ЗАПРЕЩЕНО вызывать функцию `request_human_operator` в ответ на базовые приветствия ('привет', 'здравствуйте') или общие вопросы. Вызов разрешен ТОЛЬКО при прямом требовании позвать человека или полном отсутствии ответа в базе знаний."""

# Инструмент для передачи диалога оператору (Function Calling)
REQUEST_HUMAN_OPERATOR_TOOL = {
    "type": "function",
    "name": "request_human_operator",
    "description": "Вызывать строго при просьбе позвать оператора, человека, специалиста, или если ИИ не может решить проблему пользователя.",
}

SENDER_TO_ROLE = {
    "client": "user",
    "support": "assistant",
    "assistant": "assistant",
    "bot": "assistant",
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


def _parse_response_output(response) -> tuple[str | None, bool]:
    """Извлекает текст ответа и флаг request_operator из output (включая tool calls)."""
    message_text: str | None = None
    request_operator = False

    for item in response.output:
        if getattr(item, "type", None) == "function_call":
            if getattr(item, "name", None) == "request_human_operator":
                request_operator = True
        elif getattr(item, "type", None) == "message":
            for content in getattr(item, "content", []):
                if getattr(content, "type", None) == "output_text":
                    text = getattr(content, "text", None) or ""
                    if text.strip():
                        message_text = (message_text or "") + text

    # Fallback на output_text property если output пустой
    if message_text is None and response.output_text:
        message_text = response.output_text.strip() or None

    # При вызове request_human_operator без текста — используем фразу по умолчанию
    if request_operator and not (message_text and message_text.strip()):
        message_text = "Перевожу диалог на специалиста, пожалуйста, ожидайте."

    return message_text, request_operator


def _parse_time(s: str) -> tuple[int, int] | None:
    """Парсит 'HH:MM' в (час, минута)."""
    if not s or not isinstance(s, str):
        return None
    parts = s.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return (h, m)
    except ValueError:
        pass
    return None


def _is_within_business_hours(
    business_start: str,
    business_end: str,
) -> bool:
    """Проверяет, попадает ли текущее время сервера в интервал business_start - business_end."""
    start_t = _parse_time(business_start)
    end_t = _parse_time(business_end)
    if not start_t or not end_t:
        return True
    now = datetime.now()
    current_minutes = now.hour * 60 + now.minute
    start_minutes = start_t[0] * 60 + start_t[1]
    end_minutes = end_t[0] * 60 + end_t[1]
    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    return current_minutes >= start_minutes or current_minutes <= end_minutes


async def generate_draft(
    messages: list[Message],
    session: "AsyncSession | None" = None,
    override_instructions: str | None = None,
) -> tuple[str | None, bool]:
    """Генерирует черновик ответа на основе истории диалога через OpenAI Responses API.

    Принимает последние N ORM-объектов Message,
    возвращает (текст сообщения, request_operator) или (None, False) при ошибке.
    override_instructions: при задании подменяет инструкции и отключает request_human_operator.
    """
    api_key = settings.OPENAI_API_KEY
    prompt_id = settings.OPENAI_RESPONSE_ID
    if not api_key:
        log.warning("OPENAI_API_KEY не задан — генерация невозможна")
        return None, False
    if not prompt_id:
        log.warning("OPENAI_RESPONSE_ID / OPENAI_ASSISTANT_ID не задан — генерация невозможна")
        return None, False

    input_items = [
        {"role": SENDER_TO_ROLE.get(msg.sender, "user"), "content": msg.content}
        for msg in messages
    ]

    instructions = TECHNICAL_INSTRUCTIONS
    tools = [REQUEST_HUMAN_OPERATOR_TOOL]

    if override_instructions:
        instructions = override_instructions
        tools = []
    elif session:
        result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
        sys_settings = result.scalar_one_or_none()
        if sys_settings and getattr(sys_settings, "business_hours_enabled", False):
            start = getattr(sys_settings, "business_start", "09:00") or "09:00"
            end = getattr(sys_settings, "business_end", "18:00") or "18:00"
            if not _is_within_business_hours(start, end):
                instructions = TECHNICAL_INSTRUCTIONS + "\n\n" + OFF_HOURS_INSTRUCTION

    create_params: dict = {
        "prompt": {"id": prompt_id},
        "instructions": instructions,
        "input": input_items,
        "tools": tools,
        "store": False,
    }

    try:
        client = AsyncOpenAI(api_key=api_key)
        response = await client.responses.create(**create_params)

        message_text, request_operator = _parse_response_output(response)
        if not tools:
            request_operator = False
        return message_text, request_operator

    except APIError as exc:
        log.error("OpenAI Responses API error: %s", exc)
    except Exception as exc:
        log.error("Непредвиденная ошибка при генерации черновика: %s", exc)

    return None, False
