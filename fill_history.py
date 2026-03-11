import asyncio
import random
from datetime import datetime, timedelta
from app.core.database import AsyncSessionLocal
from app.models import Client, Conversation, Message
from sqlalchemy import select

# Сценарии: (Кто пишет, Текст сообщения)
# client = Клиент
# assistant = ИИ (Ева)
SCENARIOS = {
    "visitor": [ # Вопросы с сайта
        [
            ("client", "Здравствуйте, сколько стоит доставка в Казань?"),
            ("assistant", "Добрый день! Доставка в Казань стоит 500р, срок 2-3 дня. Оформить?"),
            ("client", "А самовывоз есть?"),
            ("assistant", "Да, у нас есть пункт выдачи на ул. Пушкина, д. 10. Работаем с 9 до 21."),
            ("client", "Супер, спасибо. Сейчас оформлю.")
        ],
        [
            ("client", "У вас есть скидки для новых клиентов?"),
            ("assistant", "Здравствуйте! Да, по промокоду WELCOME скидка 5% на первый заказ."),
            ("client", "Куда его вводить?"),
            ("assistant", "В корзине при оформлении заказа есть поле 'Промокод'.")
        ]
    ],
    "vip": [ # Постоянные клиенты
        [
            ("client", "Привет! Когда будет отгрузка по заказу #4820?"),
            ("assistant", "Ева, привет! Вижу твой заказ. Машина уже выехала, ожидай завтра к обеду."),
            ("client", "Отлично. Документы положили?"),
            ("assistant", "Да, накладная и счет-фактура в коробке №1."),
            ("client", "Спасибо, вы лучшие! ❤️")
        ]
    ],
    "urgent": [ # Проблемы
        [
            ("client", "Срочно! У меня приложение не открывается!"),
            ("assistant", "Хабиб, спокойствие. Какую ошибку пишет система?"),
            ("client", "Пишет 'Error 500'. Скрин скинул."),
            ("assistant", "Вижу. Это сбой на сервере. Передала техникам, починим в течение 10 минут."),
            ("client", "Жду. Клиенты нервничают.")
        ]
    ]
}

async def fill_history():
    async with AsyncSessionLocal() as session:
        print("🎭 Начинаем писать историю диалогов...")

        # Получаем всех клиентов
        result = await session.execute(select(Client))
        clients = result.scalars().all()

        for client in clients:
            # Ищем чаты клиента
            chat_res = await session.execute(select(Conversation).where(Conversation.client_id == client.id))
            chats = chat_res.scalars().all()

            if not chats:
                continue

            # Подбираем роль по имени
            role = "visitor" # По умолчанию
            name_lower = client.name.lower()
            
            if "посетитель" in name_lower or "guest" in name_lower:
                role = "visitor"
            elif any(n in name_lower for n in ["ева", "анна", "мария"]):
                role = "vip"
            elif any(n in name_lower for n in ["хабиб", "дмитрий", "матвей"]):
                role = "urgent"

            # Генерируем историю для каждого чата
            for chat in chats:
                dialogue = random.choice(SCENARIOS[role])
                
                # Отматываем время назад, чтобы диалог был в прошлом
                base_time = datetime.utcnow() - timedelta(hours=random.randint(1, 48))

                for i, (sender, text) in enumerate(dialogue):
                    # Если пишет ИИ, ставим sender='assistant' (или support), если клиент - 'client'
                    # ВАЖНО: is_read=True, потому что это история, ИИ уже ответил
                    msg = Message(
                        conversation_id=chat.id,
                        content=text,
                        sender=sender, 
                        created_at=base_time + timedelta(minutes=i*2),
                        is_read=True 
                    )
                    session.add(msg)
        
        await session.commit()
        print(f"✅ Готово! История добавлена и помечена как прочитанная.")

if __name__ == "__main__":
    asyncio.run(fill_history())