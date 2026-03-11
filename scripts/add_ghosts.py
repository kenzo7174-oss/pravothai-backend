import asyncio
import random
from datetime import datetime, timedelta
from app.core.database import AsyncSessionLocal
from app.models import Client, Conversation, Message

async def create_ghosts():
    async with AsyncSessionLocal() as session:
        print("👻 Выпускаем призраков...")
        
        for i in range(5):
            # Генерируем ID сессии
            session_id = random.randint(10000, 99999)
            
            # В реальных системах имя часто None, но для удобства пишем плейсхолдер
            client = Client(
                name=f"Посетитель #{session_id}", 
                avatar="", 
                phone="", # Телефона нет
                notes="Анонимный чат с виджета",
                tags="инкогнито"
            )
            session.add(client)
            await session.commit()
            await session.refresh(client)
            
            # Время (случайное за последние сутки)
            offset = random.randint(10, 1400)
            created_at = datetime.utcnow() - timedelta(minutes=offset)

            # Создаем диалог
            chat = Conversation(
                client_id=client.id,
                source="livechat", # Источник - чат на сайте
                social_id=f"guest_{session_id}",
                label="Вопрос с сайта"
            )
            session.add(chat)
            await session.commit()
            await session.refresh(chat)

            # Первое сообщение (обычно вопрос о цене или доставке)
            msgs = [
                "Здравствуйте, сколько стоит доставка?",
                "А есть в наличии?",
                "Мне нужен менеджер",
                "Не могу оформить заказ",
                "Цена актуальна?"
            ]
            
            msg = Message(
                conversation_id=chat.id,
                content=random.choice(msgs),
                sender="client",
                created_at=created_at,
                is_read=False
            )
            session.add(msg)
            
        await session.commit()
        print("✅ 5 анонимов добавлены в базу.")

if __name__ == "__main__":
    asyncio.run(create_ghosts())