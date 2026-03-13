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
    sender: str          # client / bot / operator
    created_at: datetime
    is_read: bool
    is_voice: bool = False


class ConversationSchema(BaseModel):
    """Диалог в конкретном канале (Telegram, WhatsApp, Site)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str          # telegram / whatsapp / site
    social_id: str
    label: str
    intercept_mode: str = "bot"
    pending_draft: Optional[str] = ""
    messages: list[MessageSchema] = []


class InterceptModeUpdate(BaseModel):
    """Обновление серверного режима перехвата."""
    mode: str


class SystemSettingsSchema(BaseModel):
    """Системные настройки приложения."""
    senior_welcome_message: str = "К диалогу подключился старший специалист."


class SystemSettingsUpdate(BaseModel):
    """Частичное обновление системных настроек."""
    senior_welcome_message: Optional[str] = None


class ClientMergeRequest(BaseModel):
    """Запрос на объединение клиента с другим."""
    target_client_id: int


class ClientUpdate(BaseModel):
    """Частичное обновление карточки клиента."""
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


class ClientSchema(BaseModel):
    """Карточка клиента со всеми диалогами."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
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
