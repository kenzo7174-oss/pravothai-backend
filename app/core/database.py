"""
Axoloti Terminal — Асинхронное подключение к БД.

Поддерживает SQLite (aiosqlite) и PostgreSQL (asyncpg).
URL задаётся через переменную окружения DATABASE_URL.
"""

import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./axolotl.db",
)

_is_sqlite = "sqlite" in DATABASE_URL

_connect_args: dict = {}
_engine_kwargs: dict = {"echo": False}

if _is_sqlite:
    _connect_args["check_same_thread"] = False
else:
    # PostgreSQL (Neon): защита от «протухших»/закрытых по простою соединений.
    # pool_pre_ping проверяет соединение перед выдачей; pool_recycle пересоздаёт
    # его раньше, чем сервер успеет закрыть idle-коннект.
    _engine_kwargs.update(
        pool_pre_ping=True,
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "300")),
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
        pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
    )
    # asyncpg: таймаут установления соединения и таймаут команды.
    if "asyncpg" in DATABASE_URL:
        _connect_args["timeout"] = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))
        _connect_args["command_timeout"] = int(os.getenv("DB_COMMAND_TIMEOUT", "30"))

engine = create_async_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    **_engine_kwargs,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    """Dependency-генератор сессии для FastAPI."""
    async with AsyncSessionLocal() as session:
        yield session


def is_postgres() -> bool:
    """Проверка, используется ли PostgreSQL."""
    return "postgresql" in DATABASE_URL
