"""
Сервис генерации черновиков через OpenAI Responses API.

Использует системный промпт (роль продажника Axoloti) и ожидает JSON-ответ
вида { "message": "...", "request_operator": false }.
"""

import io
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

from openai import AsyncOpenAI, APIError
from sqlalchemy import select

from app.core.config import settings
from app.models import Message, SystemSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Фраза перевода — при её появлении в ответе ИИ сервер принудительно блокирует ИИ
TRANSFER_PHRASE_MARKER = "Перевожу"

OFF_HOURS_INSTRUCTION = """Сейчас нерабочее время. ПРАВИЛО: Никак не упоминай рабочее время и отсутствие людей, общайся как обычно. В JSON используй "request_operator": false, ИСКЛЮЧЕНИЕ: если клиент прямо просит оператора/специалиста — отвечай фразой "Перевожу вас на специалиста, одну минуту..." и ставь "request_operator": true."""

SYSTEM_PROMPT = """БАЗА ЗНАНИЙ: Axoloti Terminal (LiveDesk)
РОЛЬ И ЦЕЛЬ: Ты — проактивный ИИ-менеджер по продажам платформы Axoloti (робот-аксолотль). Твоя цель: продавать внедрение умного чата и переводить горячих лидов на оператора.
СТРОГОЕ ПРАВИЛО: Ты продаешь IT-продукт. Никогда не предлагай помощь с документами или возвратами.

ПРИОРИТЕТ №1 — ПЕРЕВОД НА ЧЕЛОВЕКА:
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО переводить на оператора при оскорблениях, матах или агрессии (например, «лох», «дурак»). Перевод возможен ТОЛЬКО при прямой, явной просьбе позвать человека. На оскорбления продолжай диалог по скрипту или отвечай нейтрально.

Твой высший приоритет — перевод на человека по первому требованию. Если клиент выражает желание пообщаться с оператором, специалистом, менеджером или «живым человеком» (включая сленг «кожаный»):
1. ТВОЙ ОТВЕТ ДОЛЖЕН СОСТОЯТЬ ТОЛЬКО ИЗ ОДНОЙ ФРАЗЫ: «Перевожу вас на специалиста, одну минуту...».
2. ТЫ ЗАПРЕЩАЕШЬ СЕБЕ задавать уточняющие вопросы или продолжать квалификацию лида после этой фразы.
3. Считай это приоритетной командой, отменяющей все остальные цели (продажи, сбор данных).

Примеры запросов, при которых ОБЯЗАТЕЛЬНО переводи: «позови человека», «дай оператора», «хочу со специалистом», «кожаного мне», «менеджера позови», «соедини с кем-то живым». НЕ срабатывай на случайные упоминания слов — только когда клиент ЯВНО просит связать его с человеком.

1. О КОМПАНИИ: Axoloti — платформа для умной автоматизации поддержки малого бизнеса. ИИ отвечает на 80% вопросов, сложные передает оператору. Функции: ИИ-автопилот, Суфлёр, Саммари, CRM-блок.
2. ЦЕНЫ: AI START (29 900 ₽), AI BUSINESS (54 900 ₽), Premium (89 900 ₽). Индивидуально от 19 900 ₽. Подключение: 1-7 дней.
3. ОБЩЕНИЕ: На «Вы», продающий стиль, СТРОГО 2-4 предложения. В конце задавай вопрос о бизнесе клиента.
4. ЭСКАЛАЦИЯ: Переводи на человека при «возврат средств», «жалоба», «суд», агрессия — или при любом явном запросе оператора/специалиста/менеджера.
5. ФОРМАТ ОТВЕТА (КРИТИЧЕСКИ ВАЖНО): Твой ответ ВСЕГДА строго в JSON без markdown-разметки: { "message": "Твой ответ", "request_operator": false }. Ставь true только при явном запросе клиента на перевод на человека."""

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


def _extract_text_from_response(response) -> str | None:
    """Извлекает сырой текст ответа из output."""
    message_text: str | None = None
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) == "message":
            for content in getattr(item, "content", []):
                if getattr(content, "type", None) == "output_text":
                    text = getattr(content, "text", None) or ""
                    if text.strip():
                        message_text = (message_text or "") + text
    if message_text is None and getattr(response, "output_text", None):
        message_text = response.output_text.strip() or None
    return message_text


def _parse_json_response(text: str) -> tuple[str | None, bool]:
    """Парсит JSON-ответ вида { "message": "...", "request_operator": false }."""
    if not text or not text.strip():
        return None, False
    raw = text.strip()
    # Убираем markdown-обёртки ```json ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            msg = data.get("message")
            req_op = data.get("request_operator", False)
            if msg is not None and isinstance(msg, str) and msg.strip():
                return msg.strip(), bool(req_op)
            if msg is not None:
                return str(msg).strip() or None, bool(req_op)
    except (json.JSONDecodeError, TypeError):
        pass
    return None, False


def _parse_response_output(response, use_json_format: bool = True) -> tuple[str | None, bool]:
    """Извлекает текст ответа и флаг request_operator. Поддерживает JSON-формат.
    Гарантия: если в ответе есть фраза перевода на специалиста — принудительно request_operator=True."""
    text = _extract_text_from_response(response)
    if not text:
        return None, False

    if use_json_format:
        message_text, request_operator = _parse_json_response(text)
        if message_text is not None:
            # Гарантия: ИИ сказал «Перевожу... специалиста» — блокируем, даже если забыл request_operator
            if TRANSFER_PHRASE_MARKER in message_text and "специалиста" in message_text:
                request_operator = True
            return message_text, request_operator
        # Fallback: если JSON не распарсился, используем весь текст как сообщение
        raw = text.strip()
        force_transfer = TRANSFER_PHRASE_MARKER in raw and "специалиста" in raw
        return raw, force_transfer

    raw = text.strip()
    force_transfer = TRANSFER_PHRASE_MARKER in raw and "специалиста" in raw
    return raw, force_transfer


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


OFFLINE_REPLY_MESSAGE = "В данный момент операторов нет на месте, мы ответим вам в рабочее время."


async def check_offline_block(
    session: "AsyncSession",
    conv,
) -> str | None:
    """
    Проверка рабочих часов. Первый фильтр для входящих сообщений.
    Если нерабочее время и оператор не назначен — возвращает текст автоответа.
    Иначе None (обрабатывать как обычно).
    Исключение: если operator_id задан — диалог ведёт оператор, блокировку не применяем.
    """
    if conv.operator_id is not None:
        return None
    result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
    ss = result.scalar_one_or_none()
    if not ss or not getattr(ss, "business_hours_enabled", False):
        return None
    start = getattr(ss, "business_start", "09:00") or "09:00"
    end = getattr(ss, "business_end", "18:00") or "18:00"
    if _is_within_business_hours(start, end):
        return None
    return OFFLINE_REPLY_MESSAGE


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

    # ИИ реагирует ТОЛЬКО на сообщения клиента/оператора/бота. Системные — исключаем.
    filtered = [m for m in messages if m.sender != "system"]
    if not filtered:
        log.warning("Нет сообщений для ИИ после исключения системных")
        return None, False
    input_items = [
        {"role": SENDER_TO_ROLE.get(msg.sender, "user"), "content": msg.content}
        for msg in filtered
    ]

    instructions = SYSTEM_PROMPT
    use_json_format = True

    if override_instructions:
        instructions = override_instructions
        use_json_format = False
    elif session:
        result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
        sys_settings = result.scalar_one_or_none()
        if sys_settings and getattr(sys_settings, "business_hours_enabled", False):
            start = getattr(sys_settings, "business_start", "09:00") or "09:00"
            end = getattr(sys_settings, "business_end", "18:00") or "18:00"
            if not _is_within_business_hours(start, end):
                instructions = SYSTEM_PROMPT + "\n\n" + OFF_HOURS_INSTRUCTION

    create_params: dict = {
        "prompt": {"id": prompt_id},
        "instructions": instructions,
        "input": input_items,
        "tools": [],
        "store": False,
    }

    try:
        client = AsyncOpenAI(api_key=api_key)
        response = await client.responses.create(**create_params)

        message_text, request_operator = _parse_response_output(response, use_json_format=use_json_format)
        return message_text, request_operator

    except APIError as exc:
        log.error("OpenAI Responses API error: %s", exc)
    except Exception as exc:
        log.error("Непредвиденная ошибка при генерации черновика: %s", exc)

    return None, False
