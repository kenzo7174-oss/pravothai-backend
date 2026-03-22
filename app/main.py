"""
Axoloti Terminal — Главный файл FastAPI-приложения.

Запуск:
    cd backend
    python -m app.main
"""

import asyncio
import logging
import random
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import BackgroundTasks, FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
import uvicorn

from app.core.config import settings
from app.core.database import engine, get_session, is_postgres, Base, AsyncSessionLocal
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
    ScheduledBroadcast,
    SystemSettings,
    DEFAULT_SENIOR_WELCOME_MESSAGE,
)
from app.schemas import (
    BroadcastRequest,
    ScheduledBroadcastSchema,
    ClientMergeRequest,
    ClientSchema,
    ClientUpdate,
    ConversationUpdate,
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
    get_intercept_mode_label_ru,
    process_incoming_client_message,
)
from app.services.telegram import send_telegram_message, download_telegram_file, set_telegram_webhook
from app.services.openai_service import check_offline_block, generate_draft, transcribe_voice
from app.api.endpoints import ai as ai_endpoints
from app.core.sse import sse_manager
from jose import JWTError, jwt

log = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


class _SuppressPollingFilter(logging.Filter):
    """Drop INFO access-log records for the high-frequency polling endpoint."""

    _NOISY_FRAGMENTS = ("GET /api/v1/clients ", "GET /api/v1/health ", "GET /api/v1/webhooks/web/", "GET /api/v1/events/stream")

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
        if "specialist_requested_at" not in existing_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN specialist_requested_at TIMESTAMP"
            ))
        if "tags" not in existing_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN tags VARCHAR(500) NOT NULL DEFAULT ''"
            ))
        if "last_interaction_at" not in existing_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN last_interaction_at TIMESTAMP"
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
        if "original_name" not in client_columns:
            await conn.execute(text(
                "ALTER TABLE clients ADD COLUMN original_name VARCHAR(120) NOT NULL DEFAULT ''"
            ))

        conv_columns = await _get_table_columns(conn, "conversations")
        if "original_name" not in conv_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN original_name VARCHAR(120) NOT NULL DEFAULT ''"
            ))
        if "has_new_contact" not in conv_columns:
            default_bool = "0" if not is_postgres() else "false"
            await conn.execute(text(
                f"ALTER TABLE conversations ADD COLUMN has_new_contact BOOLEAN NOT NULL DEFAULT {default_bool}"
            ))
        if "operator_id" not in conv_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN operator_id INTEGER"
            ))
        if "operator_name" not in conv_columns:
            await conn.execute(text(
                "ALTER TABLE conversations ADD COLUMN operator_name VARCHAR(120) NOT NULL DEFAULT ''"
            ))

        # system_settings (рабочие часы, SLA)
        ss_columns = await _get_table_columns(conn, "system_settings")
        if "business_hours_enabled" not in ss_columns:
            default_bool = "0" if not is_postgres() else "false"
            await conn.execute(text(
                f"ALTER TABLE system_settings ADD COLUMN business_hours_enabled BOOLEAN NOT NULL DEFAULT {default_bool}"
            ))
        if "business_start" not in ss_columns:
            await conn.execute(text(
                "ALTER TABLE system_settings ADD COLUMN business_start VARCHAR(10) NOT NULL DEFAULT '09:00'"
            ))
        if "business_end" not in ss_columns:
            await conn.execute(text(
                "ALTER TABLE system_settings ADD COLUMN business_end VARCHAR(10) NOT NULL DEFAULT '18:00'"
            ))
        if "operator_sla_minutes" not in ss_columns:
            await conn.execute(text(
                "ALTER TABLE system_settings ADD COLUMN operator_sla_minutes INTEGER NOT NULL DEFAULT 5"
            ))


async def sla_monitor_loop() -> None:
    """Фоновый монитор: автовозврат ИИ при SLA-таймауте (оператор не ответил)."""
    while True:
        await asyncio.sleep(60)
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
                sys_settings = result.scalar_one_or_none()
                sla_minutes = getattr(sys_settings, "operator_sla_minutes", 5) if sys_settings else 5
                sla_minutes = max(1, min(60, int(sla_minutes)))

                now = datetime.now(timezone.utc)
                cutoff_naive = (now - timedelta(minutes=sla_minutes)).replace(tzinfo=None)

                r = await session.execute(
                    select(Conversation)
                    .where(
                        Conversation.specialist_requested == True,
                        Conversation.specialist_requested_at.isnot(None),
                        Conversation.specialist_requested_at < cutoff_naive,
                    )
                )
                timed_out_convs = list(r.scalars().all())

                for conv in timed_out_convs:
                    try:
                        conv.specialist_requested = False
                        conv.intercept_mode = INTERCEPT_MODE_BOT
                        conv.pending_draft = ""
                        conv.pending_draft_message_id = None
                        conv.operator_id = None
                        conv.operator_name = ""

                        internal_msg = Message(
                            conversation_id=conv.id,
                            content="Сработал автовозврат ИИ: таймаут ожидания оператора.",
                            sender="system",
                            is_read=False,
                            is_voice=False,
                            is_internal=True,
                        )
                        session.add(internal_msg)
                        await session.flush()

                        msg_result = await session.execute(
                            select(Message)
                            .where(Message.conversation_id == conv.id)
                            .order_by(Message.created_at.desc())
                            .limit(10)
                        )
                        recent = list(reversed(msg_result.scalars().all()))

                        override = "Оператор не смог подойти к чату. Сильно извинись перед клиентом и постарайся помочь ему самостоятельно."
                        draft, _ = await generate_draft(recent, session=session, override_instructions=override)

                        if draft:
                            ai_msg = Message(
                                conversation_id=conv.id,
                                content=draft,
                                sender="assistant",
                                is_read=False,
                            )
                            session.add(ai_msg)
                            await session.commit()
                            await session.refresh(ai_msg)

                            if conv.source == "telegram" and conv.social_id:
                                await send_telegram_message(conv.social_id, draft)

                            await sse_manager.broadcast("chat_updated", {"conversation_id": conv.id})
                            log.info("SLA автовозврат: conv_id=%s", conv.id)
                        else:
                            await session.commit()
                            await sse_manager.broadcast("chat_updated", {"conversation_id": conv.id})
                    except Exception as e:
                        log.error("SLA monitor error for conv %s: %s", conv.id, e)
                        await session.rollback()
        except Exception as e:
            log.error("SLA monitor loop error: %s", e)


AUTO_AI_RETURN_MINUTES = 15


async def _auto_ai_return_loop() -> None:
    """Фоновый цикл: автовозврат в режим ИИ (bot) через 15 минут бездействия оператора."""
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(timezone.utc)
            cutoff_naive = (now - timedelta(minutes=AUTO_AI_RETURN_MINUTES)).replace(tzinfo=None)

            async with AsyncSessionLocal() as session:
                r = await session.execute(
                    select(Conversation)
                    .where(
                        Conversation.intercept_mode.in_(
                            (INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_SENIOR)
                        ),
                    )
                )
                manual_convs = list(r.scalars().all())

            for conv in manual_convs:
                try:
                    async with AsyncSessionLocal() as session:
                        c = await session.get(Conversation, conv.id)
                        if not c or c.intercept_mode not in {
                            INTERCEPT_MODE_MANUAL,
                            INTERCEPT_MODE_SENIOR,
                        }:
                            continue
                        # Проверяем: оператор не писал 15 минут?
                        r = await session.execute(
                            select(Message)
                            .where(
                                Message.conversation_id == c.id,
                                or_(
                                    Message.sender == "operator",
                                    and_(Message.sender == "system", Message.is_internal == True),
                                ),
                            )
                            .order_by(Message.created_at.desc())
                            .limit(1)
                        )
                        last_op = r.scalar_one_or_none()
                        if last_op:
                            msg_ts = last_op.created_at
                            if msg_ts.tzinfo is None:
                                msg_ts = msg_ts.replace(tzinfo=timezone.utc)
                            if msg_ts >= (now - timedelta(minutes=AUTO_AI_RETURN_MINUTES)):
                                continue  # оператор писал недавно — не возвращаем
                        c.intercept_mode = INTERCEPT_MODE_BOT
                        c.pending_draft = ""
                        c.pending_draft_message_id = None
                        c.operator_id = None
                        c.operator_name = ""
                        await session.commit()
                        client_id = c.client_id
                    async with AsyncSessionLocal() as session:
                        result = await session.execute(
                            select(Client)
                            .where(Client.id == client_id)
                            .options(selectinload(Client.conversations).selectinload(Conversation.messages))
                        )
                        client = result.scalar_one_or_none()
                        if client:
                            client_data = ClientSchema.model_validate(client).model_dump(mode="json")
                            await sse_manager.broadcast("client_updated", {"client": client_data})
                    log.info("Auto AI return: conv_id=%s (15 min inactivity)", conv.id)
                except Exception as e:
                    log.error("Auto AI return error for conv %s: %s", conv.id, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Auto AI return loop error: %s", e)


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
    sla_task = asyncio.create_task(sla_monitor_loop())
    auto_ai_task = asyncio.create_task(_auto_ai_return_loop())
    broadcast_scheduler_task = asyncio.create_task(_scheduled_broadcast_loop())
    log.info("FastAPI startup complete")
    yield
    broadcast_scheduler_task.cancel()
    try:
        await broadcast_scheduler_task
    except asyncio.CancelledError:
        pass
    auto_ai_task.cancel()
    try:
        await auto_ai_task
    except asyncio.CancelledError:
        pass
    sla_task.cancel()
    try:
        await sla_task
    except asyncio.CancelledError:
        pass


# ── FastAPI приложение ────────────────────────────────────────────────────
app = FastAPI(
    title="Axoloti Terminal API",
    version="0.2.0",
    description="Омниканальная CRM для поддержки клиентов",
    lifespan=lifespan,
    strict_slashes=False,
)

# ── CORS (сразу после app, выше маршрутов) ─────────────────────────────────
# Разрешаем виджет (axoloti.ru), фронтенд (localhost) и SSE.
CORS_ORIGINS = [
    "http://axoloti.ru",
    "https://axoloti.ru",
    "http://www.axoloti.ru",
    "https://www.axoloti.ru",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
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
#  API v1 — SSE (мгновенные обновления при specialist_requested)
# ═══════════════════════════════════════════════════════════════════════════

def _verify_sse_token(token: str | None = Query(None, alias="token")) -> None:
    """Проверяет JWT из query-параметра (EventSource не поддерживает заголовки)."""
    if not token:
        raise HTTPException(status_code=401, detail="Token required")
    try:
        jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


@app.get("/api/v1/events/stream")
async def sse_stream(
    _: None = Depends(_verify_sse_token),
):
    """SSE-поток для мгновенных уведомлений (chat_updated при specialist_requested)."""
    from fastapi.responses import StreamingResponse

    async def event_generator():
        queue = sse_manager.add_client()
        try:
            yield "data: {\"event\":\"connected\"}\n\n"
            while True:
                msg = await queue.get()
                yield f"data: {msg}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sse_manager.remove_client(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    return TokenResponse(
        access_token=token,
        operator_id=operator.id,
        operator_username=operator.username,
    )


@app.get("/api/v1/auth/me")
async def get_current_operator_info(
    _operator: Operator = Depends(get_current_operator),
):
    """Возвращает данные текущего оператора (id, username)."""
    return {"id": _operator.id, "username": _operator.username}


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
        business_hours_enabled=getattr(settings, "business_hours_enabled", False),
        business_start=getattr(settings, "business_start", "09:00"),
        business_end=getattr(settings, "business_end", "18:00"),
        operator_sla_minutes=getattr(settings, "operator_sla_minutes", 5),
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
    if body.business_hours_enabled is not None:
        settings.business_hours_enabled = body.business_hours_enabled
    if body.business_start is not None:
        settings.business_start = body.business_start
    if body.business_end is not None:
        settings.business_end = body.business_end
    if body.operator_sla_minutes is not None:
        settings.operator_sla_minutes = body.operator_sla_minutes
    await session.commit()
    await session.refresh(settings)
    return SystemSettingsSchema(
        senior_welcome_message=settings.senior_welcome_message,
        business_hours_enabled=getattr(settings, "business_hours_enabled", False),
        business_start=getattr(settings, "business_start", "09:00"),
        business_end=getattr(settings, "business_end", "18:00"),
        operator_sla_minutes=getattr(settings, "operator_sla_minutes", 5),
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


@app.get("/api/v1/clients/{client_id}", response_model=ClientSchema)
async def get_client(
    client_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Возвращает одного клиента с диалогами (для подтягивания актуальных тегов и заметок при открытии чата)."""
    query = (
        select(Client)
        .where(Client.id == client_id)
        .options(
            selectinload(Client.conversations)
            .selectinload(Conversation.messages)
        )
    )
    result = await session.execute(query)
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


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
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Создаёт новое сообщение в диалоге и сохраняет в БД.

    Если диалог привязан к Telegram — параллельно пересылает текст клиенту.
    Блокировка: при sender=operator проверяем, что чат назначен текущему оператору.
    """
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Нормализация sender: support → operator для совместимости с виджетом и схемой
    sender = body.sender
    if sender in ("support", "operator"):
        sender = "operator"
    elif sender in ("assistant", "bot"):
        sender = "bot"

    if sender == "operator":
        if conv.operator_id is not None and conv.operator_id != _operator.id:
            raise HTTPException(
                status_code=403,
                detail="Чат уже занят другим оператором",
            )

    # Защита от дубликатов: если последнее сообщение с тем же текстом и sender создано недавно — 409
    if sender != "client":
        dup_window_sec = 5
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=dup_window_sec)).replace(tzinfo=None)
        last_result = await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        last_msg = last_result.scalar_one_or_none()
        if last_msg and last_msg.content == body.content and last_msg.sender == sender:
            last_created = last_msg.created_at
            last_naive = last_created.replace(tzinfo=None) if last_created and last_created.tzinfo else last_created
            if last_naive and last_naive >= cutoff:
                raise HTTPException(status_code=409, detail="Duplicate message")

    # Сброс pending_draft при любом ответе не от клиента (оператор или бот/суфлёр)
    if sender != "client":
        conv.pending_draft = ""
        conv.pending_draft_message_id = None
        if sender == "operator":
            conv.specialist_requested = False

    conv.last_interaction_at = datetime.now(timezone.utc).replace(tzinfo=None)
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


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Рассылка (Broadcast)
# ═══════════════════════════════════════════════════════════════════════════


async def _scheduled_broadcast_loop() -> None:
    """Фоновый цикл: каждые 60 секунд проверяет запланированные рассылки и запускает их."""
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            # naive datetime для сравнения с БД (SQLite хранит без tz)
            now_naive = now.replace(tzinfo=None) if now.tzinfo else now
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(ScheduledBroadcast)
                    .where(
                        ScheduledBroadcast.is_sent == False,
                        ScheduledBroadcast.scheduled_at <= now_naive,
                    )
                )
                rows = list(result.scalars().all())
            for row in rows:
                try:
                    raw = row.recipients if isinstance(row.recipients, list) else []
                    recipients = _normalize_recipients(raw)
                    if recipients:
                        await _run_broadcast_task(row.text, recipients)
                    async with AsyncSessionLocal() as session:
                        sb = await session.get(ScheduledBroadcast, row.id)
                        if sb:
                            sb.is_sent = True
                            await session.commit()
                    log.info("Scheduled broadcast #%s sent", row.id)
                except Exception as e:
                    log.error("Scheduled broadcast #%s failed: %s", row.id, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Scheduled broadcast loop error: %s", e)


def _normalize_recipients(raw: list) -> list[dict]:
    """Приводит recipients к формату [{client_id, source}]. Поддержка legacy [1,2,3]."""
    if not raw:
        return []
    first = raw[0]
    if isinstance(first, dict) and "client_id" in first and "source" in first:
        return [{"client_id": int(r["client_id"]), "source": str(r["source"])} for r in raw]
    return [{"client_id": int(x), "source": None} for x in raw]  # legacy: source=None = все каналы


async def _run_broadcast_task(text: str, recipients: list[dict]) -> None:
    """
    Фоновая задача рассылки. recipients: [{client_id, source}, ...].
    source=None (legacy) = отправить во все каналы клиента.
    """
    if not recipients:
        return
    client_ids = list({r["client_id"] for r in recipients})
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Client)
            .where(Client.id.in_(client_ids), Client.is_archived == False)
            .options(selectinload(Client.conversations))
        )
        clients_by_id = {c.id: c for c in result.scalars().all()}

    for rec in recipients:
        client_id = rec["client_id"]
        source = rec.get("source")
        client = clients_by_id.get(client_id)
        if not client or not client.conversations:
            continue
        client_name = (client.name or "Пользователь").strip() or "Пользователь"
        personalized_text = text.replace("{name}", client_name)

        convs = (
            [c for c in client.conversations if c.source == source]
            if source
            else client.conversations
        )
        for conv in convs:
            try:
                async with AsyncSessionLocal() as session:
                    c = await session.get(Conversation, conv.id)
                    if c:
                        c.last_interaction_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    msg = Message(
                        conversation_id=conv.id,
                        content=personalized_text,
                        sender="operator",
                        is_read=False,
                    )
                    session.add(msg)
                    await session.commit()

                    if conv.source == "telegram" and conv.social_id:
                        ok = await send_telegram_message(conv.social_id, personalized_text)
                        if not ok:
                            log.warning(
                                "Broadcast: не удалось отправить в Telegram (conv=%s)",
                                conv.id,
                            )
            except Exception as e:
                log.error("Broadcast: ошибка отправки клиенту %s: %s", client.id, e)
            await asyncio.sleep(1)


@app.post("/api/v1/broadcast")
async def start_broadcast(
    body: BroadcastRequest,
    background_tasks: BackgroundTasks,
    _: Operator = Depends(get_current_operator),
):
    """
    Запускает рассылку в фоне. Принимает text и recipients — список {client_id, source}.
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    recipients = [
        {"client_id": int(r.client_id), "source": str(r.source)}
        for r in (body.recipients or [])
        if r is not None
    ]
    if not recipients:
        raise HTTPException(status_code=400, detail="recipients is required and cannot be empty")

    scheduled_at = body.scheduled_at
    if scheduled_at is None:
        background_tasks.add_task(_run_broadcast_task, text, recipients)
        return {"status": "started", "message": "Рассылка запущена в фоновом режиме"}

    # Запланировать: сохраняем в ScheduledBroadcast (naive UTC для SQLite)
    dt = scheduled_at
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        dt = (dt.astimezone(timezone.utc)).replace(tzinfo=None)
    elif isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    async with AsyncSessionLocal() as session:
        sb = ScheduledBroadcast(
            text=text,
            recipients=recipients,
            scheduled_at=dt,
            is_sent=False,
        )
        session.add(sb)
        await session.commit()
    return {"status": "scheduled", "message": "Рассылка запланирована"}


@app.get("/api/v1/broadcasts/scheduled", response_model=list[ScheduledBroadcastSchema])
async def list_scheduled_broadcasts(
    _: Operator = Depends(get_current_operator),
):
    """Список запланированных рассылок (is_sent=False), отсортированных по времени."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ScheduledBroadcast)
            .where(ScheduledBroadcast.is_sent == False)
            .order_by(ScheduledBroadcast.scheduled_at.asc())
        )
        rows = list(result.scalars().all())
    return [
        ScheduledBroadcastSchema(
            id=r.id,
            text=r.text,
            recipients=r.recipients if isinstance(r.recipients, list) else [],
            scheduled_at=r.scheduled_at.replace(tzinfo=timezone.utc) if r.scheduled_at and r.scheduled_at.tzinfo is None else r.scheduled_at,
            is_sent=r.is_sent,
        )
        for r in rows
    ]


@app.delete("/api/v1/broadcasts/scheduled/{broadcast_id}")
async def delete_scheduled_broadcast(
    broadcast_id: int,
    _: Operator = Depends(get_current_operator),
):
    """Удаляет запланированную рассылку по ID."""
    async with AsyncSessionLocal() as session:
        sb = await session.get(ScheduledBroadcast, broadcast_id)
        if not sb:
            raise HTTPException(status_code=404, detail="Scheduled broadcast not found")
        await session.delete(sb)
        await session.commit()
    return {"status": "deleted", "message": "Рассылка отменена"}


# Regex: телефон (10+ цифр, возможно с +) или email
_CONTACT_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{9,}\d|\d{10,}")
_CONTACT_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def _detect_contact_in_text(text: str) -> bool:
    """Проверяет, есть ли в тексте телефон (10+ цифр) или email."""
    if not text or not isinstance(text, str):
        return False
    t = text.strip()
    return bool(_CONTACT_PHONE_RE.search(t) or _CONTACT_EMAIL_RE.search(t))


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
    conv.operator_id = None
    conv.operator_name = ""
    await session.commit()
    client_id = conv.client_id
    log.info(
        "AUTO-WAKEUP: Client %s (conv %s) returned to AI mode (operator timeout)",
        client_id,
        conv.id,
    )
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(Client)
            .where(Client.id == client_id)
            .options(selectinload(Client.conversations).selectinload(Conversation.messages))
        )
        c = r.scalar_one_or_none()
        if c:
            client_data = ClientSchema.model_validate(c).model_dump(mode="json")
            await sse_manager.broadcast("client_updated", {"client": client_data})
    return True


async def _get_senior_welcome_message(session: AsyncSession) -> str:
    """Возвращает приветственное сообщение для режима senior из SystemSettings."""
    result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings:
        return settings.senior_welcome_message
    return DEFAULT_SENIOR_WELCOME_MESSAGE


@app.patch("/api/v1/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: int,
    body: ConversationUpdate,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Частичное обновление диалога (теги и др.)."""
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if body.tags is not None:
        conv.tags = body.tags
    await session.commit()
    await session.refresh(conv)
    return {"id": conv.id, "tags": conv.tags}


@app.patch("/api/v1/conversations/{conversation_id}/clear-contact", status_code=200)
async def clear_conversation_contact(
    conversation_id: int,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Сбрасывает флаг has_new_contact в False после обработки контакта."""
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.has_new_contact = False
    await session.commit()
    await session.refresh(conv)
    result = await session.execute(
        select(Client)
        .where(Client.id == conv.client_id)
        .options(selectinload(Client.conversations).selectinload(Conversation.messages))
    )
    client = result.scalar_one_or_none()
    if client:
        client_data = ClientSchema.model_validate(client).model_dump(mode="json")
        await sse_manager.broadcast("client_updated", {"client": client_data})
    return {"id": conv.id, "has_new_contact": False}


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
    Блокировка: если чат занят другим оператором — 403.
    """
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conv.operator_id is not None and conv.operator_id != _operator.id:
        raise HTTPException(
            status_code=403,
            detail="Чат уже занят другим оператором",
        )

    prev_mode = conv.intercept_mode or INTERCEPT_MODE_BOT
    new_mode = _normalize_intercept_mode(body.mode)
    conv.intercept_mode = new_mode

    if new_mode == INTERCEPT_MODE_BOT:
        conv.operator_id = None
        conv.operator_name = ""

    if conv.intercept_mode != INTERCEPT_MODE_PROMPTER:
        conv.pending_draft = ""
        conv.pending_draft_message_id = None

    # Сброс флага вызова при любой ручной смене режима (manual, prompter, senior)
    if new_mode in {INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_PROMPTER, INTERCEPT_MODE_SENIOR}:
        conv.specialist_requested = False

    # Оператор перехватил управление — авто-назначаем если свободен
    if new_mode in {INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_SENIOR}:
        conv.last_interaction_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if conv.operator_id is None:
            conv.operator_id = _operator.id
            conv.operator_name = (body.operator_name or "").strip() or _operator.username

    # Скрытое системное сообщение при перехвате (видны только в Терминале)
    if prev_mode == INTERCEPT_MODE_BOT and new_mode in {
        INTERCEPT_MODE_PROMPTER,
        INTERCEPT_MODE_MANUAL,
        INTERCEPT_MODE_SENIOR,
    }:
        device_id = (body.device_id or "").strip() or "—"
        operator_name = (body.operator_name or "").strip() or "Оператор"
        operator_role = (body.operator_role or "").strip() or "Специалист"
        operator_os = (body.operator_os or "").strip() or "—"
        internal_text = (
            f"{operator_name} ({operator_role}) • {operator_os} • {device_id} — "
            f"перехватил управление. Режим: {get_intercept_mode_label_ru(new_mode)}"
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

    # При переключении на senior — отправляем уведомление клиенту (из запроса или SystemSettings)
    if conv.intercept_mode == INTERCEPT_MODE_SENIOR:
        senior_text = (body.senior_welcome_message or "").strip() or await _get_senior_welcome_message(session)
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


@app.post("/api/v1/conversations/{conversation_id}/assign")
async def assign_conversation(
    conversation_id: int,
    body: InterceptModeUpdate,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Назначает диалог текущему оператору (Assign to me). Race condition: 403 если уже занят другим."""
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conv.operator_id is not None and conv.operator_id != _operator.id:
        raise HTTPException(
            status_code=403,
            detail="Чат уже занят другим оператором",
        )

    operator_name = (body.operator_name or "").strip() or _operator.username
    conv.operator_id = _operator.id
    conv.operator_name = operator_name
    conv.specialist_requested = False
    await session.commit()
    await session.refresh(conv)

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(Client)
            .where(Client.id == conv.client_id)
            .options(selectinload(Client.conversations).selectinload(Conversation.messages))
        )
        c = r.scalar_one_or_none()
        if c:
            client_data = ClientSchema.model_validate(c).model_dump(mode="json")
            await sse_manager.broadcast("client_updated", {"client": client_data})

    return {
        "conversation_id": conv.id,
        "operator_id": conv.operator_id,
        "operator_name": conv.operator_name,
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

    draft, request_operator = await generate_draft(recent, session=session)
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
        log.warning("telegram_webhook invalid JSON: %s", e)
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
        tg_original = f"@{username}" if username else full_name
        client = Client(
            name=full_name,
            original_name=tg_original,
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
            label=f"Telegram: {tg_original}",
            original_name=tg_original,
        )
        session.add(conv)
        await session.flush()
    else:
        client = await session.get(Client, conv.client_id)

    conv.last_interaction_at = datetime.now(timezone.utc).replace(tzinfo=None)
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

    # Детектор контактов: телефон или email в сообщении
    if _detect_contact_in_text(text):
        conv.has_new_contact = True

    await session.commit()
    await session.refresh(msg)

    if conv.has_new_contact or conv.specialist_requested:
        async with AsyncSessionLocal() as s:
            r = await s.execute(
                select(Client)
                .where(Client.id == conv.client_id)
                .options(selectinload(Client.conversations).selectinload(Conversation.messages))
            )
            c = r.scalar_one_or_none()
            if c:
                client_data = ClientSchema.model_validate(c).model_dump(mode="json")
                await sse_manager.broadcast("client_updated", {"client": client_data})

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
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded and forwarded.strip():
        # Первый адрес в списке — клиент, остальные — прокси
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real_ip = request.headers.get("X-Real-IP")
    if real_ip and real_ip.strip():
        return real_ip.strip()
    if request.client:
        return request.client.host or ""
    return ""


def _extract_city_for_visitor(location: str) -> str:
    """Извлекает город из location. Пустая строка = город не определён (не писать «Инкогнито»)."""
    if not location or not location.strip():
        return ""
    loc = location.strip()
    if loc in ("Сеть клиента", "Локальная сеть"):
        return ""
    if "," in loc:
        return loc.split(",")[0].strip() or ""
    return loc


def _make_visitor_name(city: str = "") -> str:
    """Генерирует имя «Посетитель #XXXX» или «Посетитель #XXXX (город)». Всегда 4 случайные цифры."""
    random_num = random.randint(1000, 9999)
    if city and city.strip():
        return f"Посетитель #{random_num} ({city.strip()})"
    return f"Посетитель #{random_num}"


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
        log.warning("web_widget_webhook invalid JSON: %s", e)
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
        client = result.scalars().first()

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
        # Клиент НЕ найден — создаём нового. Имя строго: «Посетитель #XXXX» или «Посетитель #XXXX (город)»
        city = _extract_city_for_visitor(location)
        visitor_name = _make_visitor_name(city)
        client = Client(
            name=visitor_name,
            original_name=visitor_name,
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
            conv = result.scalars().first()

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
        conv = result.scalars().first()

    if conv is None:
        social_id = visitor_id or str(uuid.uuid4())
        web_label = f"Web: {client.original_name or client.name}"
        conv = Conversation(
            client_id=client.id,
            source="web",
            social_id=social_id,
            label=web_label,
            original_name=client.original_name or client.name,
        )
        session.add(conv)
        await session.flush()

    conv.last_interaction_at = datetime.now(timezone.utc).replace(tzinfo=None)
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

    # Детектор контактов: телефон или email в сообщении
    if _detect_contact_in_text(text):
        conv.has_new_contact = True

    await session.commit()
    await session.refresh(msg)

    if conv.has_new_contact or conv.specialist_requested:
        async with AsyncSessionLocal() as s:
            r = await s.execute(
                select(Client)
                .where(Client.id == conv.client_id)
                .options(selectinload(Client.conversations).selectinload(Conversation.messages))
            )
            c = r.scalar_one_or_none()
            if c:
                client_data = ClientSchema.model_validate(c).model_dump(mode="json")
                await sse_manager.broadcast("client_updated", {"client": client_data})

    if conv.intercept_mode in {INTERCEPT_MODE_MANUAL, INTERCEPT_MODE_SENIOR}:
        did_wakeup = await _check_and_auto_wakeup_manual_mode(session, conv)
        if not did_wakeup:
            return {
                "reply": "Ожидайте ответа оператора.",
                "thread_id": f"conv-{conv.id}",
            }

    # В режиме prompter: только черновик в pending_draft, НЕ создаём сообщение в чат
    if conv.intercept_mode == INTERCEPT_MODE_PROMPTER:
        result = await session.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc())
            .limit(10)
        )
        recent = list(reversed(result.scalars().all()))
        draft, request_operator = await generate_draft(recent, session=session)
        if request_operator:
            # Проверка рабочих часов при эскалации: нерабочее время — отменяем перевод, шлём офлайн-сообщение
            offline_msg = await check_offline_block(session, conv)
            if offline_msg:
                content_to_reply = offline_msg
                ai_msg = Message(
                    conversation_id=conv.id,
                    content=offline_msg,
                    sender="bot",
                    is_read=False,
                    is_voice=False,
                )
                session.add(ai_msg)
                await session.commit()
                await sse_manager.broadcast("chat_updated", {"conversation_id": conv.id})
            else:
                conv.specialist_requested = True
                conv.specialist_requested_at = datetime.utcnow()
                conv.intercept_mode = INTERCEPT_MODE_MANUAL
                internal_msg = Message(
                    conversation_id=conv.id,
                    content=f"Система — перевёл на специалиста по запросу клиента. Режим: {get_intercept_mode_label_ru(INTERCEPT_MODE_MANUAL)}",
                    sender="system",
                    is_read=False,
                    is_voice=False,
                    is_internal=True,
                )
                session.add(internal_msg)
                content_to_reply = "Перевожу вас на специалиста, одну минуту..."
                ai_msg = Message(
                    conversation_id=conv.id,
                    content=content_to_reply,
                    sender="bot",
                    is_read=False,
                    is_voice=False,
                )
                session.add(ai_msg)
                await session.commit()
                await sse_manager.broadcast("chat_updated", {"conversation_id": conv.id})
        elif draft:
            conv.pending_draft = draft
            conv.pending_draft_message_id = msg.id
            conv.last_ai_handled_message_id = msg.id
            await session.commit()
            await sse_manager.broadcast("chat_updated", {"conversation_id": conv.id})
            content_to_reply = "Ожидайте ответа оператора."
        else:
            content_to_reply = "Извините, не удалось сформировать ответ. Попробуйте позже."
        return {
            "reply": content_to_reply,
            "thread_id": f"conv-{conv.id}",
        }

    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc())
        .limit(10)
    )
    recent = list(reversed(result.scalars().all()))
    draft, request_operator = await generate_draft(recent, session=session)

    # Проверка рабочих часов при эскалации: нерабочее время — отменяем перевод на оператора
    if request_operator:
        offline_msg = await check_offline_block(session, conv)
        if offline_msg:
            request_operator = False
            draft = offline_msg

    if request_operator:
        conv.specialist_requested = True
        conv.specialist_requested_at = datetime.utcnow()
        conv.intercept_mode = INTERCEPT_MODE_MANUAL  # Жёстко блокируем ИИ
        internal_msg = Message(
            conversation_id=conv.id,
            content=f"Система — перевёл на специалиста по запросу клиента. Режим: {get_intercept_mode_label_ru(INTERCEPT_MODE_MANUAL)}",
            sender="system",
            is_read=False,
            is_voice=False,
            is_internal=True,
        )
        session.add(internal_msg)

    content_to_reply = "Перевожу вас на специалиста, одну минуту..." if request_operator else (draft or "Извините, не удалось сформировать ответ. Попробуйте позже.")

    if request_operator or draft:
        conv.last_interaction_at = datetime.now(timezone.utc).replace(tzinfo=None)
        ai_msg = Message(
            conversation_id=conv.id,
            content="Перевожу вас на специалиста, одну минуту..." if request_operator else draft,
            sender="bot",
            is_read=False,
            is_voice=False,
        )
        session.add(ai_msg)
        await session.commit()
        await session.refresh(ai_msg)
    elif request_operator:
        await session.commit()

    if request_operator:
        await sse_manager.broadcast("chat_updated", {"conversation_id": conv.id})

    return {
        "reply": content_to_reply or "Извините, не удалось сформировать ответ. Попробуйте позже.",
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

    # Имя: если у conv/source уже есть original_name — оставляем; иначе генерируем по правилу
    source_client = await session.get(Client, conv.client_id)
    existing_name = (conv.original_name or "").strip() or (source_client.original_name if source_client else "")
    if existing_name and existing_name.strip():
        visitor_label = existing_name.strip()
    else:
        visitor_label = _make_visitor_name("")

    new_client = Client(
        name=visitor_label,
        original_name=visitor_label,
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
    conv.original_name = visitor_label
    conv.label = f"Web: {visitor_label}"
    await session.flush()

    await session.commit()

    return {"conversation_id": conv.id, "new_client_id": new_client.id}


# ── Запуск ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
