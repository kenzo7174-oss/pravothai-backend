"""
Экстренный сброс пароля Владельца на '123456'.

Запуск:
    cd backend
    source venv/bin/activate
    python scripts/reset_owner_password.py

Находит владельца (role=owner или admin) и перезаписывает пароль на '123456'.
Использует сырой SQL, чтобы работать даже при устаревшей схеме БД.
"""

import asyncio
import os
import sys

# Добавляем backend в path для импортов
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core.database import engine
from app.core.auth import hash_password


async def reset_owner_password() -> None:
    new_password = "123456"
    hashed = hash_password(new_password)

    async with engine.begin() as conn:
        # Используем сырой SQL — не зависит от полной схемы ORM
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
        # Обновляем только hashed_password (совместимо со старой схемой)
        await conn.execute(
            text("UPDATE operators SET hashed_password = :h WHERE id = :id"),
            {"h": hashed, "id": op_id}
        )
        print(f"✅ Пароль владельца '{username}' (id={op_id}) сброшен на: {new_password}")


async def main() -> None:
    try:
        await reset_owner_password()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
