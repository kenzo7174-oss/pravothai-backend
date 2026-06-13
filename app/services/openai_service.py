"""
Универсальный сервис генерации ответов через OpenAI.

Режим выбирается по переменным окружения:
- OPENAI_ASSISTANT_ID задан → Assistants API (client.beta.assistants...)
- OPENAI_ASSISTANT_ID не задан → Chat Completions API (client.chat.completions.create)

В режиме Chat Completions системный промпт берётся из OPENAI_SYSTEM_PROMPT
или из DEFAULT_SYSTEM_PROMPT ниже.

Ожидается JSON-ответ вида { "message": "...", "request_operator": false }.
"""

import asyncio
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from openai import AsyncOpenAI, APIError
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import Conversation, Message, SystemSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Таймаут на каждый HTTP-вызов к OpenAI и число ретраев.
# Защищает синхронные пути (например, вебхук виджета) от зависаний на минуты.
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
OPENAI_MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "1"))

_openai_client: "AsyncOpenAI | None" = None
_openai_client_key: str | None = None


def get_openai_client() -> "AsyncOpenAI | None":
    """Возвращает переиспользуемый AsyncOpenAI-клиент с заданным таймаутом.

    Кэшируется по api_key (пересоздаётся при смене ключа). None, если ключ не задан.
    """
    global _openai_client, _openai_client_key
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        return None
    if _openai_client is None or _openai_client_key != api_key:
        _openai_client = AsyncOpenAI(
            api_key=api_key,
            timeout=OPENAI_TIMEOUT_SECONDS,
            max_retries=OPENAI_MAX_RETRIES,
        )
        _openai_client_key = api_key
    return _openai_client


# Фраза перевода — при её появлении в ответе ИИ сервер принудительно блокирует ИИ
TRANSFER_PHRASE_MARKER = "Перевожу"

OFF_HOURS_INSTRUCTION = """Сейчас нерабочее время. ПРАВИЛО: Никак не упоминай рабочее время и отсутствие людей, общайся как обычно. В JSON используй "request_operator": false, ИСКЛЮЧЕНИЕ: если клиент прямо просит оператора/специалиста — отвечай фразой "Перевожу вас на специалиста, одну минуту..." и ставь "request_operator": true."""

DEFAULT_SYSTEM_PROMPT = """БАЗА ЗНАНИЙ: Axoloti Terminal (LiveDesk)
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
        client = get_openai_client()
        if client is None:
            return None
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


RUN_TIMEOUT_SECONDS = 120
RUN_POLL_INTERVAL_SECONDS = 0.5


def _extract_assistant_message_text(message) -> str | None:
    """Извлекает текст из сообщения ассистента в треде."""
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(getattr(block, "text", None), "value", None) or ""
            text = re.sub(r'【[^】]*】', '', text)
            if text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip() or None


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


def _parse_assistant_output(text: str | None, use_json_format: bool = True) -> tuple[str | None, bool]:
    """Парсит текст ответа ассистента и флаг request_operator."""
    if not text:
        return None, False

    if use_json_format:
        message_text, request_operator = _parse_json_response(text)
        if message_text is not None:
            if TRANSFER_PHRASE_MARKER in message_text and "специалиста" in message_text:
                request_operator = True
            return message_text, request_operator
        raw = text.strip()
        force_transfer = TRANSFER_PHRASE_MARKER in raw and "специалиста" in raw
        return raw, force_transfer

    raw = text.strip()
    force_transfer = TRANSFER_PHRASE_MARKER in raw and "специалиста" in raw
    return raw, force_transfer


def _get_assistant_id() -> str | None:
    return settings.OPENAI_ASSISTANT_ID


def _uses_assistants_api() -> bool:
    """True, если задан OPENAI_ASSISTANT_ID — используем Assistants API."""
    return bool(_get_assistant_id())


def _get_system_prompt() -> str:
    """Системный промпт для Chat Completions: env или встроенный по умолчанию."""
    custom = settings.OPENAI_SYSTEM_PROMPT
    if custom and custom.strip():
        return custom.strip()
    return DEFAULT_SYSTEM_PROMPT


def _get_chat_model() -> str:
    return settings.OPENAI_CHAT_MODEL or "gpt-4o"


def _build_chat_messages(
    history: list[tuple[str, str]],
    additional_instructions: str | None = None,
) -> list[dict[str, str]]:
    """Собирает messages для Chat Completions API."""
    system_content = _get_system_prompt()
    if additional_instructions:
        system_content = f"{system_content}\n\n{additional_instructions}"
    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    for role, content in history:
        if (content or "").strip():
            messages.append({"role": role, "content": content.strip()})
    return messages


async def _run_chat_on_history(
    history: list[tuple[str, str]],
    additional_instructions: str | None = None,
) -> str | None:
    """Генерирует ответ через Chat Completions API по истории сообщений."""
    client = get_openai_client()
    if client is None or not history:
        return None

    response = await client.chat.completions.create(
        model=_get_chat_model(),
        messages=_build_chat_messages(history, additional_instructions),
        temperature=0.7,
    )
    return (response.choices[0].message.content or "").strip() or None


async def _generate_chat_reply(
    message: str,
    additional_instructions: str | None = None,
) -> str | None:
    """Одно сообщение через Chat Completions API."""
    text = (message or "").strip()
    if not text:
        return None
    return await _run_chat_on_history(
        [("user", text)],
        additional_instructions=additional_instructions,
    )


async def _wait_for_run(client: AsyncOpenAI, thread_id: str, run_id: str):
    """Ожидает завершения Run в треде Assistants API."""
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while True:
        run = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)
        if run.status in ("completed", "failed", "cancelled", "expired", "incomplete"):
            return run
        if time.monotonic() >= deadline:
            raise TimeoutError(f"OpenAI run {run_id} timed out after {RUN_TIMEOUT_SECONDS}s")
        await asyncio.sleep(RUN_POLL_INTERVAL_SECONDS)


async def _fetch_latest_assistant_reply(client: AsyncOpenAI, thread_id: str) -> str | None:
    """Возвращает текст последнего сообщения ассистента в треде."""
    messages = await client.beta.threads.messages.list(
        thread_id=thread_id,
        order="desc",
        limit=10,
    )
    for message in messages.data:
        if message.role == "assistant":
            return _extract_assistant_message_text(message)
    return None


async def generate_assistant_reply(
    message: str,
    thread_id: str | None = None,
    additional_instructions: str | None = None,
) -> tuple[str | None, str | None]:
    """Генерирует ответ через OpenAI (Assistants API или Chat Completions).

    Assistants API: принимает message и опциональный OpenAI thread_id.
    Если thread_id нет — создаёт новый тред, добавляет сообщение, запускает Run
    и возвращает (текст ответа, thread_id).

    Chat Completions: thread_id не используется, возвращает (текст, None).
    """
    api_key = settings.OPENAI_API_KEY
    text = (message or "").strip()

    if not api_key:
        log.warning("OPENAI_API_KEY не задан — генерация невозможна")
        return None, thread_id
    if not text:
        log.warning("Пустое сообщение для OpenAI")
        return None, thread_id

    if not _uses_assistants_api():
        try:
            reply = await _generate_chat_reply(text, additional_instructions=additional_instructions)
            return reply, None
        except APIError as exc:
            log.error("OpenAI Chat Completions API error: %s", exc)
        except Exception as exc:
            log.error("Непредвиденная ошибка Chat Completions API: %s", exc)
        return None, thread_id

    assistant_id = _get_assistant_id()
    try:
        client = get_openai_client()
        if client is None:
            return None, thread_id

        if thread_id:
            openai_thread_id = thread_id
        else:
            thread = await client.beta.threads.create()
            openai_thread_id = thread.id

        await client.beta.threads.messages.create(
            thread_id=openai_thread_id,
            role="user",
            content=text,
        )

        run_kwargs: dict = {
            "thread_id": openai_thread_id,
            "assistant_id": assistant_id,
        }
        if additional_instructions:
            run_kwargs["additional_instructions"] = additional_instructions

        run = await client.beta.threads.runs.create(**run_kwargs)
        run = await _wait_for_run(client, openai_thread_id, run.id)

        if run.status != "completed":
            log.error(
                "OpenAI Assistants run failed: status=%s error=%s",
                run.status,
                getattr(run, "last_error", None),
            )
            return None, openai_thread_id

        reply = await _fetch_latest_assistant_reply(client, openai_thread_id)
        return reply, openai_thread_id

    except APIError as exc:
        log.error("OpenAI Assistants API error: %s", exc)
    except TimeoutError as exc:
        log.error("%s", exc)
    except Exception as exc:
        log.error("Непредвиденная ошибка Assistants API: %s", exc)

    return None, thread_id


async def _run_assistant_on_history(
    history: list[tuple[str, str]],
    additional_instructions: str | None = None,
) -> str | None:
    """Генерирует сырой ответ по истории сообщений (Assistants API или Chat Completions)."""
    api_key = settings.OPENAI_API_KEY
    if not api_key or not history:
        return None

    if not _uses_assistants_api():
        return await _run_chat_on_history(history, additional_instructions=additional_instructions)

    assistant_id = _get_assistant_id()
    client = get_openai_client()
    if client is None:
        return None
    thread = await client.beta.threads.create()

    for role, content in history:
        if not (content or "").strip():
            continue
        await client.beta.threads.messages.create(
            thread_id=thread.id,
            role=role,
            content=content.strip(),
        )

    run_kwargs: dict = {
        "thread_id": thread.id,
        "assistant_id": assistant_id,
    }
    if additional_instructions:
        run_kwargs["additional_instructions"] = additional_instructions

    run = await client.beta.threads.runs.create(**run_kwargs)
    run = await _wait_for_run(client, thread.id, run.id)

    if run.status != "completed":
        log.error(
            "OpenAI Assistants run failed: status=%s error=%s",
            run.status,
            getattr(run, "last_error", None),
        )
        return None

    return await _fetch_latest_assistant_reply(client, thread.id)


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


# Текст автоответа в нерабочее время (по-человечески, без официоза)
OFFLINE_REPLY_MESSAGE = "Сейчас нас нет на месте, но мы обязательно ответим в рабочее время."


MSK_TZ = timezone(timedelta(hours=3))


def _is_within_business_hours_msk(business_start: str, business_end: str) -> bool:
    """
    Проверяет, попадает ли текущее время (по МСК, UTC+3) в интервал business_start - business_end.
    """
    start_t = _parse_time(business_start)
    end_t = _parse_time(business_end)
    if not start_t or not end_t:
        return True
    now_msk = datetime.now(MSK_TZ).time()
    current_minutes = now_msk.hour * 60 + now_msk.minute
    start_minutes = start_t[0] * 60 + start_t[1]
    end_minutes = end_t[0] * 60 + end_t[1]
    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    return current_minutes >= start_minutes or current_minutes <= end_minutes


async def check_offline_block(
    session: "AsyncSession",
    conv,
) -> str | None:
    """
    Проверка рабочих часов при эскалации на оператора.

    1. Если у диалога уже есть оператор (conv.operator_id), возвращает None.
    2. Берёт из SystemSettings: business_hours_enabled, business_start, business_end.
    3. Если business_hours_enabled == True, проверяет текущее время по МСК (UTC+3).
    4. Если время вне диапазона — возвращает текст автоответа. Иначе None.
    """
    if conv.operator_id is not None:
        return None
    result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
    ss = result.scalar_one_or_none()
    if not ss or not getattr(ss, "business_hours_enabled", False):
        return None
    start = getattr(ss, "business_start", "09:00") or "09:00"
    end = getattr(ss, "business_end", "18:00") or "18:00"
    if _is_within_business_hours_msk(start, end):
        return None
    return OFFLINE_REPLY_MESSAGE


async def generate_draft(
    messages: list[Message],
    session: "AsyncSession | None" = None,
    override_instructions: str | None = None,
) -> tuple[str | None, bool]:
    """Генерирует черновик ответа на основе истории диалога через OpenAI.

    Режим API определяется наличием OPENAI_ASSISTANT_ID в окружении.
    Принимает последние N ORM-объектов Message,
    возвращает (текст сообщения, request_operator) или (None, False) при ошибке.
    override_instructions: при задании подменяет инструкции и отключает request_human_operator.
    """
    if not settings.OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY не задан — генерация невозможна")
        return None, False

    filtered = [m for m in messages if m.sender != "system"]
    if not filtered:
        log.warning("Нет сообщений для ИИ после исключения системных")
        return None, False

    history = [
        (SENDER_TO_ROLE.get(msg.sender, "user"), msg.content)
        for msg in filtered
    ]

    additional_instructions: str | None = None
    use_json_format = True

    if override_instructions:
        additional_instructions = override_instructions
        use_json_format = False
    elif session:
        result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
        sys_settings = result.scalar_one_or_none()
        if sys_settings and getattr(sys_settings, "business_hours_enabled", False):
            start = getattr(sys_settings, "business_start", "09:00") or "09:00"
            end = getattr(sys_settings, "business_end", "18:00") or "18:00"
            if not _is_within_business_hours(start, end):
                additional_instructions = OFF_HOURS_INSTRUCTION

    try:
        raw_reply = await _run_assistant_on_history(
            history,
            additional_instructions=additional_instructions,
        )
        return _parse_assistant_output(raw_reply, use_json_format=use_json_format)

    except APIError as exc:
        api_mode = "Assistants" if _uses_assistants_api() else "Chat Completions"
        log.error("OpenAI %s API error: %s", api_mode, exc)
    except Exception as exc:
        log.error("Непредвиденная ошибка при генерации черновика: %s", exc)

    return None, False


DEFAULT_CONVERSATION_TAGS = frozenset({"web", "telegram"})

AUTO_TAG_SYSTEM_PROMPT = (
    "Проанализируй диалог и выдай строго одно короткое слово-тег — причину, "
    "по которой потребовался человек (например: Оплата, Баг, Доставка, Консультация). "
    "Выведи только одно слово, без точек, кавычек и лишних символов."
)

AUTO_TAG_SENDER_LABELS = {
    "client": "Клиент",
    "support": "Оператор",
    "operator": "Оператор",
    "bot": "ИИ",
    "assistant": "ИИ",
}


def _parse_conversation_tags(tags_str: str) -> list[str]:
    if not tags_str:
        return []
    return [tag.strip() for tag in tags_str.split(",") if tag.strip()]


def _has_meaningful_tag(tags_str: str) -> bool:
    """True, если среди тегов есть не дефолтный (не web/telegram)."""
    for tag in _parse_conversation_tags(tags_str):
        if tag.lower() not in DEFAULT_CONVERSATION_TAGS:
            return True
    return False


def _normalize_auto_tag(raw: str) -> str | None:
    """Приводит ответ ИИ к одному слову-тегу."""
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip().strip('"\'«»“”‘’.')
    word = cleaned.split()[0] if cleaned else ""
    word = re.sub(r"[^\w\-]", "", word, flags=re.UNICODE)
    return word or None


def _format_messages_for_auto_tag(messages: list[Message]) -> str:
    lines = []
    for msg in messages:
        label = AUTO_TAG_SENDER_LABELS.get(msg.sender, msg.sender)
        lines.append(f"{label}: {msg.content}")
    return "\n".join(lines)


async def auto_tag_conversation(conversation_id: int) -> None:
    """Фоновое авто-тегирование диалога при перехвате оператором."""
    try:
        async with AsyncSessionLocal() as session:
            conv = await session.get(Conversation, conversation_id)
            if not conv:
                log.warning("auto_tag: диалог %s не найден", conversation_id)
                return

            if _has_meaningful_tag(conv.tags):
                return

            result = await session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.is_internal.is_(False),
                )
                .order_by(Message.created_at.desc())
                .limit(10)
            )
            recent_messages = list(reversed(result.scalars().all()))
            if not recent_messages:
                log.info("auto_tag: нет сообщений для диалога %s", conversation_id)
                return

            dialog_text = _format_messages_for_auto_tag(recent_messages)
            if not dialog_text.strip():
                return

            client = get_openai_client()
            if client is None:
                log.warning("OPENAI_API_KEY не задан — авто-тегирование пропущено")
                return

            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": AUTO_TAG_SYSTEM_PROMPT},
                    {"role": "user", "content": dialog_text},
                ],
                max_tokens=16,
                temperature=0.2,
            )
            raw_tag = (response.choices[0].message.content or "").strip()
            new_tag = _normalize_auto_tag(raw_tag)
            if not new_tag:
                log.warning(
                    "auto_tag: пустой тег от ИИ для диалога %s (raw=%r)",
                    conversation_id,
                    raw_tag,
                )
                return

            existing_tags = _parse_conversation_tags(conv.tags)
            if any(tag.lower() == new_tag.lower() for tag in existing_tags):
                return

            conv.tags = f"{conv.tags}, {new_tag}" if conv.tags else new_tag
            await session.commit()
            log.info("auto_tag: диалог %s помечен тегом %r", conversation_id, new_tag)

    except APIError as exc:
        log.error("auto_tag: OpenAI API error для диалога %s: %s", conversation_id, exc)
    except Exception as exc:
        log.error("auto_tag: ошибка для диалога %s: %s", conversation_id, exc)
