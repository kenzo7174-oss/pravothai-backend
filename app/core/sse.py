"""
Менеджер SSE-подключений для мгновенной рассылки событий операторам.

Используется при срабатывании request_human_operator — рассылает событие
chat_updated, чтобы фронтенд сразу обновил данные.
"""

import asyncio
import json
import logging

log = logging.getLogger(__name__)


class SSEManager:
    """Хранит активные SSE-подключения и рассылает события."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []

    def add_client(self) -> asyncio.Queue:
        """Добавляет клиента, возвращает очередь для чтения."""
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        log.debug("SSE client connected, total=%d", len(self._queues))
        return q

    def remove_client(self, q: asyncio.Queue) -> None:
        """Удаляет клиента."""
        if q in self._queues:
            self._queues.remove(q)
        log.debug("SSE client disconnected, total=%d", len(self._queues))

    async def broadcast(self, event: str, data: dict | None = None) -> None:
        """Рассылает событие всем подключённым клиентам."""
        payload = json.dumps({"event": event, "data": data or {}})
        for q in list(self._queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                self._queues.remove(q)


sse_manager = SSEManager()
