"""
Аварийный сброс пароля оператора напрямую в базе данных.

Запуск:
    cd backend
    source venv/bin/activate
    python reset_password.py

Читает DATABASE_URL из .env (или переменных окружения),
запрашивает email (логин) и новый пароль, хэширует и обновляет запись в operators.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent


def load_dotenv() -> None:
    """Загружает переменные из backend/.env, если файл существует."""
    env_path = BACKEND_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.auth import hash_password  # noqa: E402
from app.core.database import AsyncSessionLocal, engine  # noqa: E402
from app.models import Operator  # noqa: E402


async def reset_password() -> None:
    email = input("Email пользователя: ").strip()
    if not email:
        print("❌ Email не может быть пустым.")
        return

    new_password = getpass.getpass("Новый пароль: ")
    confirm_password = getpass.getpass("Повторите пароль: ")
    if not new_password:
        print("❌ Пароль не может быть пустым.")
        return
    if new_password != confirm_password:
        print("❌ Пароли не совпадают.")
        return
    if len(new_password) < 4:
        print("❌ Пароль должен быть не менее 4 символов.")
        return

    hashed = hash_password(new_password)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Operator).where(Operator.username == email)
        )
        operator = result.scalar_one_or_none()
        if operator is None:
            print(f"❌ Пользователь '{email}' не найден.")
            return

        operator.hashed_password = hashed
        operator.needs_password_setup = False
        await session.commit()
        print(f"✅ Пароль пользователя '{email}' (id={operator.id}) успешно обновлён.")


async def main() -> None:
    try:
        await reset_password()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
