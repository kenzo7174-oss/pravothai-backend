import asyncio
import random
from datetime import datetime, timedelta
from app.core.database import Base, engine, AsyncSessionLocal
from app.models import Client, Conversation, Message

# --- БАЗА ЗНАНИЙ ---
DIALOGUES = {
    "vip_order": [
        ("client", "Ева, привет! Заказ #7788 оформили?"),
        ("support", "Да, Анна! Уже упаковали."),
        ("client", "Супер. Положите пробники новой коллекции?"),
        ("support", "Обязательно! Положил 3 штуки."),
        ("client", "Спасибо, вы лучшие! ❤️")
    ],
    "delivery_q": [
        ("client", "Сколько идет посылка до Владивостока?"),
        ("support", "Добрый день! СДЭК — 5-7 дней, Почта — до 14 дней."),
        ("client", "Ого, долго. А авиа есть?"),
        ("support", "Есть, за 2 дня, но стоит 1500р."),
        ("client", "Нормально, оформляем.")
    ],
    "tech_bug": [
        ("client", "У меня кнопка 'Купить' серая, не нажимается."),
        ("support", "Какой у вас браузер?"),
        ("client", "Сафари на айфоне."),
        ("support", "Попробуйте обновить страницу. Мы как раз залили фикс."),
        ("client", "О, заработало. Спасибо.")
    ],
    "admin_talk": [
        ("client", "Сервер перезагрузили?"),
        ("support", "Да, Айдар, логи чистые."),
        ("client", "Отлично. Я тогда выкатываю обновление."),
        ("support", "Давай, я на страховке.")
    ],
    "collab": [
        ("client", "Здравствуйте! Я блогер, хочу рекламу у вас."),
        ("support", "Добрый день! Пришлите статистику профиля."),
        ("client", "Вот скриншоты статистики."),
        ("support", "Передал маркетологу, ответим завтра.")
    ],
    "return_item": [
        ("client", "Пришел разбитый флакон!"),
        ("support", "Ужас! Пришлите фото, пожалуйста."),
        ("client", "Вот фото [img_22.jpg]."),
        ("support", "Вижу. Высылаем новый за наш счет сегодня же.")
    ],
    "simple_hello": [
        ("client", "Работаете?"),
        ("support", "Да, до 21:00."),
        ("client", "Зайду через час.")
    ],
    "payment_fail": [
        ("client", "Оплата не проходит, пишет ошибка банка."),
        ("support", "Попробуйте другую карту или СБП."),
        ("client", "Через СБП прошло, спасибо."),
        ("support", "Отлично, видим оплату.")
    ],
    "thanks": [
        ("client", "Всё получил, качество огонь!"),
        ("support", "Рады стараться! Носите с удовольствием."),
        ("client", "Закажу еще другу.")
    ],
    "wrong_address": [
        ("client", "Я улицу перепутал в заказе!"),
        ("support", "Не страшно, какой верный адрес?"),
        ("client", "Ленина 5, а не 50."),
        ("support", "Поправили. Курьер поедет на Ленина 5.")
    ]
}

# Данные для генерации профилей
DEVICES = [
    "iPhone 15 Pro / iOS 17.3", "Samsung S24 / Android 14", "Windows 11 / Chrome 122", 
    "MacBook Air M2 / macOS Sonoma", "Xiaomi Redmi / Android 13", "iPad Pro / Safari"
]
CITIES = ["Москва", "Санкт-Петербург", "Казань", "Екатеринбург", "Новосибирск", "Сочи", "Владивосток"]

async def reset_and_fill():
    # 1. Снос и создание базы (ВАЖНО для применения новых колонок)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    async with AsyncSessionLocal() as session:
        print("🚀 Генерируем список с ПОЛНЫМИ профилями...")

        # ==========================================
        # 1. АННА ИВАНОВА (ГРУППА - VIP)
        # ==========================================
        anna = Client(
            name="Анна Иванова", 
            phone="+7 (999) 111-22-33", 
            email="anna.vip@gmail.com",
            location="Москва, Россия",
            website="instagram.com/anna_style",
            device_info="iPhone 15 Pro Max / iOS 17.4",
            notes="VIP Клиент. Любит быструю доставку.", 
            tags="vip", 
            is_pinned=True
        )
        session.add(anna)
        await session.commit()
        await create_chat(session, anna, "WhatsApp", "vip_order", minutes_ago=0)
        await create_chat(session, anna, "Telegram", "simple_hello", hours_ago=1)
        await create_chat(session, anna, "Email", "collab", hours_ago=2)

        # ==========================================
        # 2. ПРОСЛОЙКА (3 ЧЕЛОВЕКА)
        # ==========================================
        # Посетитель 101
        g1 = Client(
            name="Посетитель #101", 
            location="Санкт-Петербург", 
            device_info=random.choice(DEVICES), 
            tags="ghost"
        )
        session.add(g1)
        await session.commit()
        await create_chat(session, g1, "Live Chat", "delivery_q", minutes_ago=10)

        # Посетитель 102
        g2 = Client(
            name="Посетитель #102", 
            location="Омск", 
            device_info=random.choice(DEVICES), 
            tags="ghost"
        )
        session.add(g2)
        await session.commit()
        await create_chat(session, g2, "Live Chat", "wrong_address", minutes_ago=20)

        # Елена Смирнова
        girl = Client(
            name="Елена Смирнова", 
            email="elena.s@mail.ru",
            location="Сочи", 
            device_info="Samsung Galaxy S23", 
            tags="new"
        )
        session.add(girl)
        await session.commit()
        await create_chat(session, girl, "Instagram", "thanks", minutes_ago=30)

        # ==========================================
        # 3. АЙДАР ГАЛИУЛИН (ГРУППА - ADMIN)
        # ==========================================
        ajdar = Client(
            name="Айдар Галиулин", 
            phone="+7 (999) 888-77-66", 
            email="aidar.dev@axolotl.ru",
            location="Казань, Татарстан",
            website="github.com/aidar_dev",
            device_info="MacBook Pro M3 / macOS Sonoma",
            notes="Admin. Доступ ко всем логам системы.", 
            tags="admin"
        )
        session.add(ajdar)
        await session.commit()
        await create_chat(session, ajdar, "Telegram", "admin_talk", minutes_ago=40)
        await create_chat(session, ajdar, "WhatsApp", "tech_bug", hours_ago=5)
        await create_chat(session, ajdar, "Email", "simple_hello", days_ago=1)

        # ==========================================
        # 4. ПРОСЛОЙКА (1 ЧЕЛОВЕК)
        # ==========================================
        g3 = Client(
            name="Посетитель #103", 
            location="Тверь", 
            device_info=random.choice(DEVICES), 
            tags="ghost"
        )
        session.add(g3)
        await session.commit()
        await create_chat(session, g3, "Live Chat", "payment_fail", minutes_ago=50)

        # ==========================================
        # 5. ХАБИБ (ГРУППА - URGENT)
        # ==========================================
        habib = Client(
            name="Хабиб", 
            phone="+7 (900) 555-44-33", 
            email="habib.tech@yandex.ru",
            location="Махачкала, РФ",
            website="t.me/habib_support",
            device_info="Windows 11 / Chrome 122",
            notes="Срочные вопросы. Реагировать за 5 минут.", 
            tags="urgent"
        )
        session.add(habib)
        await session.commit()
        await create_chat(session, habib, "Live Chat", "tech_bug", minutes_ago=60)
        await create_chat(session, habib, "Telegram", "delivery_q", hours_ago=3)

        # ==========================================
        # 6. СТАРЫЕ ДИАЛОГИ (Oldies)
        # ==========================================
        
        # Максим Шабаев
        maxim = Client(
            name="Максим Шабаев", 
            location="Новосибирск", 
            device_info="Xiaomi 13 Ultra", 
            tags="old"
        )
        session.add(maxim)
        await session.commit()
        await create_chat(session, maxim, "Telegram", "return_item", days_ago=1)

        # Никита Мохитов
        nikita = Client(
            name="Никита Мохитов", 
            location="Екатеринбург", 
            device_info="iPhone 12", 
            tags="old"
        )
        session.add(nikita)
        await session.commit()
        await create_chat(session, nikita, "WhatsApp", "collab", days_ago=2)

        # Константин Петрович
        kostya = Client(
            name="Константин Петрович", 
            location="Ростов-на-Дону", 
            device_info="PC / Opera Browser", 
            tags="old"
        )
        session.add(kostya)
        await session.commit()
        await create_chat(session, kostya, "Odnoklassniki", "simple_hello", days_ago=5)

        # Мухаммад Якубов
        muhammad = Client(
            name="Мухаммад Якубов", 
            location="Душанбе", 
            device_info="Android Mobile", 
            tags="archive"
        )
        session.add(muhammad)
        await session.commit()
        await create_chat(session, muhammad, "Telegram", "thanks", days_ago=10)

        # ==========================================
        # 7. ПОДВАЛ (6 НОВЫХ ПОСЕТИТЕЛЕЙ)
        # ==========================================
        scenarios = ["delivery_q", "wrong_address", "payment_fail", "simple_hello", "thanks", "collab"]
        
        for i in range(6):
            uid = random.randint(2000, 9000)
            ghost = Client(
                name=f"Посетитель #{uid}", 
                device_info=random.choice(DEVICES), # <-- Теперь у всех есть инфо
                location=random.choice(CITIES),     # <-- И город
                tags="ghost"
            )
            session.add(ghost)
            await session.commit()
            
            scenario = scenarios[i % len(scenarios)]
            await create_chat(session, ghost, "Live Chat", scenario, days_ago=12 + i)

        await session.commit()
        print("✅ ГОТОВО: Профили полные, порядок идеальный.")

async def create_chat(session, client, source, scenario_key, minutes_ago=0, hours_ago=0, days_ago=0):
    chat = Conversation(
        client_id=client.id, 
        source=source.lower().replace(" ", ""),
        social_id=f"@{client.name}_id", 
        label=source
    )
    session.add(chat)
    await session.commit()

    dialogue = DIALOGUES.get(scenario_key, DIALOGUES["simple_hello"])
    
    end_time = datetime.utcnow() - timedelta(days=days_ago, hours=hours_ago, minutes=minutes_ago)
    start_time = end_time - timedelta(minutes=len(dialogue)*2)

    for k, (sender, text) in enumerate(dialogue):
        msg = Message(
            conversation_id=chat.id,
            content=text,
            sender=sender,
            created_at=start_time + timedelta(minutes=k*2),
            is_read=True
        )
        session.add(msg)

if __name__ == "__main__":
    asyncio.run(reset_and_fill())