"""
Axoloti Terminal — ORM-модели омниканальной CRM.
"""

from datetime import datetime
from typing import List

from sqlalchemy import (
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# ═══════════════════════════════════════════════════════════════════════════
#  OPERATOR — Оператор (JWT-аутентификация)
# ═══════════════════════════════════════════════════════════════════════════
class Operator(Base):
    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    def __repr__(self) -> str:
        return f"<Operator #{self.id} {self.username}>"


# ═══════════════════════════════════════════════════════════════════════════
#  CLIENT — Карточка клиента
# ═══════════════════════════════════════════════════════════════════════════
class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    avatar: Mapped[str] = mapped_column(String(500), default="")
    
    # 🟢 КОНТАКТЫ И ПРОФИЛЬ (Добавлены новые поля)
    phone: Mapped[str] = mapped_column(String(50), index=True, default="")
    email: Mapped[str] = mapped_column(String(255), index=True, default="")
    social_link: Mapped[str] = mapped_column(String(500), default="")   # vk/t.me/instagram/сайт (глобус)
    location: Mapped[str] = mapped_column(String(255), default="")       # Город/Страна
    website: Mapped[str] = mapped_column(String(255), default="")        # Сайт/Соцсеть (legacy)
    device_info: Mapped[str] = mapped_column(String(255), default="")    # Данные системы (legacy)

    # 🟢 ВЕБ-ВИДЖЕТ: склейка клиентов и техданные
    axolotl_visitor_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    ip: Mapped[str] = mapped_column(String(45), default="")  # IPv4/IPv6
    browser: Mapped[str] = mapped_column(String(64), default="")
    os_device: Mapped[str] = mapped_column(String(128), default="")
    
    notes: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(String(500), default="")

    # Управление списком
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    conversations: Mapped[List["Conversation"]] = relationship(
        back_populates="client",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Client #{self.id} {self.name}>"


# ═══════════════════════════════════════════════════════════════════════════
#  CONVERSATION — Диалог
# ═══════════════════════════════════════════════════════════════════════════
class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Без ondelete="CASCADE" — при merge перепривязываем диалоги до удаления клиента
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=False,
    )

    source: Mapped[str] = mapped_column(String(30), nullable=False)
    social_id: Mapped[str] = mapped_column(String(120), default="")
    label: Mapped[str] = mapped_column(String(255), default="")
    intercept_mode: Mapped[str] = mapped_column(String(30), default="bot")
    specialist_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    specialist_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pending_draft: Mapped[str] = mapped_column(Text, default="")
    pending_draft_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_ai_handled_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags: Mapped[str] = mapped_column(String(500), default="")
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    messages: Mapped[List["Message"]] = relationship(
        back_populates="conversation",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    client: Mapped["Client"] = relationship(back_populates="conversations")


# ═══════════════════════════════════════════════════════════════════════════
#  MESSAGE — Сообщение
# ═══════════════════════════════════════════════════════════════════════════
class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    sender: Mapped[str] = mapped_column(String(20), nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_voice: Mapped[bool] = mapped_column(Boolean, default=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


# ═══════════════════════════════════════════════════════════════════════════
#  SYSTEM SETTINGS — Singleton (всегда одна запись id=1)
# ═══════════════════════════════════════════════════════════════════════════
DEFAULT_SENIOR_WELCOME_MESSAGE = "К диалогу подключился старший специалист."


class SystemSettings(Base):
    """Системные настройки приложения. Singleton: всегда одна запись с id=1."""
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    senior_welcome_message: Mapped[str] = mapped_column(
        String(500),
        default=DEFAULT_SENIOR_WELCOME_MESSAGE,
        nullable=False,
    )
    business_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    business_start: Mapped[str] = mapped_column(String(10), default="09:00")
    business_end: Mapped[str] = mapped_column(String(10), default="18:00")
    operator_sla_minutes: Mapped[int] = mapped_column(Integer, default=5)

    def __repr__(self) -> str:
        return f"<SystemSettings id={self.id}>"


# ═══════════════════════════════════════════════════════════════════════════
#  SCHEDULED BROADCAST — Отложенная рассылка
# ═══════════════════════════════════════════════════════════════════════════
class ScheduledBroadcast(Base):
    """Запланированная рассылка (отложенная по времени)."""
    __tablename__ = "scheduled_broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    recipients: Mapped[list] = mapped_column("client_ids", JSON, nullable=False)  # [{"client_id": 1, "source": "telegram"}, ...]; legacy: [1,2,3]
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # UTC
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<ScheduledBroadcast #{self.id} at {self.scheduled_at}>"
