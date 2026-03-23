"""
Экстренный сброс пароля Владельца и установка link.

Запуск:
    cd backend
    source venv/bin/activate
    python scripts/reset_owner_password.py

Находит владельца (role=owner или admin), устанавливает:
- link = https://axolotl-backend.onrender.com
- пароль = 123456 (с хешированием)
"""

import asyncio
import os
import sys

# Добавляем backend в path для импортов
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core.database import engine, is_postgres
from app.core.auth import hash_password

OWNER_LINK = "https://axolotl-backend.onrender.com"
NEW_PASSWORD = "123456"


async def _ensure_link_column(conn):
    """Добавляет колонку link, если её нет."""
    if is_postgres():
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'operators'"
            )
        )
        columns = {row[0] for row in result.fetchall()}
    else:
        result = await conn.execute(text("PRAGMA table_info(operators)"))
        columns = {row[1] for row in result.fetchall()}
    if "link" not in columns:
        await conn.execute(text(
            "ALTER TABLE operators ADD COLUMN link VARCHAR(500)"
        ))


async def reset_owner_password() -> None:
    hashed = hash_password(NEW_PASSWORD)

    async with engine.begin() as conn:
        await _ensure_link_column(conn)

        result = await conn.execute(
            text("""
                SELECT id, username FROM operators
                WHERE role IN ('owner', 'admin')
                ORDER BY id ASC
                LIMIT 1
            """)
        )
        row = result.fetchone()
        if not row:
            print("❌ Владелец (owner/admin) не найден. Запустите приложение — при первом старте будет создан admin.")
            return

        op_id, username = row[0], row[1]
        await conn.execute(
            text("""
                UPDATE operators
                SET hashed_password = :h, link = :link
                WHERE id = :id
            """),
            {"h": hashed, "link": OWNER_LINK, "id": op_id}
        )
        print(f"✅ Владелец '{username}' (id={op_id}): link={OWNER_LINK}, пароль={NEW_PASSWORD}")


async def main() -> None:
    try:
        await reset_owner_password()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
