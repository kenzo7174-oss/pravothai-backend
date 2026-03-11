"""
Axoloti Terminal — dev-автоматизация Telegram webhook через Pinggy tunnel.
"""

import asyncio
import contextlib
import logging
import re
from urllib.parse import urljoin

from app.core.config import settings
from app.services.telegram import get_telegram_webhook_info, set_telegram_webhook

log = logging.getLogger(__name__)

TUNNEL_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9_-]+\.a\.pinggy\.link")


class TelegramDevTunnelManager:
    def __init__(self) -> None:
        self._process = None
        self._runner_task = None
        self._reader_task = None
        self._stop_event = asyncio.Event()
        self._url_ready_event = asyncio.Event()
        self._current_public_url = ""

    async def start(self) -> None:
        if self._runner_task is not None:
            return

        if not settings.TELEGRAM_BOT_TOKEN:
            log.info("Telegram tunnel automation пропущен: не задан TELEGRAM_BOT_TOKEN")
            return

        if settings.APP_ENV.lower() == "production":
            log.info("Telegram tunnel automation отключен в production")
            return

        if not settings.TELEGRAM_WEBHOOK_AUTOMATION_ENABLED:
            log.info("Telegram tunnel automation отключен настройками")
            return

        log.info(
            "Telegram tunnel automation enabled: port=%s path=%s",
            settings.TELEGRAM_TUNNEL_PORT,
            settings.TELEGRAM_WEBHOOK_PATH,
        )
        self._stop_event.clear()
        self._runner_task = asyncio.create_task(self._run(), name="telegram-dev-tunnel")

    async def stop(self) -> None:
        self._stop_event.set()
        await self._stop_process()

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        if self._runner_task is not None:
            self._runner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner_task
            self._runner_task = None

        self._current_public_url = ""
        self._url_ready_event = asyncio.Event()

    async def _run(self) -> None:
        direct_public_url = settings.TELEGRAM_WEBHOOK_PUBLIC_URL.strip().rstrip("/")
        if direct_public_url:
            log.info("Используется статический публичный URL для Telegram webhook: %s", direct_public_url)
            await self._sync_webhook(direct_public_url)
            await self._stop_event.wait()
            return

        restart_delay = max(settings.TELEGRAM_TUNNEL_RESTART_DELAY_SECONDS, 2)

        while not self._stop_event.is_set():
            try:
                await self._start_tunnel_process()
                await self._wait_until_tunnel_ready()
                log.info("Получен публичный URL туннеля: %s", self._current_public_url)
                await self._wait_for_local_backend()
                await self._sync_webhook(self._current_public_url)
                exit_code = await self._process.wait()
                if self._stop_event.is_set():
                    return
                log.warning(
                    "Telegram tunnel неожиданно остановился с кодом %s, переподключение через %sс",
                    exit_code,
                    restart_delay,
                )
            except asyncio.TimeoutError:
                log.error(
                    "Не удалось получить публичный URL от Pinggy за %sс",
                    settings.TELEGRAM_TUNNEL_STARTUP_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Ошибка автоматизации Telegram tunnel: %s", exc)
            finally:
                await self._stop_process()

            await asyncio.sleep(restart_delay)

    async def _start_tunnel_process(self) -> None:
        self._url_ready_event = asyncio.Event()
        self._current_public_url = ""

        command = [
            "ssh",
            "-p", "443",
            "-R", f"0:localhost:{settings.TELEGRAM_TUNNEL_PORT}",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ServerAliveInterval=30",
            "a.pinggy.io",
        ]

        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._reader_task = asyncio.create_task(
            self._read_tunnel_output(self._process),
            name="telegram-dev-tunnel-reader",
        )
        log.info("Запущен Pinggy tunnel для Telegram webhook (pid=%s)", self._process.pid)

    async def _wait_until_tunnel_ready(self) -> None:
        url_task = asyncio.create_task(
            self._url_ready_event.wait(),
            name="telegram-dev-tunnel-url-ready",
        )
        process_wait_task = asyncio.create_task(
            self._process.wait(),
            name="telegram-dev-tunnel-process-wait",
        )

        try:
            done, pending = await asyncio.wait(
                {url_task, process_wait_task},
                timeout=settings.TELEGRAM_TUNNEL_STARTUP_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                raise asyncio.TimeoutError

            if url_task in done and self._current_public_url:
                process_wait_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await process_wait_task
                return

            exit_code = process_wait_task.result()
            raise RuntimeError(
                f"Pinggy tunnel завершился до получения публичного URL (exit_code={exit_code})"
            )
        finally:
            for task in (url_task, process_wait_task):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def _read_tunnel_output(self, process) -> None:
        while True:
            line = await process.stdout.readline()
            if not line:
                break

            text = line.decode("utf-8", errors="ignore").strip()
            if not text:
                continue

            log.info("[telegram tunnel] %s", text)
            public_url = self._extract_public_url(text)
            if public_url and not self._current_public_url:
                self._current_public_url = public_url
                self._url_ready_event.set()

    async def _sync_webhook(self, public_base_url: str) -> None:
        webhook_url = urljoin(f"{public_base_url}/", settings.TELEGRAM_WEBHOOK_PATH.lstrip("/"))
        log.info("Регистрируем Telegram webhook: %s", webhook_url)
        response = await set_telegram_webhook(webhook_url)
        if response is None or not response.get("ok"):
            raise RuntimeError(f"Не удалось обновить Telegram webhook: {webhook_url}")
        info = await get_telegram_webhook_info()
        log.info("Telegram getWebhookInfo response: %s", info)

    async def _wait_for_local_backend(self) -> None:
        host = "127.0.0.1"
        port = settings.TELEGRAM_TUNNEL_PORT
        for attempt in range(1, 11):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=2,
                )
                writer.close()
                await writer.wait_closed()
                log.info(
                    "Локальный backend начал слушать порт перед регистрацией webhook: %s:%s",
                    host,
                    port,
                )
                return
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                log.info(
                    "Ожидание локального backend (%s:%s), попытка %s/10: %s",
                    host,
                    port,
                    attempt,
                    exc,
                )
                await asyncio.sleep(1)

        raise RuntimeError(f"Локальный backend не начал слушать порт {host}:{port}")

    async def _stop_process(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        if self._process is None:
            return

        if self._process.returncode is None:
            log.info("Останавливаем Telegram tunnel (pid=%s)", self._process.pid)
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        self._process = None

    @staticmethod
    def _extract_public_url(text: str) -> str:
        match = TUNNEL_URL_PATTERN.search(text)
        if match:
            return match.group(0).rstrip("/")
        return ""
