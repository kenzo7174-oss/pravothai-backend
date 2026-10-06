import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import httpx
from openai import AsyncOpenAI

from app.services import openai_service


def response_fixture(decision="answer", source="https://www.mfa.go.th/en/content/visa", results=True, listed=None):
    return {
        "id": "resp_test", "object": "response", "created_at": 0,
        "status": "completed", "model": "gpt-4o",
        "output": ([{"id": "ws_test", "type": "web_search_call",
                     "status": "completed", "action": {"type": "search", "query": "Thai visa",
                     "sources": [{"type": "url", "url": listed or source}]}}] if results else []) + [
            {"id": "msg_test", "type": "message", "role": "assistant",
             "status": "completed", "content": [{"type": "output_text",
                "text": json.dumps({"decision": decision,
                    "message": "У вас паспорт какой страны?" if decision == "clarify" else "Краткий ответ по официальному источнику.",
                    "sources": [source] if decision == "answer" else []}),
                "annotations": []}]}],
    }


def route_fixture(route):
    result = response_fixture(results=False)
    result["output"][0]["content"][0]["text"] = json.dumps({"route": route})
    return result


class LegalAnswers(unittest.IsolatedAsyncioTestCase):
    async def ask(self, fixture):
        def transport(request):
            if request.url.path == "/v1/responses":
                request_body = json.loads(request.content)
                if request_body["text"]["format"]["name"] == "question_route":
                    return httpx.Response(200, json=route_fixture("legal"))
                self.assertEqual(request_body["tools"][0]["type"], "web_search")
                self.assertIn("mfa.go.th", request_body["tools"][0]["filters"]["allowed_domains"])
                self.assertEqual(request_body["tool_choice"], "required")
                return httpx.Response(200, json=fixture)
            raise AssertionError("Legal questions must use Responses with official web search")
        client = AsyncOpenAI(api_key="test-only", max_retries=0,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        with patch.object(openai_service.settings, "OPENAI_VECTOR_STORE_ID", "vs_test", create=True), \
             patch.object(openai_service.settings, "OPENAI_API_KEY", "test-only"), \
             patch.object(openai_service, "get_openai_client", return_value=client):
            raw = await openai_service._run_assistant_on_history([("user", "Сколько дней для туриста?")])
            answer = openai_service._parse_assistant_output(raw)
        await client.close()
        return answer

    async def test_supported_answer_has_verified_official_link(self):
        reply, transfer = await self.ask(response_fixture())
        self.assertIn("Краткий ответ", reply)
        self.assertIn("https://www.mfa.go.th/en/content/visa", reply)
        self.assertFalse(transfer)

    async def test_simple_ambiguous_question_asks_clarification_without_form(self):
        from app.services.legal_knowledge import needs_contact_form
        reply, transfer = await self.ask(response_fixture(decision="clarify"))
        self.assertEqual(reply, "У вас паспорт какой страны?")
        self.assertFalse(needs_contact_form(reply))
        self.assertFalse(transfer)

    async def test_visa_clarification_uses_clear_fixed_question(self):
        reply, _ = await self.ask(response_fixture(decision="clarify_entry"))
        self.assertEqual(reply, "Речь о безвизовом въезде или туристической визе TR?")

    async def test_complex_or_unconfirmed_question_offers_representative(self):
        from app.services.legal_knowledge import needs_contact_form
        reply, transfer = await self.ask(response_fixture(decision="contact"))
        self.assertTrue(needs_contact_form(reply))
        self.assertIn("представитель", reply)
        self.assertFalse(transfer)

    async def test_answer_without_search_results_is_not_shown(self):
        from app.services.legal_knowledge import needs_contact_form
        reply, _ = await self.ask(response_fixture(results=False))
        self.assertTrue(needs_contact_form(reply))

    async def test_unofficial_lookalike_source_is_not_shown(self):
        from app.services.legal_knowledge import needs_contact_form
        reply, _ = await self.ask(response_fixture(source="https://mfa.go.th.example.com/visa"))
        self.assertTrue(needs_contact_form(reply))

    async def test_invented_official_url_is_not_shown(self):
        from app.services.legal_knowledge import needs_contact_form
        reply, _ = await self.ask(response_fixture(listed="https://www.mfa.go.th/en/content/other"))
        self.assertTrue(needs_contact_form(reply))


class GeneralAnswers(unittest.IsolatedAsyncioTestCase):
    async def test_general_question_uses_unrestricted_search_without_contact_form(self):
        from app.services.legal_knowledge import generate_legal_reply, needs_contact_form
        seen = []
        def transport(request):
            body = json.loads(request.content)
            seen.append(body)
            if body.get("text", {}).get("format", {}).get("name") == "question_route":
                return httpx.Response(200, json=route_fixture("general"))
            self.assertEqual(body["tool_choice"], "auto")
            self.assertNotIn("filters", body["tools"][0])
            self.assertIn("только о повседневной жизни и поездках в Таиланде", body["instructions"])
            result = response_fixture(results=False)
            result["output"][0]["content"][0]["text"] = "Здравствуйте! Чем помочь в Таиланде?"
            return httpx.Response(200, json=result)
        client = AsyncOpenAI(api_key="test-only", max_retries=0,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        reply = json.loads(await generate_legal_reply(client, [("user", "Привет!")]))["message"]
        await client.close()
        self.assertEqual(len(seen), 2)
        self.assertFalse(needs_contact_form(reply))
        self.assertIn("Здравствуйте", reply)

    async def test_router_failure_falls_back_to_verified_legal_mode(self):
        from app.services.legal_knowledge import generate_legal_reply
        def transport(request):
            body = json.loads(request.content)
            if body.get("text", {}).get("format", {}).get("name") == "question_route":
                return httpx.Response(503, json={"error": {"message": "Test routing failure"}})
            self.assertEqual(body["tool_choice"], "required")
            self.assertIn("mfa.go.th", body["tools"][0]["filters"]["allowed_domains"])
            return httpx.Response(200, json=response_fixture())
        client = AsyncOpenAI(api_key="test-only", max_retries=0,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        reply = json.loads(await generate_legal_reply(client, [("user", "Какие штрафы?")]))["message"]
        await client.close()
        self.assertIn("Официальные источники", reply)


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


class DialogueExport(unittest.IsolatedAsyncioTestCase):
    async def test_restarting_idle_timer_keeps_replacement_registered(self):
        from app.services import thai_legal_tg_export as export
        try:
            with patch.object(export, "_export_enabled", return_value=True):
                export.restart_export_timer(999)
                first = export.active_timers[999]
                await asyncio.sleep(0)
                export.restart_export_timer(999)
                second = export.active_timers[999]
                await asyncio.gather(first, return_exceptions=True)
                self.assertIs(export.active_timers.get(999), second)
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
            export.active_timers.pop(999, None)

    async def test_dialogue_without_contact_exports_questions_and_answers_once(self):
        from app.services import thai_legal_tg_export as export
        from datetime import datetime
        messages = [SimpleNamespace(sender=sender, content=text, is_internal=False,
                    is_exported_to_tg=False, created_at=datetime(2026, 10, 6))
                    for sender, text in [("client", "Какая погода на Пхукете?"), ("bot", "Ответ бота")]]
        client = SimpleNamespace(axolotl_visitor_id="visitor-test", location=None,
                                 ip=None, os_device=None, browser=None)
        conv = SimpleNamespace(id=1, client=client, messages=messages)
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        with patch.object(export, "_send_to_telegram", new_callable=AsyncMock, return_value=True) as send:
            await export._process_single_conversation(session, conv)
            await export._process_single_conversation(session, conv)
            send.assert_awaited_once()
            text = send.call_args.args[0]
            self.assertIn("Какая погода на Пхукете?", text)
            self.assertIn("Ответ бота", text)
            self.assertTrue(all(m.is_exported_to_tg for m in messages))

    async def test_failed_dialogue_export_keeps_messages_pending(self):
        from app.services import thai_legal_tg_export as export
        message = SimpleNamespace(sender="client", content="Привет", is_internal=False,
                                  is_exported_to_tg=False, created_at=None)
        client = SimpleNamespace(axolotl_visitor_id="visitor-test", location=None,
                                 ip=None, os_device=None, browser=None)
        conv = SimpleNamespace(id=1, client=client, messages=[message])
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        with patch.object(export, "_send_to_telegram", new_callable=AsyncMock, return_value=False):
            await export._process_single_conversation(session, conv)
        self.assertFalse(message.is_exported_to_tg)
        session.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
