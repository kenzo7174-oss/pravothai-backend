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

_connect_args = {}
if "sqlite" in DATABASE_URL:
    _connect_args["check_same_thread"] = False

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=_connect_args if _connect_args else {},
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
