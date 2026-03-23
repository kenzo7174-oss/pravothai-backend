"""
Миграция: добавление колонки job_title в таблицу operators.

Запуск (из корня backend):
    python -m scripts.add_job_title_column

Для существующих БД без Alembic. После применения можно удалить этот скрипт.
"""

import asyncio
from sqlalchemy import text
from app.core.database import engine, DATABASE_URL


async def migrate() -> None:
    async with engine.begin() as conn:
        if "sqlite" in DATABASE_URL:
            await conn.execute(text("ALTER TABLE operators ADD COLUMN job_title VARCHAR(120)"))
        else:
            await conn.execute(text("ALTER TABLE operators ADD COLUMN IF NOT EXISTS job_title VARCHAR(120)"))
    print("✅ Колонка job_title добавлена в operators.")


async def main() -> None:
    try:
        await migrate()
    except Exception as e:
        if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
            print("ℹ️  Колонка job_title уже существует.")
        else:
            raise
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
