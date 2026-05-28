"""
Web Push — отправка уведомлений операторам (мобильные устройства).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Iterable

from pywebpush import WebPushException, webpush
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import Operator, PushSubscription

log = logging.getLogger(__name__)

_STALE_STATUS_CODES = frozenset({404, 410})
_DEFAULT_VIBRATE = [200, 100, 200, 100, 200]
_MAX_BODY_LEN = 200


def _vapid_configured() -> bool:
    return bool((settings.VAPID_PUBLIC_KEY or "").strip() and (settings.VAPID_PRIVATE_KEY or "").strip())


def _build_payload(
    title: str,
    body: str,
    *,
    url: str = "/",
    tag: str | None = None,
    require_interaction: bool = False,
) -> str:
    payload = {
        "title": title,
        "body": (body or "").strip()[:_MAX_BODY_LEN] or "Уведомление",
        "url": url or "/",
        "tag": tag or "axoloti-push",
        "sound": "default",
        "vibrate": _DEFAULT_VIBRATE,
        "requireInteraction": require_interaction,
    }
    return json.dumps(payload, ensure_ascii=False)


def _send_webpush_sync(subscription: PushSubscription, payload_json: str) -> None:
    webpush(
        subscription_info={
            "endpoint": subscription.endpoint,
            "keys": {
                "p256dh": subscription.p256dh,
                "auth": subscription.auth,
            },
        },
        data=payload_json,
        vapid_private_key=settings.VAPID_PRIVATE_KEY,
        vapid_claims={"sub": settings.VAPID_CONTACT_EMAIL or "mailto:admin@axoloti.ru"},
    )


async def _resolve_operator_ids(session, operator_id: int | None) -> list[int]:
    if operator_id is not None:
        return [operator_id]

    result = await session.execute(
        select(Operator.id).where(Operator.is_active.is_(True))
    )
    return list(result.scalars().all())


async def send_push_notification(
    operator_id: int | None,
    title: str,
    body: str,
    *,
    url: str = "/",
    tag: str | None = None,
    require_interaction: bool = False,
) -> int:
    """Отправляет Web Push одному оператору или всем активным операторам.

    operator_id=None — рассылка всем активным операторам с push-подписками.
    Возвращает число успешно доставленных уведомлений.
    """
    if not _vapid_configured():
        log.debug("VAPID keys not configured, skip push")
        return 0

    payload_json = _build_payload(
        title,
        body,
        url=url,
        tag=tag,
        require_interaction=require_interaction,
    )

    sent = 0
    stale_ids: list[int] = []

    async with AsyncSessionLocal() as session:
        operator_ids = await _resolve_operator_ids(session, operator_id)
        if not operator_ids:
            return 0

        result = await session.execute(
            select(PushSubscription).where(PushSubscription.operator_id.in_(operator_ids))
        )
        subscriptions: Iterable[PushSubscription] = result.scalars().all()

        for sub in subscriptions:
            try:
                await asyncio.to_thread(_send_webpush_sync, sub, payload_json)
                sent += 1
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in _STALE_STATUS_CODES:
                    log.info("Removing stale push subscription id=%s status=%s", sub.id, status)
                    stale_ids.append(sub.id)
                else:
                    log.warning("Web push failed subscription id=%s: %s", sub.id, exc)
            except Exception as exc:
                log.warning("Web push error subscription id=%s: %s", sub.id, exc)

        if stale_ids:
            await session.execute(
                delete(PushSubscription).where(PushSubscription.id.in_(stale_ids))
            )
            await session.commit()

    return sent


async def push_specialist_requested(conversation_id: int, operator_id: int | None = None) -> None:
    await send_push_notification(
        operator_id,
        "Axoloti Terminal",
        "Требуется помощь оператора",
        tag=f"specialist-{conversation_id}",
        require_interaction=True,
    )


async def push_new_message(
    operator_id: int | None,
    message_text: str,
    conversation_id: int,
) -> None:
    body = (message_text or "").strip()[:_MAX_BODY_LEN] or "Вам написали"
    await send_push_notification(
        operator_id,
        "Новое сообщение",
        body,
        tag=f"message-{conversation_id}",
    )


async def push_new_conversation(conversation_id: int, operator_id: int | None = None) -> None:
    await send_push_notification(
        operator_id,
        "Новый диалог",
        "Подключился новый посетитель",
        tag=f"conversation-{conversation_id}",
    )


def schedule_push(coro) -> None:
    """Запускает push в фоне, не блокируя HTTP-ответ."""

    async def _runner():
        try:
            await coro
        except Exception:
            log.exception("Background push task failed")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        asyncio.run(_runner())
