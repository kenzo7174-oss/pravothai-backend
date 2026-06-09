"""
Axoloti Terminal — Pydantic-схемы для сериализации данных в JSON.

Эти схемы превращают ORM-объекты (Client, Conversation, Message)
в красивый, типизированный JSON для фронтенда.
"""

from datetime import datetime
from typing import Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, field_validator


class LoginRequest(BaseModel):
    """Запрос на вход (Логин + Пароль)."""
    username: str
    password: str


class CheckLoginRequest(BaseModel):
    """Запрос проверки логина (шаг 1, опционально)."""
    username: str


class CheckLoginResponse(BaseModel):
    """Ответ проверки логина: существует ли пользователь и нужно ли установить пароль."""
    exists: bool
    needs_password_setup: bool
    setup_token: Optional[str] = None


class SetPasswordRequest(BaseModel):
    """Запрос на первичную установку пароля."""
    new_password: str
    confirm_password: str


class TokenResponse(BaseModel):
    """JWT-токен после успешного входа."""
    access_token: str
    token_type: str = "bearer"
    operator_id: Optional[int] = None
    operator_username: Optional[str] = None
    operator_role: Optional[str] = None


class OperatorSchema(BaseModel):
    """Оператор для списка команды."""
    id: int
    username: str
    full_name: Optional[str] = None
    job_title: Optional[str] = None
    role: str
    is_active: bool
    is_online: bool = False
    last_login: Optional[datetime] = None
    last_device_os: Optional[str] = None
    last_device_browser: Optional[str] = None


class OperatorProfileUpdate(BaseModel):
    """Обновление профиля текущего оператора (только свой ID)."""
    full_name: Optional[str] = None
    job_title: Optional[str] = None


class UpdateCredentialsRequest(BaseModel):
    """Смена логина и/или пароля текущего оператора."""
    current_password: str
    new_username: Optional[str] = None
    new_password: Optional[str] = None


class UpdateCredentialsResponse(BaseModel):
    """Результат обновления учетных данных."""
    username: str
    password_changed: bool
    username_changed: bool
    access_token: Optional[str] = None
    token_type: str = "bearer"


class MessageCreate(BaseModel):
    """Входящий запрос на создание сообщения."""
    content: str
    sender: str
    is_system: Optional[bool] = False


class MessageSchema(BaseModel):
    """Одно сообщение в диалоге."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    content: str
    sender: str          # client / bot / operator / system
    created_at: datetime
    is_read: bool
    is_voice: bool = False
    is_internal: bool = False


class ConversationSchema(BaseModel):
    """Диалог в конкретном канале (Telegram, WhatsApp, Site)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str          # telegram / whatsapp / site
    social_id: str
    label: str
    original_name: str = ""  # Имя для списка чатов (канал-специфичное)
    intercept_mode: str = "bot"
    specialist_requested: bool = False
    specialist_requested_at: Optional[datetime] = None
    pending_draft: Optional[str] = ""
    tags: Optional[str] = ""
    has_new_contact: bool = False
    operator_id: Optional[int] = None
    operator_name: str = ""
    messages: list[MessageSchema] = []


class ConversationUpdate(BaseModel):
    """Частичное обновление диалога."""
    tags: Optional[str] = None


class InterceptModeUpdate(BaseModel):
    """Обновление серверного режима перехвата."""
    mode: str
    device_id: Optional[str] = None
    operator_name: Optional[str] = None
    operator_role: Optional[str] = None
    operator_os: Optional[str] = None
    senior_welcome_message: Optional[str] = None
    trigger: Optional[str] = None


class SystemSettingsSchema(BaseModel):
    """Системные настройки приложения."""
    senior_welcome_message: str = "К диалогу подключился старший специалист."
    business_hours_enabled: bool = False
    business_start: str = "09:00"
    business_end: str = "18:00"
    operator_sla_minutes: int = 5


class SystemSettingsUpdate(BaseModel):
    """Частичное обновление системных настроек."""
    senior_welcome_message: Optional[str] = None
    business_hours_enabled: Optional[bool] = None
    business_start: Optional[str] = None
    business_end: Optional[str] = None
    operator_sla_minutes: Optional[int] = None


class DatabaseStorageSchema(BaseModel):
    """Заполненность базы данных относительно лимита провайдера."""
    size_bytes: int
    limit_bytes: int
    usage_percent: float
    level: str  # ok / warning / critical
    is_postgres: bool
    provider_name: str  # Neon / Aiven / PostgreSQL / SQLite


class StorageChannelsSchema(BaseModel):
    """Список уникальных каналов (source) в базе данных."""
    channels: list[str]


class StorageCleanupRequest(BaseModel):
    """Запрос на удаление неактивных диалогов."""
    period: Union[Literal[30, 60, 90], Literal["all"]]
    channels_to_delete: list[str]
    delete_mode: str  # full / messages_only

    @field_validator("period", mode="before")
    @classmethod
    def validate_period(cls, value):
        if value == "all":
            return "all"
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if value in (30, 60, 90):
            return value
        raise ValueError("Период должен быть 30, 60, 90 дней или all")

    @field_validator("channels_to_delete")
    @classmethod
    def validate_channels(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("Необходимо выбрать хотя бы один канал")
        return cleaned

    @field_validator("delete_mode")
    @classmethod
    def validate_delete_mode(cls, value: str) -> str:
        if value not in ("full", "messages_only"):
            raise ValueError("delete_mode должен быть full или messages_only")
        return value


class StorageCleanupResponse(BaseModel):
    """Результат очистки неактивных диалогов."""
    deleted_count: int
    delete_mode: str


class ClientMergeRequest(BaseModel):
    """Запрос на объединение клиента с другим."""
    target_client_id: int


class ClientUpdate(BaseModel):
    """Частичное обновление карточки клиента. original_name не обновляется."""
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    social_link: Optional[str] = None
    location: Optional[str] = None
    ip: Optional[str] = None
    website: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[Union[str, list[str]]] = None
    browser: Optional[str] = None
    os_device: Optional[str] = None

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, v):
        if v is None:
            return v
        if isinstance(v, list):
            return ",".join(str(t).strip() for t in v if str(t).strip())
        return v


class BroadcastRecipient(BaseModel):
    """Один получатель рассылки: клиент + канал."""
    client_id: int
    source: str  # telegram / web_widget / whatsapp / ...


class BroadcastRequest(BaseModel):
    """Запрос на запуск рассылки."""
    text: str
    recipients: list[BroadcastRecipient] = []  # список (client_id, source)
    scheduled_at: Optional[datetime] = None  # ISO-строка или null = отправить сейчас


class ScheduledBroadcastSchema(BaseModel):
    """Запланированная рассылка (ответ API)."""
    id: int
    text: str
    recipients: list[dict]  # [{client_id, source}, ...] или legacy [1,2,3]
    scheduled_at: datetime
    is_sent: bool = False

    model_config = ConfigDict(from_attributes=True)

    @field_validator("scheduled_at", mode="before")
    @classmethod
    def _parse_scheduled_at(cls, v):
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
        return None


class BroadcastHistorySchema(BaseModel):
    """Запись истории рассылки (ответ API)."""
    id: str
    created_at: datetime
    message_text: str
    channels: list[str]
    status: str  # success / error
    recipients_count: int

    model_config = ConfigDict(from_attributes=True)


class ClientSchema(BaseModel):
    """Карточка клиента со всеми диалогами."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    original_name: str = ""
    avatar: str
    phone: str
    email: str
    social_link: str = ""
    location: str = ""
    ip: str = ""
    website: str = ""
    device_info: str = ""
    browser: str = ""
    os_device: str = ""
    notes: str
    tags: str
    conversations: list[ConversationSchema] = []


class VapidPublicKeyResponse(BaseModel):
    publicKey: str


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionCreate(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys
    expirationTime: Optional[int] = None
