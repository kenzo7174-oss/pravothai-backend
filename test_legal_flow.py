import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import httpx
from openai import AsyncOpenAI

from app.services import openai_service


def response_fixture(answered=True, evidence=None, results=True):
    text = "Оверстей: штраф 500 бат в день, максимум 20 000 бат."
    return {
        "id": "resp_test", "object": "response", "created_at": 0,
        "status": "completed", "model": "gpt-4o",
        "output": ([{"id": "fs_test", "type": "file_search_call",
                     "status": "completed", "queries": ["оверстей"],
                     "results": [{"file_id": "file_test", "filename": "laws.txt",
                                  "score": 0.9, "text": text}]}] if results else []) + [
            {"id": "msg_test", "type": "message", "role": "assistant",
             "status": "completed", "content": [{"type": "output_text",
                "text": json.dumps({"answered": answered, "message": "Штраф — 500 бат в день.",
                                    "evidence": evidence if evidence is not None else [text]}),
                "annotations": []}]}],
    }


class LegalAnswers(unittest.IsolatedAsyncioTestCase):
    async def ask(self, fixture):
        def transport(request):
            if request.url.path == "/v1/responses":
                return httpx.Response(200, json=fixture)
            return httpx.Response(200, json={"id": "chat_test", "object": "chat.completion",
                "created": 0, "model": "gpt-4o", "choices": [{"index": 0,
                "finish_reason": "stop", "message": {"role": "assistant",
                "content": '{"message":"Legacy answer without knowledge search","request_operator":false}'}}]})
        client = AsyncOpenAI(api_key="test-only", max_retries=0,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        with patch.object(openai_service.settings, "OPENAI_VECTOR_STORE_ID", "vs_test", create=True), \
             patch.object(openai_service.settings, "OPENAI_API_KEY", "test-only"), \
             patch.object(openai_service, "get_openai_client", return_value=client):
            raw = await openai_service._run_assistant_on_history([("user", "Штраф за оверстей?")])
            answer = openai_service._parse_assistant_output(raw)
        await client.close()
        return answer

    async def test_supported_answer_is_shown_without_transfer(self):
        self.assertEqual(await self.ask(response_fixture()), ("Штраф — 500 бат в день.", False))

    async def test_no_explicit_answer_offers_contact_without_automatic_transfer(self):
        reply, transfer = await self.ask(response_fixture(answered=False))
        self.assertIn("имя", reply.lower())
        self.assertIn("телефон", reply.lower())
        self.assertFalse(transfer)

    async def test_answer_without_search_results_is_not_shown(self):
        reply, _ = await self.ask(response_fixture(results=False))
        self.assertNotIn("Штраф —", reply)
        self.assertIn("имя", reply.lower())

    async def test_fabricated_evidence_is_not_shown(self):
        reply, _ = await self.ask(response_fixture(evidence=["Несуществующее правило"]))
        self.assertNotIn("Штраф —", reply)
        self.assertIn("имя", reply.lower())


class ContactSubmission(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.core.database import Base, get_session
        from app.main import app
        self.app = app
        self.get_session = get_session
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async def session_dependency():
            async with self.sessions() as session:
                yield session
        app.dependency_overrides[get_session] = session_dependency
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        self.app.dependency_overrides.pop(self.get_session, None)
        await self.client.aclose()
        await self.engine.dispose()

    async def test_contact_saved_to_crm_and_ai_stopped(self):
        from sqlalchemy import select
        from app import main
        from app.models import Client, Conversation, Message
        with patch.object(main, "_fetch_geolocation", return_value=""), \
             patch.object(main, "generate_draft", side_effect=AssertionError("Contact must not invoke AI")), \
             patch.object(main, "AsyncSessionLocal", self.sessions), \
             patch.object(main, "schedule_push", side_effect=lambda coroutine: coroutine.close()), \
             patch.object(main, "restart_export_timer"), \
             patch("app.services.thai_legal_tg_export.AsyncSessionLocal", self.sessions), \
             patch("app.services.thai_legal_tg_export._send_to_telegram", new_callable=AsyncMock, return_value=True) as telegram_send, \
             patch.object(main.settings, "TELEGRAM_TOKEN", "test-token"), \
             patch.object(main.settings, "TELEGRAM_CHAT_ID", "test-chat"):
            result = await self.client.post("/api/v1/webhooks/web", json={
                "client_id": "visitor-contact-test", "contact": {
                    "name": "Анна", "phone": "+66 800000001", "email": "anna@example.com",
                    "question": "Помогите с моим договором аренды"}})
            retry = await self.client.post("/api/v1/webhooks/web", json={
                "client_id": "visitor-contact-test", "contact": {
                    "name": "Анна", "phone": "+66 800000001", "email": "anna@example.com",
                    "question": "Помогите с моим договором аренды"}})
        telegram_send.assert_awaited_once()
        sent_text = telegram_send.await_args.args[0]
        for value in ("Анна", "+66 800000001", "anna@example.com", "договором аренды"):
            self.assertIn(value, sent_text)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["contact_received"])
        async with self.sessions() as session:
            client = (await session.execute(select(Client))).scalar_one()
            conv = (await session.execute(select(Conversation))).scalar_one()
            messages = (await session.execute(select(Message))).scalars().all()
            self.assertEqual((client.name, client.phone, client.email),
                             ("Анна", "+66 800000001", "anna@example.com"))
            self.assertEqual(conv.intercept_mode, "manual")
            self.assertTrue(conv.specialist_requested)
            self.assertTrue(any("договором аренды" in m.content for m in messages))
            self.assertTrue(next(m for m in messages if m.sender == "client").is_exported_to_tg)
            self.assertEqual(len(messages), 2, "Retry after a lost response must not duplicate the request")
            self.assertEqual(result.json()["message_id"], retry.json()["message_id"])

    async def test_telegram_failure_keeps_request_and_retries_after_restart(self):
        from sqlalchemy import select
        from app import main
        from app.models import Message
        from app.services import thai_legal_tg_export as telegram
        with patch.object(main, "_fetch_geolocation", return_value=""), \
             patch.object(main, "AsyncSessionLocal", self.sessions), \
             patch.object(main, "schedule_push", side_effect=lambda coroutine: coroutine.close()), \
             patch.object(main, "restart_export_timer"), \
             patch.object(telegram, "AsyncSessionLocal", self.sessions), \
             patch.object(main.settings, "TELEGRAM_TOKEN", "test-token"), \
             patch.object(main.settings, "TELEGRAM_CHAT_ID", "test-chat"), \
             patch.object(main.settings, "ENABLE_TELEGRAM_EXPORT", False), \
             patch.object(telegram, "_send_to_telegram", new_callable=AsyncMock, return_value=False) as sender:
            result = await self.client.post("/api/v1/webhooks/web", json={
                "client_id": "telegram-failure-test", "contact": {
                    "name": "Анна", "phone": "+66 800000001", "email": "anna@example.com",
                    "question": "Вопрос"}})
            self.assertTrue(result.json()["contact_received"])
            async with self.sessions() as session:
                message = await session.scalar(select(Message).where(Message.sender == "client"))
                self.assertFalse(message.is_exported_to_tg)
            sender.return_value = True
            await telegram.thai_legal_tg_export_loop()
            self.assertEqual(sender.await_count, 2)
            async with self.sessions() as session:
                message = await session.scalar(select(Message).where(Message.sender == "client"))
                self.assertTrue(message.is_exported_to_tg)

    async def test_invalid_contact_does_not_create_crm_records(self):
        from sqlalchemy import select, func
        from app.models import Client
        result = await self.client.post("/api/v1/webhooks/web", json={
            "client_id": "visitor-invalid", "contact": {
                "name": " ", "phone": "abc", "email": "invalid", "question": "Вопрос"}})
        self.assertEqual(result.status_code, 422)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Client)), 0)


if __name__ == "__main__":
    unittest.main()
