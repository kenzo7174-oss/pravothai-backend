import asyncio
import random
from datetime import datetime, timedelta
from app.core.database import AsyncSessionLocal
from app.models import Client, Conversation, Message

# 1. ДВЕ ГРУППЫ (СТОПКИ)
GROUPS = [
    {"name": "Ева", "count": 3, "offset": 5},   # Будет наверху (3 чата)
    {"name": "Хабиб", "count": 2, "offset": 120} # Будет ниже (2 чата)
]

# 2. ИМЕННЫЕ (ОДИНОЧНЫЕ)
NAMED_USERS = ["Матвей", "Ольга", "Дмитрий"]

# 3. АНОНИМЫ
GHOST_COUNT = 3

async def fill_data():
    async with AsyncSessionLocal() as session:
        print("🚀 Начинаю финальную заливку...")

        # --- СОЗДАЕМ ГРУППЫ ---
        for g in GROUPS:
            client = Client(name=g["name"], phone="+7999...", notes="VIP", tags="group")
            session.add(client)
            await session.commit()
            await session.refresh(client)
            
            for i in range(g["count"]):
                chat = Conversation(
                    client_id=client.id, 
                    source="whatsapp", 
                    social_id=f"@{client.name}_{i}", 
                    label=f"Заказ {i+1}"
                )
                session.add(chat)
                await session.commit()
                await session.refresh(chat)
                
                # Сообщение
                msg = Message(
                    conversation_id=chat.id,
                    content=f"Привет от {client.name}, чат {i+1}",
                    sender="client",
                    created_at=datetime.utcnow() - timedelta(minutes=g["offset"] + i*10)
                )
                session.add(msg)
        
        # --- СОЗДАЕМ ИМЕННЫХ ---
        for name in NAMED_USERS:
            client = Client(name=name, phone="+7900...", notes="Клиент", tags="new")
            session.add(client)
            await session.commit()
            await session.refresh(client)
            
            chat = Conversation(client_id=client.id, source="telegram", social_id="@user", label="Вопрос")
            session.add(chat)
            await session.commit()
            await session.refresh(chat)
            
            msg = Message(
                conversation_id=chat.id,
                content="Здравствуйте, есть вопрос.",
                sender="client",
                created_at=datetime.utcnow() - timedelta(minutes=random.randint(20, 300))
            )
            session.add(msg)

        # --- СОЗДАЕМ АНОНИМОВ ---
        for i in range(GHOST_COUNT):
            uid = random.randint(1000,9999)
            client = Client(name=f"Посетитель #{uid}", phone="", notes="Инкогнито", tags="ghost")
            session.add(client)
            await session.commit()
            await session.refresh(client)
            
            chat = Conversation(client_id=client.id, source="livechat", social_id=f"guest_{uid}", label="С сайта")
            session.add(chat)
            await session.commit()
            await session.refresh(chat)
            
            msg = Message(
                conversation_id=chat.id,
                content="Сколько стоит доставка?",
                sender="client",
                created_at=datetime.utcnow() - timedelta(minutes=random.randint(10, 500))
            )
            session.add(msg)

        await session.commit()
        print("✅ БАЗА ГОТОВА: 2 группы, 3 именных, 3 анонима.")

if __name__ == "__main__":
    asyncio.run(fill_data())