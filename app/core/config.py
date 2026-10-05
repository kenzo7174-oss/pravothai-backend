"""
Axoloti Terminal — Настройки приложения.

Все значения читаются через os.getenv.
"""

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# Небезопасные значения по умолчанию — запрещены в проде (см. assert_production_secrets).
DEFAULT_JWT_SECRET = "axoloti-dev-secret-change-me-in-production"
DEFAULT_ADMIN_PASSWORD_FALLBACK = "admin"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
APP_ENV = os.getenv("APP_ENV", "development")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", DEFAULT_JWT_SECRET)
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
# Срок сессии оператора: по умолчанию 180 дней (можно переопределить через JWT_ACCESS_TOKEN_EXPIRE_MINUTES).
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", str(180 * 24 * 60))
)
TELEGRAM_WEBHOOK_AUTOMATION_ENABLED = os.getenv("TELEGRAM_WEBHOOK_AUTOMATION_ENABLED", "true").lower() == "true"
TELEGRAM_WEBHOOK_PATH = os.getenv("TELEGRAM_WEBHOOK_PATH", "/api/v1/webhooks/telegram")
# Секрет для валидации входящих Telegram-вебхуков (заголовок X-Telegram-Bot-Api-Secret-Token).
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
TELEGRAM_WEBHOOK_PUBLIC_URL = os.getenv("WEBHOOK_DOMAIN", os.getenv("TELEGRAM_WEBHOOK_PUBLIC_URL", "")).strip().rstrip("/")
TELEGRAM_TUNNEL_PORT = int(os.getenv("TELEGRAM_TUNNEL_PORT", "8000"))
TELEGRAM_TUNNEL_STARTUP_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TUNNEL_STARTUP_TIMEOUT_SECONDS", "60"))
TELEGRAM_TUNNEL_RESTART_DELAY_SECONDS = int(os.getenv("TELEGRAM_TUNNEL_RESTART_DELAY_SECONDS", "5"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or None
OPENAI_ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID") or None
OPENAI_VECTOR_STORE_ID = os.getenv("OPENAI_VECTOR_STORE_ID", "").strip()
# Chat Completions API (если OPENAI_ASSISTANT_ID не задан)
OPENAI_SYSTEM_PROMPT = os.getenv("OPENAI_SYSTEM_PROMPT") or None
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")

DEFAULT_ADMIN_USER = os.getenv("DEFAULT_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_CONTACT_EMAIL = os.getenv("VAPID_CONTACT_EMAIL", "mailto:admin@axoloti.ru").strip()

# === DO NOT DELETE: THAI LEGAL BOT EXPORT FEATURE ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ENABLE_TELEGRAM_EXPORT = os.getenv("ENABLE_TELEGRAM_EXPORT", "false").lower() in ("1", "true", "yes")
# === DO NOT DELETE: THAI LEGAL BOT EXPORT FEATURE ===


class Settings:
    """Обёртка для совместимости с кодом, использующим settings.ATTR."""

    TELEGRAM_BOT_TOKEN: str = TELEGRAM_BOT_TOKEN
    APP_ENV: str = APP_ENV
    JWT_SECRET_KEY: str = JWT_SECRET_KEY
    JWT_ALGORITHM: str = JWT_ALGORITHM
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    TELEGRAM_WEBHOOK_AUTOMATION_ENABLED: bool = TELEGRAM_WEBHOOK_AUTOMATION_ENABLED
    TELEGRAM_WEBHOOK_PATH: str = TELEGRAM_WEBHOOK_PATH
    TELEGRAM_WEBHOOK_SECRET: str = TELEGRAM_WEBHOOK_SECRET
    TELEGRAM_WEBHOOK_PUBLIC_URL: str = TELEGRAM_WEBHOOK_PUBLIC_URL
    TELEGRAM_TUNNEL_PORT: int = TELEGRAM_TUNNEL_PORT
    TELEGRAM_TUNNEL_STARTUP_TIMEOUT_SECONDS: int = TELEGRAM_TUNNEL_STARTUP_TIMEOUT_SECONDS
    TELEGRAM_TUNNEL_RESTART_DELAY_SECONDS: int = TELEGRAM_TUNNEL_RESTART_DELAY_SECONDS
    OPENAI_API_KEY: Optional[str] = OPENAI_API_KEY
    OPENAI_ASSISTANT_ID: Optional[str] = OPENAI_ASSISTANT_ID
    OPENAI_VECTOR_STORE_ID: str = OPENAI_VECTOR_STORE_ID
    OPENAI_SYSTEM_PROMPT: Optional[str] = OPENAI_SYSTEM_PROMPT
    OPENAI_CHAT_MODEL: str = OPENAI_CHAT_MODEL
    DEFAULT_ADMIN_USER: str = DEFAULT_ADMIN_USER
    DEFAULT_ADMIN_PASSWORD: str = DEFAULT_ADMIN_PASSWORD
    VAPID_PUBLIC_KEY: str = VAPID_PUBLIC_KEY
    VAPID_PRIVATE_KEY: str = VAPID_PRIVATE_KEY
    VAPID_CONTACT_EMAIL: str = VAPID_CONTACT_EMAIL
    # === DO NOT DELETE: THAI LEGAL BOT EXPORT FEATURE ===
    TELEGRAM_TOKEN: str = TELEGRAM_TOKEN
    TELEGRAM_CHAT_ID: str = TELEGRAM_CHAT_ID
    ENABLE_TELEGRAM_EXPORT: bool = ENABLE_TELEGRAM_EXPORT
    # === DO NOT DELETE: THAI LEGAL BOT EXPORT FEATURE ===


settings = Settings()


def is_production() -> bool:
    return settings.APP_ENV.strip().lower() not in ("development", "dev", "local", "test")


def assert_production_secrets() -> None:
    """Fail-fast: в проде запрещены небезопасные значения по умолчанию.

    Вызывается на старте приложения. В dev-окружении только предупреждает.
    """
    problems: list[str] = []
    if not settings.JWT_SECRET_KEY or settings.JWT_SECRET_KEY == DEFAULT_JWT_SECRET:
        problems.append("JWT_SECRET_KEY не задан или равен небезопасному значению по умолчанию")
    if not settings.DEFAULT_ADMIN_PASSWORD or settings.DEFAULT_ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD_FALLBACK:
        problems.append("DEFAULT_ADMIN_PASSWORD не задан или равен 'admin'")

    if not problems:
        return

    if is_production():
        raise RuntimeError(
            "Небезопасная конфигурация для production: "
            + "; ".join(problems)
            + ". Задайте переменные окружения и перезапустите сервис."
        )
    for p in problems:
        log.warning("Небезопасная конфигурация (dev): %s", p)
