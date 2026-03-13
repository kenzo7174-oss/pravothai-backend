"""
Axoloti Terminal — Инициализация базы данных и загрузка демо-данных.

Запуск:
    cd backend
    python init_db.py

Скрипт полностью пересоздаёт таблицы и наполняет их «золотым сценарием»
для презентации омниканальной CRM.
"""

import asyncio
from datetime import datetime, timedelta

from app.core.database import engine, AsyncSessionLocal, Base
from app.models import Client, Conversation, Message, SystemSettings, DEFAULT_SENIOR_WELCOME_MESSAGE


# ── Вспомогательные временные метки ──────────────────────────────────────
NOW = datetime.utcnow()
YESTERDAY = NOW - timedelta(days=1)
THIS_MORNING = NOW.replace(hour=9, minute=15, second=0, microsecond=0)
FIVE_MIN_AGO = NOW - timedelta(minutes=5)
TWO_MIN_AGO = NOW - timedelta(minutes=2)


async def seed_demo_data() -> None:
    """Создаёт «Золотой сценарий» с VIP-клиентом и 4 обычными клиентами."""

    async with AsyncSessionLocal() as session:
        async with session.begin():

            # ══════════════════════════════════════════════════════════════
            #  VIP-КЛИЕНТ: Анна Иванова — 3 диалога, развёрнутая история
            # ══════════════════════════════════════════════════════════════
            anna = Client(
                name="Анна Иванова",
                avatar="https://i.pravatar.cc/150?img=47",
                phone="+7 (916) 123-45-67",
                email="anna.ivanova@mail.ru",
                notes="VIP-клиент. Крупный заказ #1042. Предпочитает Telegram.",
                tags="vip,крупный заказ,приоритет",
            )

            # ── Telegram-диалог (вчера) ──────────────────────────────────
            tg_conv = Conversation(
                source="telegram",
                social_id="tg_123",
                label="Вопрос по доставке",
                messages=[
                    Message(
                        content="Здравствуйте! Подскажите, когда приедет мой заказ #1042?",
                        sender="client",
                        created_at=YESTERDAY.replace(hour=14, minute=30),
                        is_read=True,
                    ),
                    Message(
                        content="Анна, добрый день! Ваш заказ уже в пути, ожидайте завтра до 18:00.",
                        sender="operator",
                        created_at=YESTERDAY.replace(hour=14, minute=32),
                        is_read=True,
                    ),
                    Message(
                        content="Спасибо большое! Буду ждать 🙏",
                        sender="client",
                        created_at=YESTERDAY.replace(hour=14, minute=33),
                        is_read=True,
                    ),
                ],
            )

            # ── WhatsApp-диалог (сегодня утром) ─────────────────────────
            wa_conv = Conversation(
                source="whatsapp",
                social_id="wa_456",
                label="Возврат товара",
                messages=[
                    Message(
                        content="Доброе утро! Хочу оформить возврат по позиции 3 из заказа #1042.",
                        sender="client",
                        created_at=THIS_MORNING,
                        is_read=True,
                    ),
                    Message(
                        content="Здравствуйте, Анна! Сейчас уточню условия возврата, минуту.",
                        sender="operator",
                        created_at=THIS_MORNING + timedelta(minutes=2),
                        is_read=True,
                    ),
                    Message(
                        content="Отправила вам форму возврата на почту. Заполните и пришлите фото товара.",
                        sender="operator",
                        created_at=THIS_MORNING + timedelta(minutes=5),
                        is_read=False,
                    ),
                ],
            )

            # ── Site Widget-диалог (прямо сейчас, активный) ──────────────
            site_conv = Conversation(
                source="site",
                social_id="site_789",
                label="Консультация по новинкам",
                messages=[
                    Message(
                        content="Привет! Увидела на сайте новую коллекцию — есть в наличии?",
                        sender="client",
                        created_at=FIVE_MIN_AGO,
                        is_read=True,
                    ),
                    Message(
                        content="Добрый день! Да, коллекция уже на складе. Что именно вас интересует?",
                        sender="bot",
                        created_at=FIVE_MIN_AGO + timedelta(seconds=15),
                        is_read=True,
                    ),
                    Message(
                        content="Меня интересует сумка из кожи, артикул NK-2024. Какие цвета есть?",
                        sender="client",
                        created_at=TWO_MIN_AGO,
                        is_read=False,
                    ),
                ],
            )

            anna.conversations = [tg_conv, wa_conv, site_conv]
            session.add(anna)

            # ══════════════════════════════════════════════════════════════
            #  ОБЫЧНЫЕ КЛИЕНТЫ — по 1 диалогу, чтобы список не был пустым
            # ══════════════════════════════════════════════════════════════
            simple_clients = [
                {
                    "client": Client(
                        name="Пётр Сидоров",
                        avatar="https://i.pravatar.cc/150?img=12",
                        phone="+7 (903) 555-11-22",
                        email="petr.sid@gmail.com",
                        tags="новый",
                    ),
                    "conv": Conversation(
                        source="telegram",
                        social_id="tg_petr_001",
                        label="Первичная консультация",
                        messages=[
                            Message(
                                content="Добрый день, хотел бы узнать о ваших услугах.",
                                sender="client",
                                created_at=NOW - timedelta(hours=3),
                                is_read=True,
                            ),
                            Message(
                                content="Здравствуйте! Конечно, расскажу подробно. Что вас интересует?",
                                sender="operator",
                                created_at=NOW - timedelta(hours=2, minutes=55),
                                is_read=True,
                            ),
                        ],
                    ),
                },
                {
                    "client": Client(
                        name="Мария Козлова",
                        avatar="https://i.pravatar.cc/150?img=32",
                        phone="+7 (925) 777-88-99",
                        email="maria.k@yandex.ru",
                        tags="постоянный",
                    ),
                    "conv": Conversation(
                        source="whatsapp",
                        social_id="wa_maria_002",
                        label="Статус оплаты",
                        messages=[
                            Message(
                                content="Здравствуйте! Оплата прошла, но статус не обновился.",
                                sender="client",
                                created_at=NOW - timedelta(hours=1),
                                is_read=False,
                            ),
                            Message(
                                content="Мария, проверяем. Одну минуту, пожалуйста.",
                                sender="operator",
                                created_at=NOW - timedelta(minutes=55),
                                is_read=False,
                            ),
                        ],
                    ),
                },
                {
                    "client": Client(
                        name="Айдар Хасанов",
                        avatar="https://i.pravatar.cc/150?img=53",
                        phone="+7 (917) 333-44-55",
                        email="aydar.kh@inbox.ru",
                        tags="техподдержка",
                    ),
                    "conv": Conversation(
                        source="site",
                        social_id="site_aydar_003",
                        label="Не работает личный кабинет",
                        messages=[
                            Message(
                                content="Не могу зайти в личный кабинет — пишет 'ошибка сервера'.",
                                sender="client",
                                created_at=NOW - timedelta(minutes=40),
                                is_read=False,
                            ),
                            Message(
                                content="Автоматический ответ: Ваш запрос принят. Номер тикета: #T-0087.",
                                sender="bot",
                                created_at=NOW - timedelta(minutes=39),
                                is_read=False,
                            ),
                        ],
                    ),
                },
                {
                    "client": Client(
                        name="Никита Волков",
                        avatar="https://i.pravatar.cc/150?img=60",
                        phone="+7 (926) 111-22-33",
                        email="n.volkov@corp.ru",
                        tags="b2b,корпоративный",
                    ),
                    "conv": Conversation(
                        source="telegram",
                        social_id="tg_nikita_004",
                        label="Корпоративный тариф",
                        messages=[
                            Message(
                                content="Добрый день! Интересует корпоративный тариф на 50 сотрудников.",
                                sender="client",
                                created_at=NOW - timedelta(minutes=20),
                                is_read=False,
                            ),
                            Message(
                                content="Никита, здравствуйте! Отправляю вам коммерческое предложение.",
                                sender="operator",
                                created_at=NOW - timedelta(minutes=15),
                                is_read=False,
                            ),
                        ],
                    ),
                },
            ]

            for entry in simple_clients:
                client = entry["client"]
                client.conversations = [entry["conv"]]
                session.add(client)

            # Singleton системных настроек (id=1)
            session.add(SystemSettings(
                id=1,
                senior_welcome_message=DEFAULT_SENIOR_WELCOME_MESSAGE,
            ))

    print("✅ Демо-данные загружены: 5 клиентов, 7 диалогов, 17 сообщений, системные настройки.")


async def main() -> None:
    """Пересоздаёт все таблицы и заполняет демо-данными."""

    # Удаляем старые таблицы и создаём новые
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("🗄️  Таблицы пересозданы: clients, conversations, messages.")

    # Наполняем данными
    await seed_demo_data()

    # Закрываем соединение
    await engine.dispose()
    print("✅ База данных axolotl.db успешно создана и наполнена!")


if __name__ == "__main__":
    asyncio.run(main())
