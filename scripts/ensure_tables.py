#!/usr/bin/env python3
"""
Создание всех таблиц БД (включая scheduled_broadcasts).
Запуск перед деплоем на Render, если таблицы ещё не созданы:

    cd backend
    python -m scripts.ensure_tables
"""
import asyncio
import sys

# Регистрируем все модели в Base.metadata
from app.models import (  # noqa: F401
    Client,
    Conversation,
    Message,
    Operator,
    ScheduledBroadcast,
    SystemSettings,
)
from app.core.database import engine, Base


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("OK: все таблицы созданы (включая scheduled_broadcasts)")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
