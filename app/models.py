"""
Axoloti Terminal — Pydantic-схемы для сериализации данных в JSON.

Эти схемы превращают ORM-объекты (Client, Conversation, Message)
в красивый, типизированный JSON для фронтенда.
"""

from datetime import datetime
from typing import Optional, Union
from pydantic import BaseModel, ConfigDict, field_validator


class LoginRequest(BaseModel):
    """Запрос на вход оператора."""
    username: str
    password: str


class TokenResponse(BaseModel):
    """JWT-токен после успешного входа."""
    access_token: str
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
