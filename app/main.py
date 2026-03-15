"""
Axoloti Terminal — Главный файл FastAPI-приложения.

Запуск:
    cd backend
    python -m app.main
"""

import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import BackgroundTasks, FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
import uvicorn

from app.core.config import settings
from app.core.database import engine, get_session, is_postgres, Base
from app.core.auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_operator,
)
from app.models import (
    Client,
    Conversation,
    Message,
    Operator,
    SystemSettings,
    DEFAULT_SENIOR_WELCOME_MESSAGE,
)
from app.schemas import (
    ClientMergeRequest,
    ClientSchema,
    ClientUpdate,
    InterceptModeUpdate,
    LoginRequest,
    MessageCreate,
    MessageSchema,
    SystemSettingsSchema,
    SystemSettingsUpdate,
    TokenResponse,
)
from app.services.ai_dispatcher import (
    INTERCEPT_MODE_BOT,
    INTERCEPT_MODE_PROMPTER,
    INTERCEPT_MODE_MANUAL,
    INTERCEPT_MODE_SENIOR,
    process_incoming_client_message,
)
from app.services.telegram import send_telegram_message, download_telegram_file, set_telegram_webhook
from app.services.openai_service import generate_draft, transcribe_voice
from app.api.endpoints import ai as ai_endpoints

log = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


class _SuppressPollingFilter(logging.Filter):
    """Drop INFO access-log records for the high-frequency polling endpoint."""

    _NOISY_FRAGMENTS = ("GET /api/v1/clients ", "GET /api/v1/health ", "GET /api/v1/webhooks/web/")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.INFO:
            return True
        msg = record.getMessage()
        return not any(frag in msg for frag in self._NOISY_FRAGMENTS)


logging.getLogger("uvicorn.access").addFilter(_SuppressPollingFilter())


def _extract_contacts_from_text(text: str) -> dict[str, str]:
    """Извлекает email, телефон и соцссылки из текста сообщения. Возвращает dict с ключами email, phone, social_link."""
    result: dict[str, str] = {"email": "", "phone": "", "social_link": ""}
    if not text or not isinstance(text, str):
        return result

    # Email: стандартный паттерн
    email_match = re.search(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        text,
    )
    if email_match:
        result["email"] = email_match.group(0).strip()

    # Телефон: с плюсом и без (7-15 цифр, возможны пробелы, скобки, дефисы)
    phone_match = re.search(
        r"(?:\+?\d[\d\s\-()]{8,20}\d|\d{10,15})",
        text,
    )
    if phone_match:
        raw = re.sub(r"[\s\-()]", "", phone_match.group(0))
        if len(raw) >= 10:
            result["phone"] = ("+" + raw) if not raw.startswith("+") else raw

    # Соцсети и сайты: vk.com, t.me, instagram.com или личные домены (http/https)
    # Negative lookbehind (?<![@\w.]) — не захватывать домены, являющиеся частью email
    # (исключаем позиции сразу после @, букв и точек — т.е. внутри local@domain)
    social_pattern = (
        r"(?:https?://)?(?<![@\w.])(?:"
        r"(?:vk\.com/[\w.]+)|"
        r"(?:t\.me/[\w]+)|"
        r"(?:instagram\.com/[\w.]+)|"
        r"(?:www\.)?[a-zA-Z0-9][-a-zA-Z0-9.]*\.[a-zA-Z]{2,}(?:/[^\s]*)?"
        r")"
    )
    social_match = re.search(social_pattern, text, re.IGNORECASE)
    if social_match:
        link = social_match.group(0).strip()
        if not link.startswith("http"):
            link = "https://" + link
        result["social_link"] = link

    return result


async def _apply_contacts_and_auto_merge(
    session: AsyncSession,
    client: Client,
    message_text: str,
) -> Client:
    """
    Извлекает контакты из текста, сохраняет в профиль клиента.
    Если найден другой клиент с таким же email или phone — склеивает диалоги
    (перепривязывает к старому клиенту, удаляет дубль).
    Возвращает итогового клиента (либо текущего, либо того, с кем склеили).
    """
    contacts = _extract_contacts_from_text(message_text)
    if contacts.get("email") and not client.email:
        client.email = contacts["email"]
    if contacts.get("phone") and not client.phone:
        client.phone = contacts["phone"]
    if contacts.get("social_link") and not client.social_link:
        client.social_link = contacts["social_link"]
    await session.flush()

    # Авто-склейка: ищем другого клиента с тем же email или phone
    other_client = None
    if client.email:
        r = await session.execute(
            select(Client).where(
                Client.email == client.email,
                Client.id != client.id,
            )
        )
        other_client = r.scalar_one_or_none()
    if other_client is None and client.phone:
        r = await session.execute(
            select(Client).where(
                Client.phone == client.phone,
                Client.id != client.id,
            )
        )
        other_client = r.scalar_one_or_none()

    if other_client is None:
        return client

    # Найден старый клиент — перепривязываем все диалоги и заметки (безопасный паттерн как в /merge)
    source_client = client
    target_client = other_client

    # 1. СНАЧАЛА перепривязываем все диалоги source → target
    r = await session.execute(
        select(Conversation).where(Conversation.client_id == source_client.id)
    )
    for conv in r.scalars().all():
        conv.client_id = target_client.id

    # 2. Перепривязываем заметки (склеиваем в target)
    if source_client.notes and source_client.notes.strip():
        existing = (target_client.notes or "").strip()
        target_client.notes = (
            (existing + "\n\n---\n" + source_client.notes) if existing else source_client.notes
        )

    # 3. Промежуточный flush — БД должна зафиксировать перепривязку ДО удаления
    await session.flush()

    # 4. Сбрасываем кэш relationship, чтобы при delete не каскадировало на уже перепривязанные диалоги
    session.expire(source_client, ["conversations"])

    # 5. ТОЛЬКО после успешного flush — удаляем source
    await session.delete(source_client)

    log.info(
        "AUTO-MERGE SUCCESS: client %s merged into %s via contact match",
        source_client.id,
        target_client.id,
    )
    return target_client


async def _get_table_columns(conn, table: str) -> set[str]:
    """Возвращает множество имён колонок таблицы (SQLite или PostgreSQL)."""
    if is_postgres():
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :tname"
            ),
            {"tname": table},
        )
        return {row[0] for row in result.fetchall()}
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {row[1] for row in result.fetchall()}


async def ensure_conversation_runtime_columns() -> None:
    """Лёгкая runtime-миграция для новых полей conversation и messages."""
    async with engine.begin() as conn:
        existing_columns = await _get_table_columns(conn, "conversations")

        if "intercept_mode" not in existing_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN intercept_mode VARCHAR(30) NOT NULL DEFAULT 'bot'"
            ))
        if "pending_draft" not in existing_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN pending_draft TEXT NOT NULL DEFAULT ''"
            ))
        if "pending_draft_message_id" not in existing_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN pending_draft_message_id INTEGER"
            ))
        if "last_ai_handled_message_id" not in existing_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN last_ai_handled_message_id INTEGER"
            ))
        if "specialist_requested" not in existing_columns:
            default_val = "0" if not is_postgres() else "false"
            await conn.execute(text(
                f"ALTER TABLE conversations ADD COLUMN specialist_requested BOOLEAN NOT NULL DEFAULT {default_val}"
            ))

        msg_columns = await _get_table_columns(conn, "messages")

        if "is_voice" not in msg_columns:
            default_val = "0" if not is_postgres() else "false"
            await conn.execute(text(
                f"ALTER TABLE messages ADD COLUMN is_voice BOOLEAN NOT NULL DEFAULT {default_val}"
            ))

        # clients.notes (для заметок оператора)
        client_columns = await _get_table_columns(conn, "clients")
        if "notes" not in client_columns:
            await conn.execute(text(
                "ALTER TABLE clients ADD COLUMN notes TEXT NOT NULL DEFAULT ''"
            ))
        if "axolotl_visitor_id" not in client_columns:
            await conn.execute(text(
                "ALTER TABLE clients ADD COLUMN axolotl_visitor_id VARCHAR(64) NOT NULL DEFAULT ''"
            ))
        if "browser" not in client_columns:
            await conn.execute(text(
                "ALTER TABLE clients ADD COLUMN browser VARCHAR(64) NOT NULL DEFAULT ''"
            ))
        if "os_device" not in client_columns:
            await conn.execute(text(
                "ALTER TABLE clients ADD COLUMN os_device VARCHAR(128) NOT NULL DEFAULT ''"
            ))
        if "ip" not in client_columns:
            await conn.execute(text(
                "ALTER TABLE clients ADD COLUMN ip VARCHAR(45) NOT NULL DEFAULT ''"
            ))
        if "social_link" not in client_columns:
            await conn.execute(text(
                "ALTER TABLE clients ADD COLUMN social_link VARCHAR(500) NOT NULL DEFAULT ''"
            ))


async def register_telegram_webhook_on_startup() -> None:
    """Если заданы TELEGRAM_BOT_TOKEN и WEBHOOK_DOMAIN — регистрирует webhook в Telegram API."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_WEBHOOK_PUBLIC_URL:
        return
    webhook_url = f"{settings.TELEGRAM_WEBHOOK_PUBLIC_URL.rstrip('/')}{settings.TELEGRAM_WEBHOOK_PATH}"
    response = await set_telegram_webhook(webhook_url)
    if response and response.get("ok"):
        log.info("Telegram webhook зарегистрирован: %s", webhook_url)
    else:
        log.warning("Не удалось зарегистрировать Telegram webhook: %s", response)


async def seed_default_operator() -> None:
    """Create a default operator if the operators table is empty."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Operator).limit(1))
        if result.scalar_one_or_none() is None:
            username = settings.DEFAULT_ADMIN_USER
            password = settings.DEFAULT_ADMIN_PASSWORD
            session.add(Operator(
                username=username,
                hashed_password=hash_password(password),
            ))
            await session.commit()
            log.info("Seeded default operator: %s", username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_conversation_runtime_columns()
    await seed_default_operator()
    await register_telegram_webhook_on_startup()
    log.info("FastAPI startup complete")
    yield


# ── FastAPI приложение ────────────────────────────────────────────────────
app = FastAPI(
    title="Axoloti Terminal API",
    version="0.2.0",
    description="Омниканальная CRM для поддержки клиентов",
    lifespan=lifespan,
    strict_slashes=False,
)

# ── CORS (сразу после app, выше маршрутов) ─────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(ai_endpoints.router, prefix="/api/v1")

# ── Корневой эндпоинт ────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Axoloti Terminal Engine",
        "version": "0.2.0",
    }


@app.get("/api/v1/health")
async def healthcheck():
    return {"ok": True, "status": "online"}


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Аутентификация
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
):
    """Аутентификация оператора — возвращает подписанный JWT."""
    result = await session.execute(
        select(Operator).where(Operator.username == body.username)
    )
    operator = result.scalar_one_or_none()

    if operator is None or not verify_password(body.password, operator.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Неверное имя пользователя или пароль",
        )

    token = create_access_token(subject=operator.username)
    return TokenResponse(access_token=token)


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Системные настройки (Singleton id=1)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/v1/settings", response_model=SystemSettingsSchema)
async def get_settings(
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Возвращает текущие системные настройки. Если записи нет — создаёт дефолтную (id=1)."""
    result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = SystemSettings(
            id=1,
            senior_welcome_message=DEFAULT_SENIOR_WELCOME_MESSAGE,
        )
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    return SystemSettingsSchema(
        senior_welcome_message=settings.senior_welcome_message,
    )


@app.patch("/api/v1/settings", response_model=SystemSettingsSchema)
async def patch_settings(
    body: SystemSettingsUpdate,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Обновляет запись системных настроек (id=1)."""
    result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = SystemSettings(
            id=1,
            senior_welcome_message=DEFAULT_SENIOR_WELCOME_MESSAGE,
        )
        session.add(settings)
        await session.flush()
    if body.senior_welcome_message is not None:
        settings.senior_welcome_message = body.senior_welcome_message
    await session.commit()
    await session.refresh(settings)
    return SystemSettingsSchema(
        senior_welcome_message=settings.senior_welcome_message,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Клиенты
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/v1/clients", response_model=list[ClientSchema])
async def get_clients(session: AsyncSession = Depends(get_session)):
    """
    Возвращает список всех клиентов с диалогами и сообщениями.

    Используем selectinload для жадной подгрузки связанных данных
    за минимальное количество SQL-запросов (без N+1 проблемы).
    """
    query = (
        select(Client)
        .options(
            selectinload(Client.conversations)
            .selectinload(Conversation.messages)
        )
    )
    result = await session.execute(query)
    clients = list(result.scalars().all())

    # Сортировка: клиент с самым свежим сообщением — первый.
    # Клиенты без сообщений уходят в конец списка (default=datetime.min).
    clients.sort(
        key=lambda c: max(
            (m.created_at for conv in c.conversations for m in conv.messages),
            default=datetime.min,
        ),
        reverse=True,
    )

    return clients


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Сообщения
# ═══════════════════════════════════════════════════════════════════════════

@app.post(
    "/api/v1/conversations/{conversation_id}/messages",
    response_model=MessageSchema,
    status_code=201,
)
async def create_message(
    conversation_id: int,
    body: MessageCreate,
    session: AsyncSession = Depends(get_session),
):
    """Создаёт новое сообщение в диалоге и сохраняет в БД.

    Если диалог привязан к Telegram — параллельно пересылает текст клиенту.
    """
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.sender != "client":
        conv.pending_draft = ""
        conv.pending_draft_message_id = None
        # Оператор ответил — сбрасываем флаг запроса специалиста
        conv.specialist_requested = False

    # Нормализация sender: support → operator для совместимости с виджетом и схемой
    sender = body.sender
    if sender in ("support", "operator"):
        sender = "operator"
    elif sender in ("assistant", "bot"):
        sender = "bot"

    msg = Message(
        conversation_id=conversation_id,
        content=body.content,
        sender=sender,
        is_read=False,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)

    if conv.source == "telegram" and conv.social_id and sender != "client":
        ok = await send_telegram_message(conv.social_id, body.content)
        if not ok:
            log.warning(
                "Не удалось доставить сообщение в Telegram (conv=%s, social_id=%s)",
                conversation_id,
                conv.social_id,
            )

    return msg


# Ключевые слова для определения запроса клиентом специалиста
_SPECIALIST_REQUEST_PATTERNS = re.compile(
    r"\b(специалист|оператор|менеджер|консультант|человек|живой|реальный|настоящий|хочу\s+поговорить|соедините|позовите|позвать)\b",
    re.IGNORECASE,
)


def _detect_specialist_request(text: str) -> bool:
    """Проверяет, просит ли клиент специалиста/оператора."""
    if not text or not isinstance(text, str):
        return False
    return bool(_SPECIALIST_REQUEST_PATTERNS.search(text.strip()))


def _normalize_intercept_mode(mode: str | None) -> str:
    """Нормализует режим: full_control → manual, невалидные → bot."""
    if not mode:
        return INTERCEPT_MODE_BOT
    normalized = mode.strip().lower()
    if normalized == "full_control":
        return INTERCEPT_MODE_MANUAL
    if normalized in {INTERCEPT_MODE_BOT, INTERCEPT_MODE_PROMPTER, INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_SENIOR}:
        return normalized
    return INTERCEPT_MODE_BOT


OPERATOR_TIMEOUT_MINUTES = 15


async def _check_and_auto_wakeup_manual_mode(
    session: AsyncSession,
    conv: Conversation,
) -> bool:
    """
    Если режим manual/senior и оператор не писал > 15 мин — переключает на bot и возвращает True.
    Иначе возвращает False.
    """
    if conv.intercept_mode not in {INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_SENIOR}:
        return False

    # Ищем последнее сообщение: от оператора ИЛИ внутреннее системное (перехват)
    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conv.id,
            or_(
                Message.sender == "operator",
                and_(Message.sender == "system", Message.is_internal == True),
            ),
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    last_op_msg = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=OPERATOR_TIMEOUT_MINUTES)

    if last_op_msg is None:
        timed_out = True
    else:
        msg_ts = last_op_msg.created_at
        if msg_ts.tzinfo is None:
            msg_ts = msg_ts.replace(tzinfo=timezone.utc)
        timed_out = msg_ts < cutoff

    if not timed_out:
        return False

    conv.intercept_mode = INTERCEPT_MODE_BOT
    await session.commit()
    log.info(
        "AUTO-WAKEUP: Client %s (conv %s) returned to AI mode (operator timeout)",
        conv.client_id,
        conv.id,
    )
    return True


async def _get_senior_welcome_message(session: AsyncSession) -> str:
    """Возвращает приветственное сообщение для режима senior из SystemSettings."""
    result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings:
        return settings.senior_welcome_message
    return DEFAULT_SENIOR_WELCOME_MESSAGE


@app.patch("/api/v1/conversations/{conversation_id}/intercept-mode")
async def update_intercept_mode(
    conversation_id: int,
    body: InterceptModeUpdate,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Обновляет серверный режим перехвата для диалога.

    Поддерживает 4 режима: bot, prompter, manual, senior.
    При переключении на senior — отправляет клиенту системное сообщение (БД + Telegram).
    При переключении на manual — уведомлений нет (бесшовный перехват).
    При перехвате (bot -> manual/prompter/senior) создаёт скрытое системное сообщение (is_internal).
    """
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    prev_mode = conv.intercept_mode or INTERCEPT_MODE_BOT
    new_mode = _normalize_intercept_mode(body.mode)
    conv.intercept_mode = new_mode

    if conv.intercept_mode != INTERCEPT_MODE_PROMPTER:
        conv.pending_draft = ""
        conv.pending_draft_message_id = None

    # Оператор перехватил управление — сбрасываем флаг запроса специалиста
    if new_mode in {INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_SENIOR}:
        conv.specialist_requested = False

    # Скрытое системное сообщение при перехвате (видны только в Терминале)
    if prev_mode == INTERCEPT_MODE_BOT and new_mode in {
        INTERCEPT_MODE_PROMPTER,
        INTERCEPT_MODE_MANUAL,
        INTERCEPT_MODE_SENIOR,
    }:
        device_id = (body.device_id or "").strip() or "—"
        operator_name = (body.operator_name or "").strip() or "Оператор"
        operator_role = (body.operator_role or "").strip() or "Специалист"
        internal_text = (
            f"Оператор {operator_name} ({operator_role}, ID: {device_id}) "
            f"перехватил управление. Режим: {new_mode}"
        )
        internal_msg = Message(
            conversation_id=conv.id,
            content=internal_text,
            sender="system",
            is_read=False,
            is_voice=False,
            is_internal=True,
        )
        session.add(internal_msg)
        await session.flush()

    # При переключении на senior — отправляем уведомление клиенту (из SystemSettings)
    if conv.intercept_mode == INTERCEPT_MODE_SENIOR:
        senior_text = await _get_senior_welcome_message(session)
        sys_msg = Message(
            conversation_id=conv.id,
            content=senior_text,
            sender="system",
            is_read=False,
            is_voice=False,
        )
        session.add(sys_msg)
        await session.flush()

        if conv.source == "telegram" and conv.social_id:
            # Визуально выделяем только эту отбивку: эмодзи + курсив (HTML)
            formatted_text = f"👤 <i>{senior_text}</i>"
            ok = await send_telegram_message(conv.social_id, formatted_text)
            if not ok:
                log.warning(
                    "Уведомление senior сохранено в БД, но не отправлено в Telegram (conv=%s)",
                    conversation_id,
                )

    # manual — без уведомлений (бесшовный перехват)

    await session.commit()

    return {
        "conversation_id": conv.id,
        "intercept_mode": conv.intercept_mode,
        "pending_draft": conv.pending_draft,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Генерация черновика (Суфлёр / OpenAI)
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/conversations/{conversation_id}/generate-draft")
async def generate_draft_endpoint(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Генерирует черновик ответа через OpenAI на основе последних сообщений."""
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conv.pending_draft:
        return {"draft": conv.pending_draft}

    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(10)
    )
    recent = list(reversed(result.scalars().all()))

    if not recent:
        raise HTTPException(status_code=400, detail="No messages in conversation")

    draft, request_operator = await generate_draft(recent)
    if draft is None:
        raise HTTPException(status_code=502, detail="Failed to generate draft")

    conv.pending_draft = draft
    latest_client_msg = next((msg for msg in reversed(recent) if msg.sender == "client"), None)
    conv.pending_draft_message_id = latest_client_msg.id if latest_client_msg else None
    if request_operator:
        conv.specialist_requested = True
    await session.commit()

    return {"draft": draft}


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Telegram Webhook
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """Принимает Webhook-обновления от Telegram Bot API.

    Поток:
      1. Парсим Update → извлекаем chat_id, имя, текст.
      2. Ищем Conversation по social_id == chat_id (source='telegram').
      3. Если не нашли — создаём Client + Conversation.
      4. Сохраняем входящее сообщение (sender='client').
    """
    raw_body = await request.body()
    log.info("⚡ INCOMING TELEGRAM WEBHOOK: %s", raw_body.decode("utf-8", errors="replace"))

    try:
        update = await request.json()
    except Exception as e:
        print(f"ERROR telegram_webhook JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    tg_message = update.get("message") or update.get("edited_message")
    if not tg_message:
        return {"ok": True}

    text = tg_message.get("text")
    is_voice = False

    # Голосовое или аудио-сообщение → скачиваем и расшифровываем через Whisper
    voice_obj = tg_message.get("voice") or tg_message.get("audio")
    if not text and voice_obj:
        file_id = voice_obj.get("file_id")
        if file_id:
            downloaded = await download_telegram_file(file_id)
            if downloaded:
                audio_data, filename = downloaded
                transcription = await transcribe_voice(audio_data, filename)
                if transcription:
                    text = transcription
                    is_voice = True
                else:
                    text = "[Голосовое сообщение — не удалось расшифровать]"
                    is_voice = True
            else:
                text = "[Голосовое сообщение — не удалось скачать]"
                is_voice = True
        else:
            text = "[Голосовое сообщение]"
            is_voice = True

    if not text:
        return {"ok": True}

    chat = tg_message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    first_name = (
        tg_message.get("from", {}).get("first_name")
        or chat.get("first_name")
        or "Telegram User"
    )
    last_name = (
        tg_message.get("from", {}).get("last_name")
        or chat.get("last_name")
        or ""
    )
    full_name = f"{first_name} {last_name}".strip()
    username = tg_message.get("from", {}).get("username", "")

    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing chat.id")

    # ── Ищем существующий диалог по social_id ────────────────────────────
    result = await session.execute(
        select(Conversation).where(
            Conversation.source == "telegram",
            Conversation.social_id == chat_id,
        )
    )
    conv = result.scalar_one_or_none()

    if conv is None:
        # ── Новый клиент + диалог ────────────────────────────────────────
        client = Client(
            name=full_name,
            avatar="",
            phone="",
            email="",
            social_link="",
            website=f"@{username}" if username else "",
            notes="",
            tags="telegram",
        )
        session.add(client)
        await session.flush()

        conv = Conversation(
            client_id=client.id,
            source="telegram",
            social_id=chat_id,
            label=f"Telegram: {full_name}",
        )
        session.add(conv)
        await session.flush()
    else:
        client = await session.get(Client, conv.client_id)

    # ── Сохраняем входящее сообщение ─────────────────────────────────────
    msg = Message(
        conversation_id=conv.id,
        content=text,
        sender="client",
        is_read=False,
        is_voice=is_voice,
    )
    session.add(msg)
    await session.flush()

    # Извлечение контактов и авто-склейка с дублем по email/phone
    if client:
        await _apply_contacts_and_auto_merge(session, client, text)

    # Клиент просит специалиста — устанавливаем флаг для уведомления оператора
    if _detect_specialist_request(text) and conv.intercept_mode in {
        INTERCEPT_MODE_BOT,
        INTERCEPT_MODE_PROMPTER,
    }:
        conv.specialist_requested = True

    await session.commit()
    await session.refresh(msg)

    if conv.intercept_mode in {INTERCEPT_MODE_BOT, INTERCEPT_MODE_PROMPTER}:
        background_tasks.add_task(process_incoming_client_message, conv.id, msg.id)
    elif conv.intercept_mode in {INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_SENIOR}:
        did_wakeup = await _check_and_auto_wakeup_manual_mode(session, conv)
        if did_wakeup:
            background_tasks.add_task(process_incoming_client_message, conv.id, msg.id)

    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Web Widget Webhook
# ═══════════════════════════════════════════════════════════════════════════


def _get_client_ip(request: Request) -> str:
    """Извлекает реальный IP клиента. Учитывает Render: X-Forwarded-For, X-Real-IP."""
    headers = {
        "X-Forwarded-For": request.headers.get("X-Forwarded-For"),
        "X-Real-IP": request.headers.get("X-Real-IP"),
    }
    print(f"DEBUG IP: {headers}")
    forwarded = headers["X-Forwarded-For"]
    if forwarded and forwarded.strip():
        # Первый адрес в списке — клиент, остальные — прокси
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real_ip = headers["X-Real-IP"]
    if real_ip and real_ip.strip():
        return real_ip.strip()
    if request.client:
        return request.client.host or ""
    return ""


async def _fetch_geolocation(ip: str) -> str:
    """Получает геолокацию по IP через ip-api.com. При fail/ошибке — «Сеть клиента»."""
    if not ip or ip.startswith("127.") or ip == "::1":
        return "Локальная сеть"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"http://ip-api.com/json/{ip}?lang=ru",
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "success":
                return "Сеть клиента"
            city = data.get("city") or ""
            country = data.get("country") or ""
            parts = [p for p in (city, country) if p]
            return ", ".join(parts) if parts else "Сеть клиента"
    except Exception as exc:
        log.warning("GeoIP запрос не удался для %s: %s", ip, exc)
        return "Сеть клиента"


def _parse_user_agent(ua: str | None) -> tuple[str, str]:
    """Парсит userAgent в (os_device, browser). Возвращает понятные строки."""
    if not ua or not isinstance(ua, str):
        return ("", "")
    ua = ua.strip()
    os_device = ""
    browser = ""
    # OS/Device
    if "iPhone" in ua:
        os_device = "iPhone (iOS)"
    elif "iPad" in ua:
        os_device = "iPad (iOS)"
    elif "iPod" in ua:
        os_device = "iPod (iOS)"
    elif "Android" in ua:
        os_device = "Android"
    elif "Mac" in ua or "Macintosh" in ua:
        os_device = "Mac (macOS)"
    elif "Windows" in ua or "Win" in ua:
        os_device = "Windows PC"
    elif "Linux" in ua:
        os_device = "Linux"
    else:
        os_device = "Unknown"
    # Browser (порядок важен: Edge до Chrome, Yandex до Chrome)
    if "Edg/" in ua:
        browser = "Edge"
    elif "YaBrowser/" in ua or "Yandex" in ua:
        browser = "Yandex"
    elif "Chrome/" in ua and "Edg" not in ua:
        browser = "Chrome"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Safari/" in ua and "Chrome" not in ua:
        browser = "Safari"
    elif "Opera" in ua or "OPR/" in ua:
        browser = "Opera"
    else:
        browser = "Other"
    return (os_device, browser)


def _parse_thread_id_int(value) -> int | None:
    """Безопасное извлечение integer из thread_id (например, 'conv-123' или '123')."""
    if value is None:
        return None
    s = str(value).strip().replace("conv-", "").replace("conv_", "").strip()
    if not s or not s.isdigit():
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


async def _resolve_web_conversation(session, thread_id_raw: str | None):
    """Ищет Conversation по thread_id (conv-123 или social_id). Возвращает (conv, None) или (None, HTTPException)."""
    if not thread_id_raw:
        return None, HTTPException(status_code=400, detail="thread_id is required")
    conv_id = _parse_thread_id_int(thread_id_raw)
    conv = None
    if conv_id is not None:
        conv = await session.get(Conversation, conv_id)
    if conv is None:
        result = await session.execute(
            select(Conversation).where(
                Conversation.source == "web",
                Conversation.social_id == str(thread_id_raw),
            )
        )
        conv = result.scalar_one_or_none()
    if conv is None:
        return None, HTTPException(status_code=404, detail="Conversation not found")
    return conv, None


def _parse_operator_from_internal_message(text: str) -> dict | None:
    """Извлекает имя и должность из текста внутреннего сообщения перехвата.

    Формат: "Оператор {name} ({role}, ID: ...) перехватил управление. Режим: ..."
    """
    if not text or not isinstance(text, str):
        return None
    m = re.match(r"Оператор (.+?) \((.+?), ID:", text)
    if m:
        return {"name": m.group(1).strip(), "role": m.group(2).strip()}
    return None


@app.get("/api/v1/webhooks/web/{thread_id}")
async def web_widget_poll_messages(
    thread_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Поллинговый эндпоинт для веб-виджета: возвращает сообщения и данные оператора.

    Формат: {"messages": [...], "current_operator": {"name": "...", "role": "..."} | null}
    """
    conv, err = await _resolve_web_conversation(session, thread_id)
    if err is not None:
        raise err

    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conv.id,
            or_(Message.is_internal == False, Message.is_internal.is_(None)),
        )
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()
    messages_data = [{"id": m.id, "sender": m.sender, "content": m.content} for m in messages]

    current_operator = None
    if conv.intercept_mode in {INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_SENIOR}:
        internal_result = await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conv.id,
                Message.sender == "system",
                Message.is_internal == True,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        last_internal = internal_result.scalar_one_or_none()
        if last_internal and last_internal.content:
            current_operator = _parse_operator_from_internal_message(last_internal.content)

    return {"messages": messages_data, "current_operator": current_operator}


@app.post("/api/v1/webhooks/web")
async def web_widget_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Принимает сообщения с веб-виджета на сайте.

    JSON: { "message": str, "thread_id"?: str }
    Возвращает: { "reply": str, "thread_id": str }
    """
    try:
        body = await request.json()
    except Exception as e:
        print(f"ERROR web_widget_webhook JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    text = (body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is required")

    visitor_id = (body.get("client_id") or "").strip()
    browser = (body.get("browser") or "").strip()
    os_device = (body.get("os_device") or "").strip()
    user_agent = body.get("user_agent") or body.get("userAgent") or ""

    if not browser and not os_device and user_agent:
        os_device, browser = _parse_user_agent(user_agent)

    # IP и геолокация
    client_ip = _get_client_ip(request)
    location = ""
    if client_ip:
        location = await _fetch_geolocation(client_ip)

    # ЖЕСТКОЕ ПРАВИЛО: сначала ищем клиента по axolotl_visitor_id, НЕ по id
    client = None
    if visitor_id:
        result = await session.execute(
            select(Client).where(Client.axolotl_visitor_id == visitor_id)
        )
        client = result.scalar_one_or_none()

    if client is not None:
        # Клиент найден — обновляем техданные, НЕ создаём нового
        if client_ip:
            client.ip = client_ip
        if location:
            client.location = location
        if browser:
            client.browser = browser
        if os_device:
            client.os_device = os_device
        await session.flush()
    else:
        # Клиент НЕ найден — создаём нового. Имя по умолчанию: «Посетитель (Ижевск)» или «Посетитель #ID»
        default_name = f"Посетитель ({location})" if location else "Посетитель сайта"
        client = Client(
            name=default_name,
            avatar="",
            phone="",
            email="",
            social_link="",
            website="",
            notes="",
            tags="web",
            axolotl_visitor_id=visitor_id or "",
            ip=client_ip or "",
            location=location,
            browser=browser,
            os_device=os_device,
        )
        session.add(client)
        await session.flush()
        if client.name == "Посетитель сайта":
            client.name = f"Посетитель #{client.id}"
            await session.flush()

    # Теперь ищем диалог для этого клиента
    thread_id_raw = body.get("thread_id")
    conv = None

    if thread_id_raw:
        conv_id = _parse_thread_id_int(thread_id_raw)
        if conv_id is not None:
            conv = await session.get(Conversation, conv_id)
        if conv is None:
            result = await session.execute(
                select(Conversation).where(
                    Conversation.source == "web",
                    Conversation.social_id == str(thread_id_raw),
                )
            )
            conv = result.scalar_one_or_none()

        # thread_id должен указывать на диалог нашего клиента, иначе — игнорируем
        if conv is not None and conv.client_id != client.id:
            conv = None

    if conv is None:
        result = await session.execute(
            select(Conversation).where(
                Conversation.source == "web",
                Conversation.client_id == client.id,
            )
        )
        conv = result.scalar_one_or_none()

    if conv is None:
        social_id = visitor_id or str(uuid.uuid4())
        conv = Conversation(
            client_id=client.id,
            source="web",
            social_id=social_id,
            label="Web: Посетитель сайта",
        )
        session.add(conv)
        await session.flush()

    msg = Message(
        conversation_id=conv.id,
        content=text,
        sender="client",
        is_read=False,
        is_voice=False,
    )
    session.add(msg)
    await session.flush()

    # Извлечение контактов и авто-склейка с дублем по email/phone
    client = await session.get(Client, conv.client_id)
    if client:
        client = await _apply_contacts_and_auto_merge(session, client, text)

    # Клиент просит специалиста — устанавливаем флаг для уведомления оператора
    if _detect_specialist_request(text) and conv.intercept_mode in {
        INTERCEPT_MODE_BOT,
        INTERCEPT_MODE_PROMPTER,
    }:
        conv.specialist_requested = True

    await session.commit()
    await session.refresh(msg)

    if conv.intercept_mode in {INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_SENIOR}:
        did_wakeup = await _check_and_auto_wakeup_manual_mode(session, conv)
        if not did_wakeup:
            return {
                "reply": "Ожидайте ответа оператора.",
                "thread_id": f"conv-{conv.id}",
            }

    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc())
        .limit(10)
    )
    recent = list(reversed(result.scalars().all()))
    draft, request_operator = await generate_draft(recent)

    if request_operator:
        conv.specialist_requested = True

    if draft:
        ai_msg = Message(
            conversation_id=conv.id,
            content=draft,
            sender="bot",
            is_read=False,
            is_voice=False,
        )
        session.add(ai_msg)
        await session.commit()
        await session.refresh(ai_msg)
    elif request_operator:
        await session.commit()

    return {
        "reply": draft or "Извините, не удалось сформировать ответ. Попробуйте позже.",
        "thread_id": f"conv-{conv.id}",
    }


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Обновление / удаление
# ═══════════════════════════════════════════════════════════════════════════

@app.patch("/api/v1/clients/{client_id}", response_model=ClientSchema)
async def update_client(
    client_id: int,
    body: ClientUpdate,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Частичное обновление карточки клиента (теги, имя, контакты и т.д.)."""
    client = await session.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(client, field, value)

    await session.commit()
    await session.refresh(client, attribute_names=["conversations"])
    return client


@app.post("/api/v1/clients/{client_id}/merge", response_model=ClientSchema)
async def merge_client(
    client_id: int,
    body: ClientMergeRequest,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Объединяет клиента source с target: перепривязывает все диалоги и заметки, удаляет source."""
    source = await session.get(Client, client_id)
    if not source:
        raise HTTPException(status_code=404, detail="Client not found")
    target = await session.get(Client, body.target_client_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target client not found")
    if source.id == target.id:
        raise HTTPException(status_code=400, detail="Cannot merge client with itself")

    # 1. СНАЧАЛА перепривязываем все диалоги source → target
    r = await session.execute(select(Conversation).where(Conversation.client_id == source.id))
    conversations = r.scalars().all()
    for conv in conversations:
        conv.client_id = target.id

    log.info(
        "merge_client: source=%s → target=%s, conversations=%s",
        client_id,
        body.target_client_id,
        len(conversations),
    )

    # 2. Перепривязываем заметки (склеиваем в target)
    if source.notes and source.notes.strip():
        existing = (target.notes or "").strip()
        target.notes = (existing + "\n\n---\n" + source.notes) if existing else source.notes

    # 3. Промежуточный flush — БД должна зафиксировать перепривязку ДО удаления
    await session.flush()

    # 4. Сбрасываем кэш relationship, чтобы при delete не каскадировало на уже перепривязанные диалоги
    session.expire(source, ["conversations"])

    # 5. ТОЛЬКО после успешного flush — удаляем source и коммитим
    await session.delete(source)
    await session.commit()
    await session.refresh(target, attribute_names=["conversations"])
    return target


@app.delete("/api/v1/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: int,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Удаляет диалог и все его сообщения. Если у клиента не осталось
    диалогов — удаляет и самого клиента."""
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    client_id = conv.client_id
    await session.delete(conv)
    await session.flush()

    remaining = await session.execute(
        select(Conversation).where(Conversation.client_id == client_id)
    )
    if not remaining.scalars().first():
        client = await session.get(Client, client_id)
        if client:
            await session.delete(client)

    await session.commit()


@app.post("/api/v1/conversations/{conversation_id}/detach", status_code=200)
async def detach_conversation(
    conversation_id: int,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Отвязывает диалог от текущего клиента: создаёт нового пустого клиента
    и перепривязывает conversation к нему. Диалог становится отдельным чатом."""
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    new_client = Client(
        name="Посетитель сайта",
        avatar="",
        phone="",
        email="",
        social_link="",
        website="",
        notes="",
        tags="",
        axolotl_visitor_id="",
        ip="",
        browser="",
        os_device="",
    )
    session.add(new_client)
    await session.flush()

    conv.client_id = new_client.id
    if new_client.name == "Посетитель сайта":
        new_client.name = f"Посетитель #{new_client.id}"
        await session.flush()

    await session.commit()

    return {"conversation_id": conv.id, "new_client_id": new_client.id}


# ── Запуск ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
