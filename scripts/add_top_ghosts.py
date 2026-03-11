import asyncio
import random
from datetime import datetime
from app.core.database import AsyncSessionLocal
from app.models import Client, Conversation, Message

async def add_top_ghosts():
    async with AsyncSessionLocal() as session:
        print("👻 Добавляем 2 свежих анонимов наверх...")

        for i in range(2):
            uid = random.randint(1000, 9999)
            
            # 1. Клиент
            client = Client(
                name=f"Посетитель #{uid}",
                avatar="",
                phone="",
                notes="Только что написал",
                tags="fresh"
            )
            session.add(client)
            await session.commit()
            await session.refresh(client)

            # 2. Диалог
            chat = Conversation(
                client_id=client.id,
                source="livechat",
                social_id=f"guest_{uid}_now",
                label="Онлайн"
            )
            session.add(chat)
            await session.commit()
            await session.refresh(chat)

            # 3. Сообщение (ВРЕМЯ = datetime.utcnow() = СЕЙЧАС)
            msg = Message(
                conversation_id=chat.id,
                content="Менеджер тут? Срочный вопрос.",
                sender="client",
                created_at=datetime.utcnow(), # Это поднимет их в топ
                is_read=False
            )
            session.add(msg)
            
        await session.commit()
        print("✅ Готово! Обнови страницу — анонимы должны быть первыми.")

if __name__ == "__main__":
    asyncio.run(add_top_ghosts())