"""
Axoloti Terminal — Главный файл FastAPI-приложения.

Запуск:
    cd backend
    python -m app.main
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import BackgroundTasks, FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
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
from app.models import Client, Conversation, Message, Operator
from app.schemas import (
    ClientSchema,
    ClientUpdate,
    InterceptModeUpdate,
    LoginRequest,
    MessageCreate,
    MessageSchema,
    TokenResponse,
)
from app.services.ai_dispatcher import (
    INTERCEPT_MODE_BOT,
    INTERCEPT_MODE_PROMPTER,
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

    _NOISY_FRAGMENTS = ("GET /api/v1/clients ", "GET /api/v1/health ")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.INFO:
            return True
        msg = record.getMessage()
        return not any(frag in msg for frag in self._NOISY_FRAGMENTS)


logging.getLogger("uvicorn.access").addFilter(_SuppressPollingFilter())


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

        msg_columns = await _get_table_columns(conn, "messages")

        if "is_voice" not in msg_columns:
            default_val = "0" if not is_postgres() else "false"
            await conn.execute(text(
                f"ALTER TABLE messages ADD COLUMN is_voice BOOLEAN NOT NULL DEFAULT {default_val}"
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
)

app.include_router(ai_endpoints.router, prefix="/api/v1")

# ── CORS ──────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

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

    msg = Message(
        conversation_id=conversation_id,
        content=body.content,
        sender=body.sender,
        is_read=False,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)

    if conv.source == "telegram" and conv.social_id and body.sender != "client":
        ok = await send_telegram_message(conv.social_id, body.content)
        if not ok:
            log.warning(
                "Не удалось доставить сообщение в Telegram (conv=%s, social_id=%s)",
                conversation_id,
                conv.social_id,
            )

    return msg


@app.patch("/api/v1/conversations/{conversation_id}/intercept-mode")
async def update_intercept_mode(
    conversation_id: int,
    body: InterceptModeUpdate,
    _operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_session),
):
    """Обновляет серверный режим перехвата для диалога."""
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    conv.intercept_mode = body.mode or INTERCEPT_MODE_BOT
    if conv.intercept_mode != INTERCEPT_MODE_PROMPTER:
        conv.pending_draft = ""
        conv.pending_draft_message_id = None

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

    draft = await generate_draft(recent)
    if draft is None:
        raise HTTPException(status_code=502, detail="Failed to generate draft")

    conv.pending_draft = draft
    latest_client_msg = next((msg for msg in reversed(recent) if msg.sender == "client"), None)
    conv.pending_draft_message_id = latest_client_msg.id if latest_client_msg else None
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
    except Exception:
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
        # Для уже существующего клиента webhook должен только добавлять Message.
        # Не восстанавливаем source-tag и не трогаем операторские правки профиля.
        pass

    # ── Сохраняем входящее сообщение ─────────────────────────────────────
    msg = Message(
        conversation_id=conv.id,
        content=text,
        sender="client",
        is_read=False,
        is_voice=is_voice,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)

    if conv.intercept_mode in {INTERCEPT_MODE_BOT, INTERCEPT_MODE_PROMPTER}:
        background_tasks.add_task(process_incoming_client_message, conv.id, msg.id)

    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
#  API v1 — Web Widget Webhook
# ═══════════════════════════════════════════════════════════════════════════

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
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    text = (body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is required")

    thread_id_raw = body.get("thread_id")
    conv = None

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

    if conv is None:
        client = Client(
            name="Посетитель сайта",
            avatar="",
            phone="",
            email="",
            website="",
            notes="",
            tags="web",
        )
        session.add(client)
        await session.flush()

        session_id = str(uuid.uuid4())
        conv = Conversation(
            client_id=client.id,
            source="web",
            social_id=session_id,
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
    await session.commit()
    await session.refresh(msg)

    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc())
        .limit(10)
    )
    recent = list(reversed(result.scalars().all()))
    draft = await generate_draft(recent)

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


# ── Запуск ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
