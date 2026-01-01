import asyncio
import json
import random
import os
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
from zoneinfo import ZoneInfo

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    exit("Ошибка: токен не найден!")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# --- ФУНКЦИИ РАБОТЫ С ДАННЫМИ ---

def get_random_quote():
    try:
        base_path = os.path.dirname(__file__)
        file_path = os.path.join(base_path, 'quotes_Statham.json')
        with open(file_path, 'r', encoding='utf-8') as f:
            quotes = json.load(f)
            quote_data = random.choice(quotes)
            return quote_data.get('text', "Текст не найден") if isinstance(quote_data, dict) else str(quote_data)
    except:
        return "Цитаты временно закончились..."


def get_today_holiday():
    try:
        base_path = os.path.dirname(__file__)
        file_path = os.path.join(base_path, 'holidays.json')

        # Получаем текущую дату в формате ММ-ДД (как в вашем JSON)
        today_date = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%m-%d")

        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            holidays = data.get('holidays', [])

            for holiday in holidays:
                if holiday.get('date') == today_date:
                    return f"🎉 {holiday.get('name')}!\n{holiday.get('greeting')}"
        return None
    except Exception as e:
        print(f"Ошибка парсинга праздников: {e}")
        return None


# --- ОБРАБОТЧИКИ ---

@dp.inline_query()
async def inline_handler(query: types.InlineQuery):
    user_name = query.from_user.first_name or "Друг"
    quote_text = get_random_quote()
    holiday_text = get_today_holiday()

    results = [
        # 1. Цитата
        InlineQueryResultArticle(
            id="quote_random",
            title="📜 Выдать случайную цитату",
            input_message_content=InputTextMessageContent(message_text=f"📜 {quote_text}")
        )
    ]

    # 2. Праздник (добавляем в список, только если он сегодня есть)
    if holiday_text:
        results.append(
            InlineQueryResultArticle(
                id="holiday_today",
                title="🥳 Поздравить с праздником!",
                description="Сегодня важный день",
                input_message_content=InputTextMessageContent(message_text=holiday_text)
            )
        )
    else:
        results.append(
            InlineQueryResultArticle(
                id="no_holiday",
                title="📅 Праздников сегодня нет",
                description="Обычный рабочий день...",
                input_message_content=InputTextMessageContent(
                    message_text="Сегодня нет праздников, но я всё равно желаю тебе хорошего дня!")
            )
        )

    # 3. Приветствие
    results.append(
        InlineQueryResultArticle(
            id="greeting",
            title="👋 Приветствие",
            input_message_content=InputTextMessageContent(message_text=f"Привет, {user_name}!")
        )
    )

    await query.answer(results, cache_time=1)


@dp.message(F.text.lower().contains("митя, выдай цитату"))
async def quote_handler(message: types.Message):
    await message.answer(f"📜 {get_random_quote()}")


@dp.message(F.text.lower().contains("пидор"))
async def insult_handler(message: types.Message):
    user_name = message.from_user.first_name or "Друг"
    await message.answer(f"Пидор - {user_name}!", reply_to_message_id=message.message_id)


async def main():
    print("Митя запущен. Праздники и цитаты на связи!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
