import asyncio
import json
import random
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
from aiogram.filters import Command

# Загружаем переменные из файла .env
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    exit("Ошибка: токен не найден в переменных окружения!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

def get_random_quote():
    try:
        # Используем абсолютный путь, чтобы сервис всегда находил файл
        base_path = os.path.dirname(__file__)
        file_path = os.path.join(base_path, 'stet.json')
        
        with open(file_path, 'r', encoding='utf-8') as f:
            quotes = json.load(f)
            quote_data = random.choice(quotes)
            if isinstance(quote_data, dict):
                return quote_data.get('text', "Текст не найден")
            return str(quote_data)
    except Exception as e:
        print(f"Ошибка при чтении JSON: {e}")
        return "Цитаты временно закончились..."

@dp.inline_query()
async def inline_handler(query: types.InlineQuery):
    user_name = query.from_user.first_name or "Друг"
    quote_text = get_random_quote()
    
    results = [
        InlineQueryResultArticle(
            id="quote_random",
            title="📜 Выдать случайную цитату",
            description="Отправить фразу из коллекции",
            input_message_content=InputTextMessageContent(message_text=f"📜 {quote_text}")
        ),
        InlineQueryResultArticle(
            id="greeting",
            title="👋 Приветствие",
            description=f"Привет, {user_name}!",
            input_message_content=InputTextMessageContent(message_text=f"Привет, {user_name}!")
        )
    ]
    await query.answer(results, cache_time=1)

@dp.message(F.text.lower().contains("митя, выдай цитату"))
async def quote_handler(message: types.Message):
    quote_text = get_random_quote()
    await message.answer(f"📜 {quote_text}")

@dp.message(F.text.lower().contains("пидор"))
async def insult_handler(message: types.Message):
    user_name = message.from_user.first_name or "Друг"
    await message.answer(f"Пидор - {user_name}!", reply_to_message_id=message.message_id)

async def main():
    print("Митя запущен в защищенном режиме...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
