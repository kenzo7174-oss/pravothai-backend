"""
Axoloti Terminal — JWT-аутентификация.

Утилиты для хеширования паролей, создания и верификации JWT-токенов,
а также FastAPI-зависимость для защиты маршрутов.
"""

from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    )
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_setup_token(username: str) -> str:
    """Краткосрочный токен для первичной установки пароля (5 мин)."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=5)
    payload = {"sub": username, "purpose": "password_setup", "exp": expire}
    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_setup_token(token: str) -> str | None:
    """Проверяет setup-токен и возвращает username или None."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        if payload.get("purpose") != "password_setup":
            return None
        return payload.get("sub")
    except JWTError:
        return None


async def get_current_operator(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
):
    """FastAPI dependency — extracts and validates the JWT Bearer token.

    Returns the Operator ORM object or raises 401.
    Updates last_active on every authenticated request so is_online stays accurate.
    """
    from app.models import Operator  # deferred to avoid circular import

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        username: str | None = payload.get("sub")
        if username is None:
            raise JWTError("missing sub")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    result = await session.execute(
        select(Operator).where(Operator.username == username)
    )
    operator = result.scalar_one_or_none()
    if operator is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Operator not found",
        )
    is_active = getattr(operator, "is_active", True)
    if is_active is None:
        is_active = True  # fallback при NULL в БД
    if not is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ запрещён",
        )

    if hasattr(operator, "last_active"):
        operator.last_active = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()

    return operator
