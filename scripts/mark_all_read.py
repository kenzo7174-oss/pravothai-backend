import asyncio
from app.core.database import AsyncSessionLocal
from app.models import Message
from sqlalchemy import select

async def cleanup_inbox():
    async with AsyncSessionLocal() as session:
        print("🧹 Наводим порядок... ИИ 'прочитывает' все сообщения.")
        
        # Находим все непрочитанные сообщения
        result = await session.execute(select(Message).where(Message.is_read == False))
        messages = result.scalars().all()
        
        for msg in messages:
            msg.is_read = True # Снимаем метку "Непрочитано"
            session.add(msg)
            
        await session.commit()
        print(f"✨ Готово! {len(messages)} сообщений помечены прочитанными. Точки исчезнут.")

if __name__ == "__main__":
    asyncio.run(cleanup_inbox())